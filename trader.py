import asyncio
import base64
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config
from agent import MarketAgent
from database import init_db, log_event, record_trade, record_decision, get_setting, set_setting


def _load_private_key():
    """Load the RSA private key (always live — demo mode is paper trading)."""
    import os

    raw = os.getenv("KALSHI_LIVE_PRIVATE_KEY") or os.getenv("KALSHI_PRIVATE_KEY")
    if raw:
        return serialization.load_pem_private_key(raw.encode(), password=None)

    with open(config.KALSHI_LIVE_PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign_request(private_key, method: str, path: str) -> dict:
    """Build Kalshi auth headers with RSA-PSS signature.

    Signs: {timestamp_ms}{METHOD}{path_without_query}
    """
    timestamp_ms = str(int(time.time() * 1000))
    clean_path = path.split("?")[0]
    message = f"{timestamp_ms}{method}{clean_path}".encode("utf-8")

    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
    }


class TradingBot:
    """Async trading engine for Kalshi BTC 15-min binary markets."""

    PATH_PREFIX = "/trade-api/v2"

    def __init__(self, alpha_monitor=None):
        init_db()

        # Restore persisted environment preference (survives restarts)
        saved_env = get_setting("env")
        if saved_env and saved_env in ("demo", "live") and saved_env != config.KALSHI_ENV:
            config.switch_env(saved_env)

        self.agent = MarketAgent()
        self.alpha = alpha_monitor
        self.running = False
        self.http: httpx.AsyncClient | None = None
        self.private_key = _load_private_key()
        self._active_env = config.KALSHI_ENV
        self._start_balance: float | None = None  # set on first cycle
        self._start_exposure: float = 0.0        # open position cost at start
        self._free_rolled: set[str] = set()      # tickers where we already sold half

        # Paper trading state (used in demo/paper mode)
        self._paper_balance: float = config.PAPER_STARTING_BALANCE
        self._paper_positions: dict[str, dict] = {}  # ticker -> {side, quantity, avg_price_cents, market_exposure_cents}
        self._paper_trades: list[dict] = []
        self._last_paper_ticker: str | None = None

        # Live state exposed to the dashboard
        self.status: dict[str, Any] = {
            "running": False,
            "balance": 0.0,
            "day_pnl": 0.0,
            "position_pnl": 0.0,
            "active_position": None,
            "current_market": None,
            "last_action": "Idle",
            "last_decision": None,
            "cycle_count": 0,
            "env": config.KALSHI_ENV,
            "alpha_latency_delta": 0.0,
            "alpha_delta_momentum": 0.0,
            "alpha_delta_baseline": 0.0,
            "alpha_projected_settlement": 0.0,
            "alpha_binance_connected": False,
            "alpha_coinbase_connected": False,
            "alpha_override": None,
            "seconds_to_close": None,
            "strike_price": None,
            "close_time": None,
            "market_title": None,
        }

    @property
    def base_host(self) -> str:
        return config.KALSHI_HOST

    @property
    def paper_mode(self) -> bool:
        return config.KALSHI_ENV == "demo"

    def _save_paper_state(self):
        """Persist paper balance and positions to DB so state survives restarts."""
        set_setting("paper_balance", str(self._paper_balance))
        set_setting("paper_positions", json.dumps(self._paper_positions))
        set_setting("paper_last_ticker", self._last_paper_ticker or "")

    def _restore_paper_state(self):
        """Restore paper trading state from DB after a restart."""
        saved_balance = get_setting("paper_balance")
        if saved_balance is not None:
            try:
                self._paper_balance = float(saved_balance)
                log_event("INFO", f"Restored paper balance: ${self._paper_balance:.2f}")
            except ValueError:
                pass

        saved_positions = get_setting("paper_positions")
        if saved_positions:
            try:
                self._paper_positions = json.loads(saved_positions)
                if self._paper_positions:
                    log_event("INFO", f"Restored {len(self._paper_positions)} paper position(s)")
            except (json.JSONDecodeError, ValueError):
                pass

        saved_ticker = get_setting("paper_last_ticker")
        if saved_ticker:
            self._last_paper_ticker = saved_ticker

    def reset_paper_trading(self):
        """Reset all paper trading state — balance, positions, and trade history."""
        self._paper_balance = config.PAPER_STARTING_BALANCE
        self._paper_positions = {}
        self._last_paper_ticker = None
        self._start_balance = None
        self._start_exposure = 0.0
        self._save_paper_state()
        log_event("INFO", f"Paper trading reset — balance: ${config.PAPER_STARTING_BALANCE:.2f}")

    async def switch_environment(self, env: str):
        """Switch between 'demo' and 'live'. Stops the bot, swaps creds, resets client."""
        was_running = self.running
        if was_running:
            self.stop()
            # Give the loop a moment to exit
            await asyncio.sleep(1)

        config.switch_env(env)
        self.private_key = _load_private_key()
        self._active_env = env

        # Force new HTTP client on next request
        if self.http and not self.http.is_closed:
            await self.http.aclose()
        self.http = None

        self.status["env"] = env
        self.status["balance"] = 0.0
        self.status["day_pnl"] = 0.0
        self.status["active_position"] = None
        self.status["current_market"] = None
        self.status["cycle_count"] = 0
        self._start_balance = None  # reset so P&L recalculates for new env
        self._start_exposure = 0.0
        set_setting("env", env)

        # Restore or initialize paper trading state
        if env == "demo":
            self._restore_paper_state()
            log_event("INFO", f"Switched to PAPER mode (balance: ${self._paper_balance:.2f})")
        else:
            log_event("INFO", "Switched to LIVE environment")

    # ------------------------------------------------------------------
    # HTTP helpers (Kalshi REST API via httpx + RSA-PSS auth)
    # ------------------------------------------------------------------

    async def _ensure_client(self):
        if self.http is None or self.http.is_closed:
            self.http = httpx.AsyncClient(
                base_url=self.base_host,
                timeout=httpx.Timeout(30.0, connect=15.0),
            )

    def _full_path(self, path: str) -> str:
        """Prepend the API prefix to a relative path."""
        return f"{self.PATH_PREFIX}{path}"

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """HTTP request with automatic retry on transient network errors."""
        full = self._full_path(path)
        for attempt in range(3):
            await self._ensure_client()
            headers = _sign_request(self.private_key, method, full)
            try:
                resp = await self.http.request(method, full, headers=headers, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
                if attempt < 2:
                    wait = 2 ** attempt  # 1s, 2s
                    log_event("ERROR", f"{type(exc).__name__} on {method} {path} — retry {attempt+1}/2 in {wait}s")
                    await asyncio.sleep(wait)
                    # Force a fresh connection on retry
                    if self.http and not self.http.is_closed:
                        await self.http.aclose()
                    self.http = None
                else:
                    raise

    async def _get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, body: dict) -> dict:
        return await self._request("POST", path, json=body)

    async def _delete(self, path: str) -> dict:
        return await self._request("DELETE", path)

    # ------------------------------------------------------------------
    # Kalshi API wrappers
    # ------------------------------------------------------------------

    async def fetch_balance(self) -> float:
        if self.paper_mode:
            return self._paper_balance
        data = await self._get("/portfolio/balance")
        # Balance returned in cents
        balance_cents = data.get("balance", 0)
        return balance_cents / 100.0

    async def fetch_active_market(self) -> dict | None:
        """Find the currently active KXBTC15M market."""
        data = await self._get(
            "/markets",
            params={
                "series_ticker": config.MARKET_SERIES,
                "status": "open",
                "limit": 5,
            },
        )
        markets = data.get("markets", [])
        if not markets:
            return None

        # Pick the market closest to closing that is still tradeable
        now = datetime.now(timezone.utc)
        best = None
        for m in markets:
            close_str = m.get("close_time") or m.get("expected_expiration_time")
            if not close_str:
                continue
            close_time = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            secs_left = (close_time - now).total_seconds()
            if secs_left > 0:
                m["_seconds_to_close"] = secs_left
                if best is None or secs_left < best["_seconds_to_close"]:
                    best = m
        return best

    async def fetch_orderbook(self, ticker: str) -> dict:
        data = await self._get(f"/markets/{ticker}/orderbook")
        return data.get("orderbook", data)

    async def fetch_positions(self) -> list[dict]:
        if self.paper_mode:
            return [
                {
                    "ticker": ticker,
                    "position": p["quantity"] if p["side"] == "yes" else -p["quantity"],
                    "market_exposure": p["market_exposure_cents"],
                }
                for ticker, p in self._paper_positions.items()
                if p["quantity"] > 0
            ]
        data = await self._get("/portfolio/positions", params={"limit": 20})
        return data.get("market_positions", [])

    async def cancel_all_orders(self):
        if self.paper_mode:
            return  # No real orders to cancel in paper mode
        try:
            await _safe(self._post("/portfolio/orders/batched", {"action": "cancel_all"}))
        except Exception:
            pass

    async def place_order(
        self, ticker: str, side: str, price_cents: int, quantity: int
    ) -> dict | None:
        if self.paper_mode:
            return self._paper_place_order(ticker, side, price_cents, quantity)

        body = {
            "ticker": ticker,
            "action": "buy",
            "side": side.lower(),
            "type": "limit",
            "yes_price" if side.lower() == "yes" else "no_price": price_cents,
            "count": quantity,
        }
        try:
            result = await self._post("/portfolio/orders", body)
            order = result.get("order", {})
            order_id = order.get("order_id", "unknown")
            status = order.get("status", "")
            filled_count = order.get("filled_count", 0)
            remaining = order.get("remaining_count", quantity)

            log_event("TRADE", f"Placed {side} limit @ {price_cents}c x{quantity} on {ticker} (status={status})")

            # Only record as a trade if the order actually filled (fully or partially)
            if filled_count > 0:
                record_trade(
                    market_id=ticker,
                    side=side,
                    action="BUY",
                    price=price_cents / 100.0,
                    quantity=filled_count,
                    order_id=order_id,
                )
                log_event("TRADE", f"Filled {filled_count}x {side} @ {price_cents}c on {ticker}")
            return order
        except httpx.HTTPStatusError as exc:
            log_event("ERROR", f"Order rejected: {exc.response.text[:200]}")
            return None

    def _paper_place_order(self, ticker: str, side: str, price_cents: int, quantity: int) -> dict:
        """Simulate a buy order in paper mode."""
        cost_cents = price_cents * quantity
        cost_dollars = cost_cents / 100.0

        if cost_dollars > self._paper_balance:
            log_event("SIM", f"[PAPER] Insufficient balance: need ${cost_dollars:.2f}, have ${self._paper_balance:.2f}")
            return None

        self._paper_balance -= cost_dollars

        # Accumulate position
        if ticker in self._paper_positions:
            pos = self._paper_positions[ticker]
            if pos["side"] == side:
                # Same side — average in
                old_total = pos["avg_price_cents"] * pos["quantity"]
                new_total = price_cents * quantity
                pos["quantity"] += quantity
                pos["avg_price_cents"] = (old_total + new_total) / pos["quantity"]
                pos["market_exposure_cents"] += cost_cents
            else:
                # Opposite side — reduce position
                reduce = min(quantity, pos["quantity"])
                pos["quantity"] -= reduce
                pos["market_exposure_cents"] -= pos["avg_price_cents"] * reduce
                if pos["quantity"] <= 0:
                    del self._paper_positions[ticker]
        else:
            self._paper_positions[ticker] = {
                "side": side,
                "quantity": quantity,
                "avg_price_cents": price_cents,
                "market_exposure_cents": cost_cents,
            }

        order_id = f"paper-{int(time.time() * 1000)}"
        record_trade(
            market_id=f"[PAPER] {ticker}",
            side=side,
            action="BUY",
            price=price_cents / 100.0,
            quantity=quantity,
            order_id=order_id,
        )
        log_event("SIM", f"[PAPER] BUY {quantity}x {side.upper()} @ {price_cents}c on {ticker} (cost ${cost_dollars:.2f}, bal ${self._paper_balance:.2f})")
        self._save_paper_state()

        return {"order_id": order_id, "status": "filled", "filled_count": quantity, "remaining_count": 0}

    async def close_position(
        self, ticker: str, side: str, price_cents: int, quantity: int
    ) -> dict | None:
        """Sell an existing position at the given price."""
        if self.paper_mode:
            return self._paper_close_position(ticker, side, price_cents, quantity)

        body = {
            "ticker": ticker,
            "action": "sell",
            "side": side.lower(),
            "type": "limit",
            "yes_price" if side.lower() == "yes" else "no_price": price_cents,
            "count": quantity,
        }
        try:
            result = await self._post("/portfolio/orders", body)
            order = result.get("order", {})
            order_id = order.get("order_id", "unknown")
            status = order.get("status", "")
            filled_count = order.get("filled_count", 0)

            log_event("TRADE", f"SL SELL {side} @ {price_cents}c x{quantity} on {ticker} (status={status})")

            if filled_count > 0:
                record_trade(
                    market_id=ticker,
                    side=side,
                    action="SELL",
                    price=price_cents / 100.0,
                    quantity=filled_count,
                    order_id=order_id,
                )
                log_event("TRADE", f"SL filled {filled_count}x {side} @ {price_cents}c on {ticker}")
            return order
        except httpx.HTTPStatusError as exc:
            log_event("ERROR", f"SL order rejected: {exc.response.text[:200]}")
            return None

    def _paper_close_position(self, ticker: str, side: str, price_cents: int, quantity: int) -> dict | None:
        """Simulate selling a position in paper mode."""
        pos = self._paper_positions.get(ticker)
        if not pos or pos["quantity"] <= 0:
            log_event("SIM", f"[PAPER] No position to close for {ticker}")
            return None

        sell_qty = min(quantity, pos["quantity"])
        proceeds_cents = price_cents * sell_qty
        proceeds_dollars = proceeds_cents / 100.0

        self._paper_balance += proceeds_dollars

        pos["quantity"] -= sell_qty
        pos["market_exposure_cents"] -= pos["avg_price_cents"] * sell_qty
        if pos["quantity"] <= 0:
            del self._paper_positions[ticker]

        order_id = f"paper-sell-{int(time.time() * 1000)}"
        record_trade(
            market_id=f"[PAPER] {ticker}",
            side=side,
            action="SELL",
            price=price_cents / 100.0,
            quantity=sell_qty,
            order_id=order_id,
        )
        log_event("SIM", f"[PAPER] SELL {sell_qty}x {side.upper()} @ {price_cents}c on {ticker} (proceeds ${proceeds_dollars:.2f}, bal ${self._paper_balance:.2f})")
        self._save_paper_state()

        return {"order_id": order_id, "status": "filled", "filled_count": sell_qty, "remaining_count": 0}

    async def _settle_paper_positions(self, new_ticker: str):
        """Settle expired paper positions by checking the actual market result.

        Queries the Kalshi API for each expired market to determine whether
        YES or NO won.  Binary payout: winning side pays 100c/contract,
        losing side pays 0c.
        """
        if not self.paper_mode:
            return

        expired_tickers = [t for t in self._paper_positions if t != new_ticker]
        for ticker in expired_tickers:
            pos = self._paper_positions[ticker]
            qty = pos["quantity"]
            side = pos["side"]
            exposure_cents = pos["market_exposure_cents"]

            # Query Kalshi for the actual market result (may need retries —
            # Kalshi takes ~60s to settle after close)
            settle_price = 0
            result = ""
            market_data = {}
            for attempt in range(7):  # try up to 7 times over ~90s (covers 60s settlement)
                try:
                    mkt = await self._get(f"/markets/{ticker}")
                    market_data = mkt.get("market", mkt)
                    result = market_data.get("result", "")
                    if result:
                        break
                except Exception as exc:
                    log_event("ERROR", f"[PAPER] Could not fetch result for {ticker} (attempt {attempt+1}): {exc}")
                if attempt < 6:
                    await asyncio.sleep(15)

            if result and result.lower() == side:
                settle_price = 100
            elif not result:
                # Kalshi hasn't settled yet — use projected settlement as fallback
                if self.alpha and self.alpha.projected_settlement > 0:
                    strike = self._extract_strike(market_data) if market_data else None
                    if strike and strike > 0:
                        yes_wins = self.alpha.projected_settlement >= strike
                        if (yes_wins and side == "yes") or (not yes_wins and side == "no"):
                            settle_price = 100
                        log_event("SIM", f"[PAPER] Using projected settlement ${self.alpha.projected_settlement:.2f} vs strike ${strike:.2f} → {'YES' if yes_wins else 'NO'}")
                    else:
                        log_event("SIM", f"[PAPER] Market {ticker} result unknown, no strike — settling at 0")
                else:
                    log_event("SIM", f"[PAPER] Market {ticker} result unknown, no projection — settling at 0")

            log_event("SIM", f"[PAPER] Market {ticker} result: {result.upper() if result else 'PROJECTED'}")

            # Credit payout to paper balance
            payout_cents = settle_price * qty
            self._paper_balance += payout_cents / 100.0

            pnl_cents = payout_cents - exposure_cents
            outcome = "WON" if pnl_cents > 0 else "LOST" if pnl_cents < 0 else "BREAK-EVEN"
            log_event("SIM", f"[PAPER] SETTLED {ticker}: {qty}x {side.upper()} → {outcome} (payout ${payout_cents/100:.2f}, cost ${exposure_cents/100:.2f}, P&L ${pnl_cents/100:+.2f})")

            record_trade(
                market_id=f"[PAPER] {ticker}",
                side=side,
                action="SETTLED",
                price=settle_price / 100.0,  # cents → dollars (consistent with BUY)
                quantity=qty,
                order_id=f"paper-settle-{int(time.time() * 1000)}",
            )
            del self._paper_positions[ticker]
        self._save_paper_state()

    # ------------------------------------------------------------------
    # Safety layer ("reflexes")
    # ------------------------------------------------------------------

    def _time_guard(self, market: dict) -> bool:
        """Return True if safe to trade (enough time left)."""
        secs = market.get("_seconds_to_close", 0)
        if secs < config.MIN_SECONDS_TO_CLOSE:
            log_event("GUARD", f"Time guard: {secs:.0f}s left — too close to expiry")
            return False
        return True

    def _spread_guard(self, orderbook: dict) -> tuple[bool, int, int]:
        """Return (safe, best_yes_bid, best_yes_ask).

        best_yes_bid: highest YES bid (from YES orders).
        best_yes_ask: lowest YES ask (derived from NO orders: 100 - best_no_bid).
        If one side is missing, we still allow trading on the available side.
        """
        yes_orders = orderbook.get("yes", []) if isinstance(orderbook.get("yes"), list) else []
        no_orders = orderbook.get("no", []) if isinstance(orderbook.get("no"), list) else []

        if not yes_orders and not no_orders:
            log_event("GUARD", "Spread guard: empty orderbook")
            return False, 0, 100

        # Use max() — Kalshi may return levels in any order
        best_bid = max(p for p, q in yes_orders) if yes_orders else 0
        best_ask = (100 - max(p for p, q in no_orders)) if no_orders else 100

        # Two-sided market: enforce max spread
        if yes_orders and no_orders:
            spread = best_ask - best_bid
            if spread > config.MAX_SPREAD_CENTS:
                log_event("GUARD", f"Spread guard: {spread}c spread too wide")
                return False, best_bid, best_ask

        # One-sided market: allow trading (bot will place a limit order)
        return True, best_bid, best_ask

    def _extract_strike(self, market: dict) -> float | None:
        """Extract the strike / reference price from a KXBTC15M market.

        Tries structured fields first (floor_strike / strike_price),
        then falls back to parsing dollar amounts from yes_sub_title or title.
        """
        strike = market.get("floor_strike") or market.get("strike_price")
        if strike:
            try:
                val = float(strike)
                # BTC strikes are already in dollars (e.g., 83873.08).
                # Small values (<1000) might be cents from other market types.
                return val if val > 1000 else val / 100.0
            except (ValueError, TypeError):
                pass

        # Fall back to parsing dollar amounts from subtitles or title
        # yes_sub_title example: "Price to beat: $83,873.07"
        for field in ("yes_sub_title", "title"):
            text = market.get(field, "")
            match = re.search(r'\$([0-9,.]+)', text)
            if match:
                try:
                    return float(match.group(1).replace(",", ""))
                except ValueError:
                    pass
        return None

    async def _wait_and_retry(self, ticker: str, order_id: str, side: str,
                               price_cents: int, qty: int):
        """Wait 3s for a fill; cancel and retry 1c more aggressive if unfilled."""
        if self.paper_mode:
            return  # Paper orders fill instantly
        await asyncio.sleep(3)
        try:
            order_status = await self._get(f"/portfolio/orders/{order_id}")
            order_data = order_status.get("order", order_status)
            status = order_data.get("status", "")
            remaining = order_data.get("remaining_count", qty)

            if status == "resting" and remaining > 0:
                try:
                    await self._delete(f"/portfolio/orders/{order_id}")
                    log_event("ALPHA", f"Cancelled unfilled order {order_id}, retrying")
                except Exception:
                    pass

                new_price = price_cents + 1 if side == "yes" else price_cents - 1
                new_price = max(1, min(99, new_price))
                retry_order = await self.place_order(ticker, side, new_price, remaining)
                if retry_order:
                    self.status["last_action"] = f"Retry {side.upper()} @ {new_price}c x{remaining}"
                    log_event("ALPHA", f"Retry order placed: {side} @ {new_price}c x{remaining}")
        except Exception as exc:
            log_event("ERROR", f"Fill-check error ({type(exc).__name__}): {exc!r}")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self):
        self.running = True
        self.status["running"] = True
        log_event("INFO", "Trading bot started")

        try:
            while self.running:
                await self._cycle()
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log_event("INFO", "Trading bot cancelled")
        finally:
            self.running = False
            self.status["running"] = False
            if self.http and not self.http.is_closed:
                await self.http.aclose()
            log_event("INFO", "Trading bot stopped")

    async def _cycle(self):
        self.status["cycle_count"] += 1

        try:
            # 1. Refresh balance
            balance = await self.fetch_balance()
            self.status["balance"] = balance

            # 2. Find active market
            market = await self.fetch_active_market()
            if market is None:
                self.status["current_market"] = None
                self.status["last_action"] = "No open market found"
                self.status["seconds_to_close"] = None
                self.status["strike_price"] = None
                self.status["close_time"] = None
                self.status["market_title"] = None
                log_event("INFO", "No active KXBTC15M market found")
                return

            ticker = market.get("ticker", "")
            self.status["current_market"] = ticker
            self.status["seconds_to_close"] = market.get("_seconds_to_close")
            self.status["strike_price"] = self._extract_strike(market)
            self.status["close_time"] = market.get("close_time") or market.get("expected_expiration_time")
            self.status["market_title"] = market.get("title", "")
            # Store raw market for debug (exclude internal computed fields)
            self.status["_raw_market"] = {k: v for k, v in market.items() if not k.startswith("_")}

            # Settle expired paper positions when market changes
            if self.paper_mode and self._last_paper_ticker and self._last_paper_ticker != ticker:
                await self._settle_paper_positions(ticker)
            if self.paper_mode:
                if self._last_paper_ticker != ticker:
                    self._last_paper_ticker = ticker
                    self._save_paper_state()

            # Subscribe to live orderbook if Kalshi WS is connected
            if self.alpha and self.alpha.kalshi_connected:
                await self.alpha.subscribe_orderbook(ticker)

            # 3. Positions + P&L
            positions = await self.fetch_positions()
            my_pos = next((p for p in positions if p.get("ticker") == ticker), None)
            self.status["active_position"] = my_pos

            # Total cost of all open positions (cents → dollars)
            total_exposure_cents = sum(
                p.get("market_exposure", 0) or 0 for p in positions
            )
            total_exposure = total_exposure_cents / 100.0

            # Capture starting snapshot on first cycle
            if self._start_balance is None:
                self._start_balance = balance
                self._start_exposure = total_exposure
                log_event("INFO", f"Starting balance: ${balance:.2f}, exposure: ${total_exposure:.2f}")

            # Settled P&L: (balance + exposure) - (start_balance + start_exposure)
            # Buying a contract moves money from balance→exposure (net zero).
            # Settlement removes exposure and changes balance by payout (net = profit/loss).
            settled_pnl = (
                (balance + total_exposure) - (self._start_balance + self._start_exposure)
            )
            # Daily loss circuit breaker (percentage of starting balance)
            # Uses realized P&L only — unrealized swings shouldn't trigger halt
            max_daily_loss = self._start_balance * config.MAX_DAILY_LOSS_PCT / 100.0
            if settled_pnl < -max_daily_loss:
                log_event("GUARD", f"Daily loss guard: ${settled_pnl:.2f} exceeds -{config.MAX_DAILY_LOSS_PCT:.1f}% (${max_daily_loss:.2f}) limit")
                self.status["last_action"] = f"Daily loss limit hit (${settled_pnl:.2f})"
                return

            # 4. Orderbook (always fetch — needed for dashboard + P&L even during guards)
            live_ob = self.alpha.get_live_orderbook(ticker) if self.alpha else None
            ob = live_ob if live_ob else await self.fetch_orderbook(ticker)
            spread_ok, best_bid, best_ask = self._spread_guard(ob)

            # Store orderbook snapshot for dashboard
            yes_orders = ob.get("yes", []) if isinstance(ob.get("yes"), list) else []
            no_orders = ob.get("no", []) if isinstance(ob.get("no"), list) else []
            self.status["orderbook"] = {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": best_ask - best_bid,
                "yes_depth": sum(q for _, q in yes_orders),
                "no_depth": sum(q for _, q in no_orders),
            }

            # Unrealized position P&L (mark-to-market vs cost)
            secs_left = market.get("_seconds_to_close", 0)
            if my_pos:
                pos_qty = my_pos.get("position", 0) or 0
                pos_exposure_cents = my_pos.get("market_exposure", 0) or 0
                if pos_qty > 0:
                    # Long YES: value = best_bid × qty
                    mark_to_market = best_bid * pos_qty
                elif pos_qty < 0:
                    # Long NO: value = (100 - best_ask) × |qty|  (what NO side is worth)
                    mark_to_market = (100 - best_ask) * abs(pos_qty)
                else:
                    mark_to_market = 0
                self.status["position_pnl"] = (mark_to_market - pos_exposure_cents) / 100.0
            else:
                self.status["position_pnl"] = 0.0

            # All-time P&L: paper uses fixed starting balance, live uses session start
            if self.paper_mode:
                all_time_pnl = (balance + total_exposure) - config.PAPER_STARTING_BALANCE
            else:
                all_time_pnl = settled_pnl
            self.status["day_pnl"] = all_time_pnl + self.status["position_pnl"]

            # 5. Exit logic (stop-loss + profit-taking) — before time guard
            #    so hold-to-expiry can still fire, but after P&L is computed
            if my_pos:
                pos_qty = my_pos.get("position", 0) or 0
                pos_exposure_cents = my_pos.get("market_exposure", 0) or 0
                mark_to_market_exit = best_bid * pos_qty if pos_qty > 0 else (100 - best_ask) * abs(pos_qty) if pos_qty < 0 else 0

                if abs(pos_qty) > 0 and config.TRADING_ENABLED:
                    sell_side = "yes" if pos_qty > 0 else "no"
                    sell_price = best_bid if pos_qty > 0 else (100 - best_ask)
                    sell_price = max(1, min(99, sell_price))
                    current_value = sell_price

                    # Rule: Last-Minute Hold — don't sell in final stretch, ride to settlement
                    if secs_left < config.HOLD_EXPIRY_SECS:
                        log_event("GUARD", f"Hold-to-expiry: {secs_left:.0f}s left — riding to settlement")
                        self.status["last_action"] = f"Holding to expiry ({secs_left:.0f}s left)"
                        return

                    # Rule: Stop-loss (still active outside hold zone)
                    if config.STOP_LOSS_CENTS > 0:
                        loss_per_contract = (pos_exposure_cents - mark_to_market_exit) / abs(pos_qty)
                        if loss_per_contract >= config.STOP_LOSS_CENTS:
                            sell_qty = abs(pos_qty)
                            log_event("GUARD", f"Stop-loss triggered: down {loss_per_contract:.0f}c/contract (limit {config.STOP_LOSS_CENTS}c)")
                            order = await self.close_position(ticker, sell_side, sell_price, sell_qty)
                            if order:
                                self.status["last_action"] = f"SL: sold {sell_qty}x {sell_side.upper()} @ {sell_price}c"
                            else:
                                self.status["last_action"] = "SL: sell order rejected"
                            return

                    # Calculate profit for all profit-taking rules
                    avg_cost = pos_exposure_cents / abs(pos_qty) if abs(pos_qty) > 0 else 0
                    gain_pct = ((current_value - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0

                    # Rule: Hit-and-Run — instant exit at % profit (NO time restrictions)
                    if config.HIT_RUN_PCT > 0 and gain_pct >= config.HIT_RUN_PCT:
                        sell_qty = abs(pos_qty)
                        log_event("TRADE", f"Hit-and-run: +{gain_pct:.0f}% gain ({current_value}c vs {avg_cost:.0f}c cost) >= {config.HIT_RUN_PCT}% target — instant exit")
                        order = await self.close_position(ticker, sell_side, sell_price, sell_qty)
                        if order:
                            self.status["last_action"] = f"HIT&RUN: sold {sell_qty}x {sell_side.upper()} @ {sell_price}c (+{gain_pct:.0f}%)"
                        else:
                            self.status["last_action"] = "Hit&Run: sell order rejected"
                        return

                    # Rule: Pop-and-Drop — full exit at % profit with time remaining
                    if gain_pct >= config.PROFIT_TAKE_PCT and secs_left > config.PROFIT_TAKE_MIN_SECS:
                        sell_qty = abs(pos_qty)
                        log_event("TRADE", f"Profit take: +{gain_pct:.0f}% gain ({current_value}c vs {avg_cost:.0f}c cost) >= {config.PROFIT_TAKE_PCT}% target, {secs_left:.0f}s left — selling all")
                        order = await self.close_position(ticker, sell_side, sell_price, sell_qty)
                        if order:
                            self.status["last_action"] = f"TP: sold {sell_qty}x {sell_side.upper()} @ {sell_price}c (+{gain_pct:.0f}%)"
                        else:
                            self.status["last_action"] = "TP: sell order rejected"
                        return

                    # Rule: Free Roll — sell half at intermediate profit to lock in capital
                    if (current_value >= config.FREE_ROLL_PRICE
                            and ticker not in self._free_rolled
                            and abs(pos_qty) >= 2):
                        half_qty = max(1, abs(pos_qty) // 2)
                        log_event("TRADE", f"Free roll: {current_value}c >= {config.FREE_ROLL_PRICE}c — selling {half_qty}/{abs(pos_qty)} to lock in capital")
                        order = await self.close_position(ticker, sell_side, sell_price, half_qty)
                        if order:
                            self._free_rolled.add(ticker)
                            self.status["last_action"] = f"Free roll: sold {half_qty}x {sell_side.upper()} @ {sell_price}c"
                        else:
                            self.status["last_action"] = "Free roll: sell order rejected"
                        return

            # 6. Time guard
            if not self._time_guard(market):
                self.status["last_action"] = "Time guard — sleeping"
                await self.cancel_all_orders()
                return

            if not spread_ok:
                self.status["last_action"] = "Spread too wide — holding"
                return

            # 6. Build data payload for the agent
            # Use live ticker data if available for freshest volume/price
            live_tkr = self.alpha.get_live_ticker(ticker) if self.alpha else None
            market_data = {
                "ticker": ticker,
                "title": market.get("title", ""),
                "seconds_to_close": market.get("_seconds_to_close", 0),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": best_ask - best_bid,
                "last_price": live_tkr.get("yes_bid", market.get("last_price", 0)) if live_tkr else market.get("last_price", 0),
                "volume": live_tkr.get("volume", market.get("volume", 0)) if live_tkr else market.get("volume", 0),
            }
            # Enrich agent context with multi-exchange data
            if self.alpha:
                gwp = self.alpha.get_weighted_global_price()
                if gwp > 0:
                    market_data["weighted_btc_price"] = round(gwp, 2)
                    market_data["lead_lag_spread"] = round(self.alpha.lead_lag_spread, 2)
                    lead_p, settle_p, _ = self.alpha.get_lead_vs_settlement()
                    if lead_p > 0:
                        market_data["lead_price"] = round(lead_p, 2)
                    if settle_p > 0:
                        market_data["settlement_price"] = round(settle_p, 2)

            # ── ALPHA ENGINE OVERRIDES ──────────────────────────────
            alpha_override = None
            if self.alpha and self.alpha.binance_connected and self.alpha.coinbase_connected:
                momentum = self.alpha.delta_momentum
                secs_left = market.get("_seconds_to_close", 0)
                self.status["alpha_latency_delta"] = self.alpha.latency_delta
                self.status["alpha_delta_momentum"] = momentum
                self.status["alpha_delta_baseline"] = self.alpha.delta_baseline
                self.status["alpha_projected_settlement"] = self.alpha.projected_settlement
                self.status["alpha_binance_connected"] = True
                self.status["alpha_coinbase_connected"] = True
                self.status["alpha_weighted_price"] = self.alpha.get_weighted_global_price()
                self.status["alpha_lead_lag_spread"] = self.alpha.lead_lag_spread

                # Override 0: Lead-Lag Signal (weighted global price vs strike)
                # Uses all 6 exchanges to detect when BTC has moved but Kalshi
                # contracts haven't repriced yet (the "60-second lag" play).
                strike = self._extract_strike(market)
                if config.LEAD_LAG_ENABLED and strike and strike > 0:
                    signal, diff = self.alpha.get_signal(strike)
                    self.status["alpha_signal"] = signal
                    self.status["alpha_signal_diff"] = diff
                    if signal == "BULLISH":
                        alpha_override = "BUY_YES"
                        log_event("ALPHA", f"Lead-lag BUY_YES: global ${self.alpha.get_weighted_global_price():.2f} > strike ${strike:.2f} by ${diff:.2f}")
                    elif signal == "BEARISH":
                        alpha_override = "BUY_NO"
                        log_event("ALPHA", f"Lead-lag BUY_NO: global ${self.alpha.get_weighted_global_price():.2f} < strike ${strike:.2f} by ${abs(diff):.2f}")

                # Override 1: Front-Run (delta momentum — deviation from rolling baseline)
                # Only fires if lead-lag didn't already trigger
                if not alpha_override:
                    if momentum > config.DELTA_THRESHOLD:
                        alpha_override = "BUY_YES"
                        log_event("ALPHA", f"Front-run BUY_YES: momentum={momentum:+.2f} > {config.DELTA_THRESHOLD}")
                    elif momentum < -config.DELTA_THRESHOLD:
                        alpha_override = "BUY_NO"
                        log_event("ALPHA", f"Front-run BUY_NO: momentum={momentum:+.2f} < -{config.DELTA_THRESHOLD}")

                # Override 2: Anchor Defense (near expiry + holding position)
                if secs_left < config.ANCHOR_SECONDS_THRESHOLD and my_pos:
                    if strike and strike > 0:
                        projection_wins = self.alpha.get_settlement_projection(strike, secs_left)
                        pos_val = my_pos.get("position", 0) or 0
                        yes_qty = pos_val if pos_val > 0 else 0
                        no_qty = abs(pos_val) if pos_val < 0 else 0
                        if yes_qty and not projection_wins:
                            alpha_override = "BUY_NO"
                            log_event("ALPHA", f"Anchor defense: proj {self.alpha.projected_settlement:.2f} < strike {strike}, forcing BUY_NO")
                        elif no_qty and projection_wins:
                            alpha_override = "BUY_YES"
                            log_event("ALPHA", f"Anchor defense: proj {self.alpha.projected_settlement:.2f} >= strike {strike}, forcing BUY_YES")
            else:
                # Update connection status even when disconnected
                if self.alpha:
                    self.status["alpha_binance_connected"] = self.alpha.binance_connected
                    self.status["alpha_coinbase_connected"] = self.alpha.coinbase_connected

            self.status["alpha_override"] = alpha_override

            if not config.TRADING_ENABLED:
                self.status["last_action"] = "Trading disabled — dry run"
                if alpha_override:
                    decision = {"decision": alpha_override, "confidence": 1.0,
                                "reasoning": f"Alpha override: {alpha_override}"}
                else:
                    decision = await self.agent.analyze_market(market_data, my_pos)
                self.status["last_decision"] = decision
                return

            # 7. Alpha override or agent decision
            if alpha_override:
                action = alpha_override
                confidence = 1.0
                reasoning = f"Alpha engine override ({alpha_override})"
                decision = {"decision": action, "confidence": confidence, "reasoning": reasoning}
                self.status["last_decision"] = decision
                record_decision(
                    market_id=ticker, decision=action,
                    confidence=confidence, reasoning=reasoning, executed=True,
                )
            else:
                decision = await self.agent.analyze_market(market_data, my_pos)
                self.status["last_decision"] = decision

                action = decision["decision"]
                confidence = decision["confidence"]

                if action == "HOLD" or confidence < config.MIN_AGENT_CONFIDENCE:
                    self.status["last_action"] = f"Agent: {action} ({confidence:.0%})"
                    return

            # 8. Execute — cancel any stale resting orders first to prevent accumulation
            await self.cancel_all_orders()

            side = "yes" if action == "BUY_YES" else "no"

            # Same-side guard: never place orders against an existing position
            if my_pos:
                pos_val = my_pos.get("position", 0) or 0
                holding_yes = pos_val > 0
                holding_no = pos_val < 0
                if (holding_yes and side == "no") or (holding_no and side == "yes"):
                    held_side = "YES" if holding_yes else "NO"
                    log_event("GUARD", f"Same-side guard: holding {held_side}, blocked {side.upper()} order")
                    self.status["last_action"] = f"Blocked — already holding {held_side}"
                    return

            # Aggressive pricing on extreme momentum, else standard limit
            extreme_momentum = (
                self.alpha
                and abs(self.alpha.delta_momentum) > config.EXTREME_DELTA_THRESHOLD
            )
            if extreme_momentum and best_ask < 100 and best_bid > 0:
                # Cross the spread — hit the ask (YES) or bid (NO)
                price_cents = best_ask if side == "yes" else (100 - best_bid)
                log_event("ALPHA", f"Extreme momentum ({self.alpha.delta_momentum:+.2f}) — aggressive pricing at {price_cents}c")
            else:
                # Improve the best bid by 1c
                price_cents = best_bid + 1 if side == "yes" else (100 - best_ask + 1)
                price_cents = max(1, min(99, price_cents))

            # Respect price guards (avoid lottery tickets AND terrible risk/reward)
            effective_price = price_cents if side == "yes" else (100 - price_cents)
            if effective_price < config.MIN_CONTRACT_PRICE:
                log_event("GUARD", f"Price guard: {effective_price}c < {config.MIN_CONTRACT_PRICE}c min")
                self.status["last_action"] = "Price too cheap — holding"
                return
            if effective_price > config.MAX_CONTRACT_PRICE:
                log_event("GUARD", f"Price guard: {effective_price}c > {config.MAX_CONTRACT_PRICE}c max — bad risk/reward")
                self.status["last_action"] = f"Price too expensive ({effective_price}c) — holding"
                return

            # Portfolio-wide exposure guard (percentage of current balance)
            max_exposure = balance * config.MAX_TOTAL_EXPOSURE_PCT / 100.0
            if total_exposure >= max_exposure:
                log_event("GUARD", f"Exposure guard: ${total_exposure:.2f} >= {config.MAX_TOTAL_EXPOSURE_PCT:.1f}% (${max_exposure:.2f}) limit")
                self.status["last_action"] = f"Max exposure reached (${total_exposure:.2f})"
                return

            # Dynamic contract sizing from balance percentages
            # price_cents is the cost per contract we'd pay
            position_budget = balance * config.MAX_POSITION_PCT / 100.0
            max_position = max(1, int(position_budget / (price_cents / 100.0))) if price_cents > 0 else 1

            order_budget = balance * config.ORDER_SIZE_PCT / 100.0
            order_size = max(1, int(order_budget / (price_cents / 100.0))) if price_cents > 0 else 1

            # Re-fetch positions after cancel (fills may have occurred since initial fetch)
            positions = await self.fetch_positions()
            my_pos = next((p for p in positions if p.get("ticker") == ticker), None)
            self.status["active_position"] = my_pos

            # Check current position to avoid exceeding max
            current_qty = 0
            if my_pos:
                current_qty = abs(my_pos.get("position", 0) or 0)
            remaining_capacity = max_position - current_qty
            if remaining_capacity <= 0:
                log_event("GUARD", f"Position guard: {current_qty}/{max_position} contracts ({config.MAX_POSITION_PCT:.1f}% of balance)")
                self.status["last_action"] = f"Max position reached ({current_qty})"
                return

            qty = min(order_size, remaining_capacity)
            order = await self.place_order(ticker, side, price_cents, qty)
            if order:
                self.status["last_action"] = f"Placed {side.upper()} @ {price_cents}c x{qty}"
                # Fill-or-cancel for non-extreme orders
                if not extreme_momentum:
                    order_id = order.get("order_id")
                    if order_id:
                        await self._wait_and_retry(ticker, order_id, side, price_cents, qty)
            else:
                self.status["last_action"] = "Order rejected"

        except httpx.HTTPStatusError as exc:
            log_event("ERROR", f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
            self.status["last_action"] = f"API error {exc.response.status_code}"
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            log_event("ERROR", f"Cycle error ({type(exc).__name__}): {exc!r}")
            log_event("ERROR", f"Traceback: {tb[-500:]}")
            self.status["last_action"] = f"Error: {type(exc).__name__}: {exc}"

    def stop(self):
        self.running = False


async def _safe(coro):
    """Await a coroutine and swallow exceptions."""
    try:
        return await coro
    except Exception:
        return None
