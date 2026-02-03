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
from database import init_db, log_event, record_trade, record_decision, record_snapshot, get_entry_snapshot, get_unsettled_entry, get_setting, set_setting


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
        self._took_profit: set[str] = set()      # tickers where we've taken profit (prevent re-entry)
        self._edge_exit_ts: dict[str, float] = {}   # ticker -> timestamp of last edge-exit
        self._entry_ts: dict[str, float] = {}        # ticker -> timestamp of entry
        self._entry_edge: dict[str, float] = {}      # ticker -> edge at entry (cents)
        self._edge_exits_count: dict[str, int] = {}  # ticker -> number of edge-exits this contract

        # Paper trading state (used in demo/paper mode)
        self._paper_balance: float = config.PAPER_STARTING_BALANCE
        self._paper_positions: dict[str, dict] = {}  # ticker -> {side, quantity, avg_price_cents, market_exposure_cents}
        self._paper_trades: list[dict] = []
        self._last_paper_ticker: str | None = None
        self._paper_orderbook: dict | None = None  # latest orderbook snapshot for realistic paper fills

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
            "dashboard": None,
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
        """Find the currently active KXBTC15M market.

        When multiple contracts are open (overlap during settlement), prefer
        the newer contract that still has tradeable time rather than the old
        one that is about to expire.
        """
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

        now = datetime.now(timezone.utc)
        candidates = []
        for m in markets:
            close_str = m.get("close_time") or m.get("expected_expiration_time")
            if not close_str:
                continue
            close_time = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            secs_left = (close_time - now).total_seconds()
            if secs_left > 0:
                m["_seconds_to_close"] = secs_left
                candidates.append(m)

        if not candidates:
            return None

        # Sort by time remaining (ascending — soonest to close first)
        candidates.sort(key=lambda m: m["_seconds_to_close"])

        # If only one market, use it
        if len(candidates) == 1:
            return candidates[0]

        # Multiple contracts open (overlap window). If the soonest-to-close
        # contract is too close to expiry to trade, skip to the next one.
        # This lets us start trading the new contract immediately instead of
        # waiting for the old one to settle.
        if candidates[0]["_seconds_to_close"] < config.MIN_SECONDS_TO_CLOSE:
            log_event("INFO", f"Skipping expiring contract {candidates[0].get('ticker', '?')} ({candidates[0]['_seconds_to_close']:.0f}s left), switching to {candidates[1].get('ticker', '?')}")
            return candidates[1]

        # Otherwise pick the soonest (normal behavior)
        return candidates[0]

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

    def _simulate_fill(
        self, orderbook: dict, action: str, side: str,
        limit_price_cents: int, quantity: int,
    ) -> tuple[int, int, list[tuple[int, int]]]:
        """Walk the orderbook to simulate a realistic fill — crossing fills only.

        Exactly mirrors how Kalshi's matching engine works: only fills against
        resting orders whose price crosses your limit.

        Returns (filled_qty, avg_price_cents, [(price, qty), ...]).
        """
        fill_fraction = config.PAPER_FILL_FRACTION

        # Determine which book side to consume and price interpretation
        if action == "buy":
            if side == "yes":
                raw_levels = orderbook.get("no", [])
                # NO bid at P → YES available at (100-P); fillable when (100-P) <= limit
                levels = [
                    (100 - p, q) for p, q in raw_levels
                    if p >= (100 - limit_price_cents)
                ]
            else:
                raw_levels = orderbook.get("yes", [])
                levels = [
                    (100 - p, q) for p, q in raw_levels
                    if p >= (100 - limit_price_cents)
                ]
        else:  # sell
            if side == "yes":
                raw_levels = orderbook.get("yes", [])
                levels = [(p, q) for p, q in raw_levels if p >= limit_price_cents]
            else:
                raw_levels = orderbook.get("no", [])
                levels = [(p, q) for p, q in raw_levels if p >= limit_price_cents]

        # Sort: buys cheapest-first, sells best-price-first
        if action == "buy":
            levels.sort(key=lambda x: x[0])
        else:
            levels.sort(key=lambda x: x[0], reverse=True)

        filled = 0
        fills = []
        for price_at_level, qty_at_level in levels:
            if filled >= quantity:
                break
            available = max(1, int(qty_at_level * fill_fraction))
            take = min(available, quantity - filled)
            fills.append((price_at_level, take))
            filled += take

        if filled == 0:
            return 0, 0, []

        avg_price = sum(p * q for p, q in fills) / filled
        return filled, int(round(avg_price)), fills

    def _paper_place_order(self, ticker: str, side: str, price_cents: int, quantity: int) -> dict:
        """Simulate a buy order against the live orderbook depth."""
        ob = self._paper_orderbook
        if not ob:
            log_event("SIM", f"[PAPER] No orderbook available — skipping buy")
            return None

        filled_qty, avg_price, fills = self._simulate_fill(ob, "buy", side, price_cents, quantity)
        if filled_qty == 0:
            # No crossing fill — order rests on the book (same as live Kalshi behavior).
            # Return a "resting" order so the retry logic can fire and reprice.
            order_id = f"paper-{int(time.time() * 1000)}"
            log_event("SIM", f"[PAPER] Order resting: {side.upper()} @ {price_cents}c x{quantity} on {ticker} (no crossing liquidity)")
            return {"order_id": order_id, "status": "resting", "filled_count": 0, "remaining_count": quantity}

        cost_cents = avg_price * filled_qty
        cost_dollars = cost_cents / 100.0

        if cost_dollars > self._paper_balance:
            affordable = int(self._paper_balance * 100 / avg_price) if avg_price > 0 else 0
            if affordable <= 0:
                log_event("SIM", f"[PAPER] Insufficient balance: need ${cost_dollars:.2f}, have ${self._paper_balance:.2f}")
                return None
            filled_qty = affordable
            cost_cents = avg_price * filled_qty
            cost_dollars = cost_cents / 100.0

        self._paper_balance -= cost_dollars

        # Accumulate position using actual fill price (includes slippage)
        if ticker in self._paper_positions:
            pos = self._paper_positions[ticker]
            if pos["side"] == side:
                old_total = pos["avg_price_cents"] * pos["quantity"]
                new_total = avg_price * filled_qty
                pos["quantity"] += filled_qty
                pos["avg_price_cents"] = (old_total + new_total) / pos["quantity"]
                pos["market_exposure_cents"] += cost_cents
            else:
                reduce = min(filled_qty, pos["quantity"])
                pos["quantity"] -= reduce
                pos["market_exposure_cents"] -= pos["avg_price_cents"] * reduce
                if pos["quantity"] <= 0:
                    del self._paper_positions[ticker]
        else:
            self._paper_positions[ticker] = {
                "side": side,
                "quantity": filled_qty,
                "avg_price_cents": avg_price,
                "market_exposure_cents": cost_cents,
            }

        order_id = f"paper-{int(time.time() * 1000)}"
        remaining = quantity - filled_qty
        slippage = avg_price - price_cents if side == "yes" else price_cents - avg_price
        slip_str = f", slip {slippage:+d}c" if slippage != 0 else ""
        partial_str = f" (partial {filled_qty}/{quantity})" if remaining > 0 else ""

        record_trade(
            market_id=f"[PAPER] {ticker}",
            side=side,
            action="BUY",
            price=avg_price / 100.0,
            quantity=filled_qty,
            order_id=order_id,
        )
        log_event("SIM", f"[PAPER] BUY {filled_qty}x {side.upper()} @ {avg_price}c on {ticker}{partial_str}{slip_str} (cost ${cost_dollars:.2f}, bal ${self._paper_balance:.2f})")
        self._save_paper_state()

        return {"order_id": order_id, "status": "filled" if remaining == 0 else "partial", "filled_count": filled_qty, "remaining_count": remaining}

    async def close_position(
        self, ticker: str, side: str, price_cents: int, quantity: int, exit_type: str = "SELL"
    ) -> dict | None:
        """Sell an existing position at the given price.

        Args:
            exit_type: Type of exit - "SL" (stop loss), "TP" (take profit), or "SELL" (manual)
        """
        if self.paper_mode:
            return self._paper_close_position(ticker, side, price_cents, quantity, exit_type)

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

            log_event("TRADE", f"{exit_type} SELL {side} @ {price_cents}c x{quantity} on {ticker} (status={status})")

            if filled_count > 0:
                record_trade(
                    market_id=ticker,
                    side=side,
                    action="SELL",
                    price=price_cents / 100.0,
                    quantity=filled_count,
                    order_id=order_id,
                    exit_type=exit_type,
                )
                log_event("TRADE", f"{exit_type} filled {filled_count}x {side} @ {price_cents}c on {ticker}")
            return order
        except httpx.HTTPStatusError as exc:
            log_event("ERROR", f"{exit_type} order rejected: {exc.response.text[:200]}")
            return None

    def _paper_close_position(self, ticker: str, side: str, price_cents: int, quantity: int, exit_type: str = "SELL") -> dict | None:
        """Simulate selling a position against the live orderbook depth."""
        pos = self._paper_positions.get(ticker)
        if not pos or pos["quantity"] <= 0:
            log_event("SIM", f"[PAPER] No position to close for {ticker}")
            return None

        want_qty = min(quantity, pos["quantity"])
        ob = self._paper_orderbook

        if ob:
            filled_qty, avg_price, fills = self._simulate_fill(ob, "sell", side, price_cents, want_qty)
            if filled_qty == 0:
                log_event("SIM", f"[PAPER] No liquidity for {exit_type} {side.upper()} @ {price_cents}c on {ticker}")
                return None
        else:
            # No orderbook available — fall back to limit price (graceful degradation)
            filled_qty = want_qty
            avg_price = price_cents

        proceeds_cents = avg_price * filled_qty
        proceeds_dollars = proceeds_cents / 100.0

        self._paper_balance += proceeds_dollars

        pos["quantity"] -= filled_qty
        pos["market_exposure_cents"] -= pos["avg_price_cents"] * filled_qty
        if pos["quantity"] <= 0:
            del self._paper_positions[ticker]

        order_id = f"paper-sell-{int(time.time() * 1000)}"
        remaining = want_qty - filled_qty
        slippage = price_cents - avg_price if side == "yes" else avg_price - price_cents
        slip_str = f", slip {slippage:+d}c" if slippage != 0 else ""
        partial_str = f" (partial {filled_qty}/{want_qty})" if remaining > 0 else ""

        record_trade(
            market_id=f"[PAPER] {ticker}",
            side=side,
            action="SELL",
            price=avg_price / 100.0,
            quantity=filled_qty,
            order_id=order_id,
            exit_type=exit_type,
        )
        log_event("SIM", f"[PAPER] {exit_type} {filled_qty}x {side.upper()} @ {avg_price}c on {ticker}{partial_str}{slip_str} (proceeds ${proceeds_dollars:.2f}, bal ${self._paper_balance:.2f})")
        self._save_paper_state()

        return {"order_id": order_id, "status": "filled" if remaining == 0 else "partial", "filled_count": filled_qty, "remaining_count": remaining}

    async def _settle_paper_positions(self, new_ticker: str):
        """Settle expired paper positions by checking the actual market result.

        Queries the Kalshi API for each expired market to determine whether
        YES or NO won.  Binary payout: winning side pays 100c/contract,
        losing side pays 0c.

        Runs as a background task so the main loop can immediately start
        trading the next contract without waiting for settlement.
        """
        if not self.paper_mode:
            return

        expired_tickers = [t for t in list(self._paper_positions.keys()) if t != new_ticker]
        if not expired_tickers:
            return

        try:
            await self._do_settle(expired_tickers)
        except Exception as exc:
            log_event("ERROR", f"[PAPER] Background settlement failed: {exc}")

    async def _do_settle(self, expired_tickers: list[str]):
        """Inner settlement logic (separated for error handling)."""
        for ticker in expired_tickers:
            pos = self._paper_positions.get(ticker)
            if not pos:
                continue
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

            _settle_order_id = f"paper-settle-{int(time.time() * 1000)}"
            record_trade(
                market_id=f"[PAPER] {ticker}",
                side=side,
                action="SETTLED",
                price=settle_price / 100.0,  # cents → dollars (consistent with BUY)
                quantity=qty,
                order_id=_settle_order_id,
                exit_type="SETTLE",
            )
            try:
                _mid = f"[PAPER] {ticker}"
                _entry = get_entry_snapshot(_mid)
                _entry_ts = datetime.fromisoformat(_entry["ts"]).timestamp() if _entry else None
                record_snapshot({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "trade_id": _settle_order_id,
                    "market_id": _mid, "action": "SETTLE", "side": side,
                    "price_cents": settle_price, "quantity": qty,
                    "decision": "SETTLE", "confidence": 0, "trigger_type": "settlement",
                    "position_qty": qty,
                    "pnl_cents": pnl_cents,
                    "hold_duration_s": round(time.time() - _entry_ts, 1) if _entry_ts else None,
                    "entry_price_cents": _entry["price_cents"] if _entry else None,
                })
            except Exception:
                pass
            del self._paper_positions[ticker]
        self._save_paper_state()

    async def _settle_live_positions(self, old_ticker: str):
        """Record settlement for live positions that expired without an active exit.

        Mirrors _do_settle for paper mode: queries Kalshi for the market result
        and records a SETTLE trade + snapshot so the trade log shows properly.
        """
        try:
            entry = get_unsettled_entry(old_ticker)
            if not entry:
                return  # Already has an exit record, or no entry at all

            side = entry["side"]
            qty = entry.get("position_qty") or entry.get("quantity") or 0
            entry_price = entry.get("price_cents", 0)
            if qty <= 0:
                return

            # Query Kalshi for the actual result (retries — settlement takes ~60s)
            settle_price = 0
            result = ""
            for attempt in range(7):
                try:
                    mkt = await self._get(f"/markets/{old_ticker}")
                    market_data = mkt.get("market", mkt)
                    result = market_data.get("result", "")
                    if result:
                        break
                except Exception as exc:
                    log_event("ERROR", f"[LIVE] Could not fetch result for {old_ticker} (attempt {attempt+1}): {exc}")
                if attempt < 6:
                    await asyncio.sleep(15)

            if result and result.lower() == side:
                settle_price = 100
            elif not result:
                if self.alpha and self.alpha.projected_settlement > 0:
                    strike = self._extract_strike(market_data) if market_data else None
                    if strike and strike > 0:
                        yes_wins = self.alpha.projected_settlement >= strike
                        if (yes_wins and side == "yes") or (not yes_wins and side == "no"):
                            settle_price = 100

            exposure_cents = entry_price * qty
            payout_cents = settle_price * qty
            pnl_cents = payout_cents - exposure_cents
            outcome = "WON" if pnl_cents > 0 else "LOST" if pnl_cents < 0 else "BREAK-EVEN"
            log_event("TRADE", f"[LIVE] SETTLED {old_ticker}: {qty}x {side.upper()} → {outcome} (payout ${payout_cents/100:.2f}, cost ${exposure_cents/100:.2f}, P&L ${pnl_cents/100:+.2f})")

            _settle_order_id = f"live-settle-{int(time.time() * 1000)}"
            record_trade(
                market_id=old_ticker,
                side=side,
                action="SETTLED",
                price=settle_price / 100.0,
                quantity=qty,
                order_id=_settle_order_id,
                exit_type="SETTLE",
            )
            try:
                _entry = get_entry_snapshot(old_ticker)
                _entry_ts = datetime.fromisoformat(_entry["ts"]).timestamp() if _entry else None
                record_snapshot({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "trade_id": _settle_order_id,
                    "market_id": old_ticker, "action": "SETTLE", "side": side,
                    "price_cents": settle_price, "quantity": qty,
                    "decision": "SETTLE", "confidence": 0, "trigger_type": "settlement",
                    "position_qty": qty,
                    "pnl_cents": pnl_cents,
                    "hold_duration_s": round(time.time() - _entry_ts, 1) if _entry_ts else None,
                    "entry_price_cents": entry_price,
                })
            except Exception:
                pass
        except Exception as exc:
            log_event("ERROR", f"[LIVE] Settlement recording failed for {old_ticker}: {exc}")

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
                               price_cents: int, qty: int, initial_order: dict | None = None):
        """Chase fills with up to 3 retries, escalating toward the spread.

        Paper mode: retries escalate toward the ask — midpoint, then 2/3,
        then cross the spread. Standard +1c bumps can never cross wide
        spreads, but in reality resting orders get filled by counterparties.
        """
        max_retries = 3

        if self.paper_mode:
            remaining = (initial_order or {}).get("remaining_count", 0)
            if remaining <= 0:
                return  # Fully filled — nothing to retry

            for attempt in range(1, max_retries + 1):
                await asyncio.sleep(1)

                # Refresh orderbook (market may have moved)
                live_ob = self.alpha.get_live_orderbook(ticker) if self.alpha else None
                self._paper_orderbook = live_ob if live_ob else await self.fetch_orderbook(ticker)

                ob = self._paper_orderbook or {}
                yes_orders = ob.get("yes", [])
                no_orders = ob.get("no", [])
                cur_bid = max((p for p, q in yes_orders), default=0) if yes_orders else 0
                cur_ask = (100 - max((p for p, q in no_orders), default=0)) if no_orders else 100

                # Escalate: retry 1 → midpoint, retry 2 → 2/3 toward ask, retry 3 → cross spread
                if side == "yes":
                    if attempt == 1:
                        new_price = (cur_bid + cur_ask + 1) // 2      # midpoint
                    elif attempt == 2:
                        new_price = cur_bid + ((cur_ask - cur_bid) * 2) // 3  # 2/3
                    else:
                        new_price = cur_ask                           # cross spread
                else:
                    # For NO: work in no-price space (100 - yes prices)
                    no_bid = 100 - cur_ask  # best NO bid
                    no_ask = 100 - cur_bid  # best NO ask
                    if attempt == 1:
                        new_price = (no_bid + no_ask + 1) // 2
                    elif attempt == 2:
                        new_price = no_bid + ((no_ask - no_bid) * 2) // 3
                    else:
                        new_price = no_ask

                new_price = max(1, min(99, new_price))
                log_event("SIM", f"[PAPER] Retry {attempt}/{max_retries}: {remaining}x {side.upper()} @ {new_price}c (was {price_cents}c)")
                retry_order = await self.place_order(ticker, side, new_price, remaining)
                if retry_order:
                    filled = retry_order.get("filled_count", 0)
                    remaining = retry_order.get("remaining_count", remaining)
                    self.status["last_action"] = f"Retry {attempt} {side.upper()} @ {new_price}c x{filled} filled"
                    if remaining <= 0:
                        return  # Fully filled
                else:
                    return  # Order rejected (e.g., no orderbook)
            return

        current_order_id = order_id
        for attempt in range(1, max_retries + 1):
            await asyncio.sleep(1)
            try:
                order_status = await self._get(f"/portfolio/orders/{current_order_id}")
                order_data = order_status.get("order", order_status)
                status = order_data.get("status", "")
                remaining = order_data.get("remaining_count", qty)

                if status == "resting" and remaining > 0:
                    try:
                        await self._delete(f"/portfolio/orders/{current_order_id}")
                        log_event("ALPHA", f"Cancelled unfilled order {current_order_id}, retry {attempt}/{max_retries}")
                    except Exception:
                        pass

                    # Refresh orderbook and escalate toward the spread
                    live_ob = self.alpha.get_live_orderbook(ticker) if self.alpha else None
                    ob = live_ob if live_ob else await self.fetch_orderbook(ticker)
                    yes_orders = ob.get("yes", []) if isinstance(ob.get("yes"), list) else []
                    no_orders = ob.get("no", []) if isinstance(ob.get("no"), list) else []
                    cur_bid = max((p for p, q in yes_orders), default=0) if yes_orders else 0
                    cur_ask = (100 - max((p for p, q in no_orders), default=0)) if no_orders else 100

                    if side == "yes":
                        if attempt == 1:
                            new_price = (cur_bid + cur_ask + 1) // 2
                        elif attempt == 2:
                            new_price = cur_bid + ((cur_ask - cur_bid) * 2) // 3
                        else:
                            new_price = cur_ask
                    else:
                        no_bid = 100 - cur_ask
                        no_ask = 100 - cur_bid
                        if attempt == 1:
                            new_price = (no_bid + no_ask + 1) // 2
                        elif attempt == 2:
                            new_price = no_bid + ((no_ask - no_bid) * 2) // 3
                        else:
                            new_price = no_ask

                    new_price = max(1, min(99, new_price))
                    retry_order = await self.place_order(ticker, side, new_price, remaining)
                    if retry_order:
                        current_order_id = retry_order.get("order_id", current_order_id)
                        qty = remaining
                        self.status["last_action"] = f"Retry {attempt} {side.upper()} @ {new_price}c x{remaining}"
                        log_event("ALPHA", f"Retry {attempt}/{max_retries}: {side} @ {new_price}c x{remaining} (was {price_cents}c)")
                    else:
                        return  # Order rejected
                else:
                    return  # Filled or cancelled
            except Exception as exc:
                log_event("ERROR", f"Fill-check error ({type(exc).__name__}): {exc!r}")
                return

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

            # Settle expired paper positions when market changes (non-blocking)
            if self.paper_mode and self._last_paper_ticker and self._last_paper_ticker != ticker:
                old_ticker = self._last_paper_ticker
                asyncio.create_task(self._settle_paper_positions(ticker))
                log_event("INFO", f"Contract transition: {old_ticker} → {ticker} (settlement running in background)")
                # Clear tracking sets for new contract immediately
                self._free_rolled.clear()
                self._took_profit.clear()
                self._edge_exit_ts.clear()
                self._entry_ts.clear()
                self._entry_edge.clear()
                self._edge_exits_count.clear()
                if self.alpha:
                    self.alpha.reset_contract_window()
            if self.paper_mode:
                if self._last_paper_ticker != ticker:
                    self._last_paper_ticker = ticker
                    self._save_paper_state()
                    if self.alpha:
                        self.alpha.reset_contract_window()
            else:
                # In live mode, also clear tracking sets on ticker change
                if self._last_paper_ticker != ticker:
                    old_live_ticker = self._last_paper_ticker
                    self._last_paper_ticker = ticker
                    self._free_rolled.clear()
                    self._took_profit.clear()
                    self._edge_exit_ts.clear()
                    self._entry_ts.clear()
                    self._entry_edge.clear()
                    self._edge_exits_count.clear()
                    if self.alpha:
                        self.alpha.reset_contract_window()
                    # Record settlement for expired live positions (non-blocking)
                    if old_live_ticker:
                        asyncio.create_task(self._settle_live_positions(old_live_ticker))

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
            # Prefer REST API — WS orderbook often goes stale (Kalshi stops sending deltas)
            try:
                ob = await self.fetch_orderbook(ticker)
            except Exception:
                live_ob = self.alpha.get_live_orderbook(ticker) if self.alpha else None
                ob = live_ob if live_ob else {"yes": [], "no": []}
            if self.paper_mode:
                self._paper_orderbook = ob
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
                start_bal = config.PAPER_STARTING_BALANCE
                all_time_pnl = (balance + total_exposure) - start_bal
            else:
                start_bal = self._start_balance + self._start_exposure
                all_time_pnl = settled_pnl
            self.status["day_pnl"] = all_time_pnl + self.status["position_pnl"]

            # Total account value: cash + mark-to-market of all positions
            # mark_to_market is only for active position; other positions use cost basis
            active_mtm = mark_to_market / 100.0 if my_pos else 0.0
            active_cost = (my_pos.get("market_exposure", 0) or 0) / 100.0 if my_pos else 0.0
            other_exposure = total_exposure - active_cost  # cost basis of non-active positions
            self.status["total_account_value"] = balance + active_mtm + other_exposure
            self.status["start_balance"] = start_bal

            # ---- Dashboard data (observational — always computed, no impact on trading) ----
            dashboard = {}
            strike = self.status.get("strike_price")
            has_pos = my_pos is not None and (my_pos.get("position", 0) or 0) != 0

            # Fair value + edge (requires alpha + strike + time)
            if self.alpha and strike and strike > 0 and secs_left > 0:
                try:
                    fv = self.alpha.get_fair_value(strike, secs_left)
                    dashboard["fair_value"] = fv
                    dashboard["yes_edge"] = fv["fair_yes_cents"] - best_ask
                    dashboard["no_edge"] = (100 - fv["fair_yes_cents"]) - (100 - best_bid)
                except Exception:
                    dashboard["fair_value"] = None
                    dashboard["yes_edge"] = 0
                    dashboard["no_edge"] = 0
            else:
                dashboard["fair_value"] = None
                dashboard["yes_edge"] = 0
                dashboard["no_edge"] = 0

            # Time decay factor
            dashboard["time_factor"] = min(1.0, max(0.0, secs_left / 900.0)) if secs_left else 0.0

            # Guard states
            spread_val = best_ask - best_bid
            max_exposure = balance * config.MAX_TOTAL_EXPOSURE_PCT / 100.0
            price_est = best_ask if best_ask < 100 else 50
            position_budget = balance * config.MAX_POSITION_PCT / 100.0
            max_qty = max(1, int(position_budget / (price_est / 100.0))) if price_est > 0 else 1
            current_qty = abs(my_pos.get("position", 0) or 0) if my_pos else 0
            pos_val = (my_pos.get("position", 0) or 0) if my_pos else 0

            dashboard["guards"] = {
                "time": {
                    "blocked": secs_left < config.MIN_SECONDS_TO_CLOSE if secs_left else True,
                    "value": round(secs_left or 0, 0),
                    "threshold": config.MIN_SECONDS_TO_CLOSE,
                },
                "spread": {
                    "blocked": spread_val > config.MAX_SPREAD_CENTS,
                    "value": spread_val,
                    "threshold": config.MAX_SPREAD_CENTS,
                },
                "daily_loss": {
                    "blocked": settled_pnl < -max_daily_loss,
                    "value": round(settled_pnl, 2),
                    "threshold": round(-max_daily_loss, 2),
                },
                "hold_expiry": {
                    "blocked": secs_left < config.HOLD_EXPIRY_SECS if secs_left else False,
                    "value": round(secs_left or 0, 0),
                    "threshold": config.HOLD_EXPIRY_SECS,
                    "has_position": has_pos,
                },
                "price_min": {
                    "blocked": best_ask < config.MIN_CONTRACT_PRICE and (100 - best_bid) < config.MIN_CONTRACT_PRICE,
                    "value_yes": best_ask,
                    "value_no": 100 - best_bid,
                    "threshold": config.MIN_CONTRACT_PRICE,
                },
                "price_max": {
                    "blocked": best_ask > config.MAX_CONTRACT_PRICE and (100 - best_bid) > config.MAX_CONTRACT_PRICE,
                    "value_yes": best_ask,
                    "value_no": 100 - best_bid,
                    "threshold": config.MAX_CONTRACT_PRICE,
                },
                "exposure": {
                    "blocked": total_exposure >= max_exposure,
                    "value": round(total_exposure, 2),
                    "threshold": round(max_exposure, 2),
                },
                "position_size": {
                    "blocked": current_qty >= max_qty,
                    "value": current_qty,
                    "threshold": max_qty,
                },
                "same_side": {
                    "blocked": False,
                    "holding": "YES" if pos_val > 0 else ("NO" if pos_val < 0 else "NONE"),
                },
                "tp_reentry": {
                    "blocked": ticker in self._took_profit if ticker else False,
                },
                "edge_reentry": {
                    "blocked": ticker in self._edge_exit_ts and (time.time() - self._edge_exit_ts.get(ticker, 0)) < config.EDGE_EXIT_COOLDOWN_SECS if ticker else False,
                    "cooldown_left": max(0, config.EDGE_EXIT_COOLDOWN_SECS - (time.time() - self._edge_exit_ts.get(ticker, 0))) if ticker and ticker in self._edge_exit_ts else 0,
                    "premium": config.REENTRY_EDGE_PREMIUM,
                },
            }

            # Exit rule states
            exits = {}
            if has_pos:
                eq = abs(pos_val)
                pe = (my_pos.get("market_exposure", 0) or 0)
                sp = best_bid if pos_val > 0 else (100 - best_ask)
                mtm_e = best_bid * pos_val if pos_val > 0 else (100 - best_ask) * eq if pos_val < 0 else 0
                avg_c = pe / eq if eq > 0 else 0
                loss_p = (pe - mtm_e) / eq if eq > 0 else 0
                gain_p = ((sp - avg_c) / avg_c * 100) if avg_c > 0 else 0

                exits["stop_loss"] = {
                    "triggered": config.STOP_LOSS_CENTS > 0 and loss_p >= config.STOP_LOSS_CENTS,
                    "value": round(loss_p, 1),
                    "threshold": config.STOP_LOSS_CENTS,
                }
                exits["hit_and_run"] = {
                    "triggered": config.HIT_RUN_PCT > 0 and gain_p >= config.HIT_RUN_PCT,
                    "value": round(gain_p, 1),
                    "threshold": config.HIT_RUN_PCT,
                    "enabled": config.HIT_RUN_PCT > 0,
                }
                exits["profit_take"] = {
                    "triggered": gain_p >= config.PROFIT_TAKE_PCT and secs_left > config.PROFIT_TAKE_MIN_SECS,
                    "value": round(gain_p, 1),
                    "threshold": config.PROFIT_TAKE_PCT,
                    "min_secs": config.PROFIT_TAKE_MIN_SECS,
                }
                exits["free_roll"] = {
                    "triggered": sp >= config.FREE_ROLL_PRICE and eq >= 2 and ticker not in self._free_rolled,
                    "value": sp,
                    "threshold": config.FREE_ROLL_PRICE,
                    "qty": eq,
                    "already_rolled": ticker in self._free_rolled,
                }

                # Edge-exit dashboard data
                _edge_remaining = 0
                _edge_threshold = 0
                _edge_hold = time.time() - self._entry_ts.get(ticker, time.time())
                if config.EDGE_EXIT_ENABLED and self.alpha and strike and strike > 0:
                    try:
                        _fv_edge = self.alpha.get_fair_value(strike, secs_left)
                        _fair_yes_edge = _fv_edge.get("fair_yes_cents", 0)
                        if pos_val > 0:
                            _edge_remaining = _fair_yes_edge - best_bid
                        else:
                            _edge_remaining = best_ask - _fair_yes_edge
                        _tf = min(1.0, max(0.0, secs_left / 900.0))
                        _edge_threshold = config.EDGE_EXIT_THRESHOLD_CENTS * _tf
                    except Exception:
                        pass
                exits["edge_exit"] = {
                    "triggered": config.EDGE_EXIT_ENABLED and _edge_remaining <= _edge_threshold and _edge_hold >= config.EDGE_EXIT_MIN_HOLD_SECS,
                    "remaining_edge": round(_edge_remaining, 1),
                    "threshold": round(_edge_threshold, 1),
                    "hold_secs": round(_edge_hold, 0),
                    "min_hold": config.EDGE_EXIT_MIN_HOLD_SECS,
                    "enabled": config.EDGE_EXIT_ENABLED,
                    "count": self._edge_exits_count.get(ticker, 0),
                }
            else:
                exits["stop_loss"] = {"triggered": False, "value": 0, "threshold": config.STOP_LOSS_CENTS}
                exits["hit_and_run"] = {"triggered": False, "value": 0, "threshold": config.HIT_RUN_PCT, "enabled": config.HIT_RUN_PCT > 0}
                exits["profit_take"] = {"triggered": False, "value": 0, "threshold": config.PROFIT_TAKE_PCT, "min_secs": config.PROFIT_TAKE_MIN_SECS}
                exits["free_roll"] = {"triggered": False, "value": 0, "threshold": config.FREE_ROLL_PRICE, "qty": 0, "already_rolled": False}
                exits["edge_exit"] = {"triggered": False, "remaining_edge": 0, "threshold": 0, "hold_secs": 0, "min_hold": config.EDGE_EXIT_MIN_HOLD_SECS, "enabled": config.EDGE_EXIT_ENABLED, "count": self._edge_exits_count.get(ticker, 0) if ticker else 0}
            dashboard["exits"] = exits

            # Config thresholds for frontend display
            dashboard["lead_lag_enabled"] = config.LEAD_LAG_ENABLED
            dashboard["lead_lag_threshold"] = config.LEAD_LAG_THRESHOLD
            dashboard["delta_threshold"] = config.DELTA_THRESHOLD
            dashboard["extreme_delta_threshold"] = config.EXTREME_DELTA_THRESHOLD
            dashboard["anchor_seconds_threshold"] = config.ANCHOR_SECONDS_THRESHOLD
            dashboard["min_edge_cents"] = config.MIN_EDGE_CENTS
            dashboard["min_confidence"] = config.RULE_MIN_CONFIDENCE
            dashboard["edge_exit_enabled"] = config.EDGE_EXIT_ENABLED
            dashboard["edge_exit_threshold"] = config.EDGE_EXIT_THRESHOLD_CENTS
            dashboard["edge_exit_cooldown"] = config.EDGE_EXIT_COOLDOWN_SECS
            dashboard["reentry_edge_premium"] = config.REENTRY_EDGE_PREMIUM

            self.status["dashboard"] = dashboard

            # Build snapshot context dict for trade recording (used by all trade paths below)
            _snap_ctx = {}
            if self.alpha:
                _gwp = self.alpha.get_weighted_global_price()
                _vol = self.alpha.get_volatility()
                _vel = self.alpha.get_price_velocity()
                _fv_data = dashboard.get("fair_value") or {}
                _snap_ctx = {
                    "btc_price": _gwp,
                    "strike_price": strike,
                    "btc_vs_strike": _fv_data.get("btc_vs_strike", 0),
                    "secs_left": secs_left,
                    "time_factor": dashboard.get("time_factor", 0),
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": best_ask - best_bid,
                    "fair_yes_cents": _fv_data.get("fair_yes_cents", 0),
                    "fair_yes_prob": _fv_data.get("fair_yes_prob", 0),
                    "yes_edge": dashboard.get("yes_edge", 0),
                    "no_edge": dashboard.get("no_edge", 0),
                    "vol_dollar_per_min": _vol.get("vol_dollar_per_min", 0),
                    "vol_regime": _vol.get("regime", ""),
                    "delta_momentum": self.alpha.delta_momentum,
                    "velocity_1m": _vel.get("velocity_1m", 0),
                    "direction_1m": _vel.get("direction_1m", 0),
                    "price_change_1m": _vel.get("price_change_1m", 0),
                    "balance": balance,
                    "exposure": total_exposure,
                }

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
                            order = await self.close_position(ticker, sell_side, sell_price, sell_qty, exit_type="SL")
                            if order:
                                self.status["last_action"] = f"SL: sold {sell_qty}x {sell_side.upper()} @ {sell_price}c"
                                try:
                                    _mid = f"[PAPER] {ticker}" if self.paper_mode else ticker
                                    _entry = get_entry_snapshot(_mid)
                                    _entry_ts = datetime.fromisoformat(_entry["ts"]).timestamp() if _entry else None
                                    record_snapshot({
                                        "ts": datetime.now(timezone.utc).isoformat(),
                                        "trade_id": order.get("order_id", f"snap-{int(time.time()*1000)}"),
                                        "market_id": _mid, "action": "SL", "side": sell_side,
                                        "price_cents": sell_price, "quantity": sell_qty,
                                        "decision": "SL", "confidence": 0, "trigger_type": "stop_loss",
                                        "position_qty": abs(pos_qty),
                                        "pnl_cents": round(-loss_per_contract * sell_qty, 1),
                                        "hold_duration_s": round(time.time() - _entry_ts, 1) if _entry_ts else None,
                                        "entry_price_cents": _entry["price_cents"] if _entry else None,
                                        **_snap_ctx,
                                    })
                                except Exception:
                                    pass
                            else:
                                self.status["last_action"] = "SL: sell order rejected"
                            return

                    # Rule: Edge-exit — exit when remaining edge evaporates (time-scaled)
                    if config.EDGE_EXIT_ENABLED and self.alpha and strike and strike > 0:
                        try:
                            fv = self.alpha.get_fair_value(strike, secs_left)
                            fair_yes = fv.get("fair_yes_cents", 0)
                            if pos_qty > 0:
                                remaining_edge = fair_yes - best_bid
                            else:
                                remaining_edge = best_ask - fair_yes

                            time_factor = min(1.0, max(0.0, secs_left / 900.0))
                            edge_threshold = config.EDGE_EXIT_THRESHOLD_CENTS * time_factor
                            hold_elapsed = time.time() - self._entry_ts.get(ticker, 0)

                            if (remaining_edge <= edge_threshold
                                    and hold_elapsed >= config.EDGE_EXIT_MIN_HOLD_SECS):
                                sell_qty = abs(pos_qty)
                                log_event("TRADE", f"Edge-exit: remaining edge {remaining_edge:.1f}c <= threshold {edge_threshold:.1f}c (held {hold_elapsed:.0f}s)")
                                order = await self.close_position(ticker, sell_side, sell_price, sell_qty, exit_type="EDGE")
                                if order:
                                    self.status["last_action"] = f"EDGE EXIT: sold {sell_qty}x {sell_side.upper()} @ {sell_price}c (edge {remaining_edge:.1f}c)"
                                    self._edge_exit_ts[ticker] = time.time()
                                    self._edge_exits_count[ticker] = self._edge_exits_count.get(ticker, 0) + 1
                                    # Clear entry tracking (position is closed)
                                    self._entry_ts.pop(ticker, None)
                                    self._entry_edge.pop(ticker, None)
                                    try:
                                        _mid = f"[PAPER] {ticker}" if self.paper_mode else ticker
                                        _entry = get_entry_snapshot(_mid)
                                        _entry_ts_snap = datetime.fromisoformat(_entry["ts"]).timestamp() if _entry else None
                                        avg_cost_edge = pos_exposure_cents / abs(pos_qty) if abs(pos_qty) > 0 else 0
                                        _pnl = round((sell_price - avg_cost_edge) * sell_qty, 1) if avg_cost_edge else 0
                                        record_snapshot({
                                            "ts": datetime.now(timezone.utc).isoformat(),
                                            "trade_id": order.get("order_id", f"snap-{int(time.time()*1000)}"),
                                            "market_id": _mid, "action": "EDGE", "side": sell_side,
                                            "price_cents": sell_price, "quantity": sell_qty,
                                            "decision": "EDGE", "confidence": 0, "trigger_type": "edge_exit",
                                            "position_qty": abs(pos_qty),
                                            "pnl_cents": _pnl,
                                            "hold_duration_s": round(time.time() - _entry_ts_snap, 1) if _entry_ts_snap else None,
                                            "entry_price_cents": _entry["price_cents"] if _entry else None,
                                            **_snap_ctx,
                                        })
                                    except Exception:
                                        pass
                                else:
                                    self.status["last_action"] = "Edge exit: sell order rejected"
                                return
                        except Exception:
                            pass  # Fair value unavailable — skip edge-exit

                    # Calculate profit for all profit-taking rules
                    avg_cost = pos_exposure_cents / abs(pos_qty) if abs(pos_qty) > 0 else 0
                    gain_pct = ((current_value - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0

                    # Rule: Hit-and-Run — instant exit at % profit (NO time restrictions)
                    if config.HIT_RUN_PCT > 0 and gain_pct >= config.HIT_RUN_PCT:
                        sell_qty = abs(pos_qty)
                        log_event("TRADE", f"Hit-and-run: +{gain_pct:.0f}% gain ({current_value}c vs {avg_cost:.0f}c cost) >= {config.HIT_RUN_PCT}% target — instant exit")
                        order = await self.close_position(ticker, sell_side, sell_price, sell_qty, exit_type="TP")
                        if order:
                            self.status["last_action"] = f"HIT&RUN: sold {sell_qty}x {sell_side.upper()} @ {sell_price}c (+{gain_pct:.0f}%)"
                            self._took_profit.add(ticker)
                            try:
                                _mid = f"[PAPER] {ticker}" if self.paper_mode else ticker
                                _entry = get_entry_snapshot(_mid)
                                _entry_ts = datetime.fromisoformat(_entry["ts"]).timestamp() if _entry else None
                                _pnl = round((sell_price - avg_cost) * sell_qty, 1) if avg_cost else 0
                                record_snapshot({
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                    "trade_id": order.get("order_id", f"snap-{int(time.time()*1000)}"),
                                    "market_id": _mid, "action": "TP", "side": sell_side,
                                    "price_cents": sell_price, "quantity": sell_qty,
                                    "decision": "TP", "confidence": 0, "trigger_type": "hit_and_run",
                                    "position_qty": abs(pos_qty),
                                    "pnl_cents": _pnl,
                                    "hold_duration_s": round(time.time() - _entry_ts, 1) if _entry_ts else None,
                                    "entry_price_cents": _entry["price_cents"] if _entry else None,
                                    **_snap_ctx,
                                })
                            except Exception:
                                pass
                        else:
                            self.status["last_action"] = "Hit&Run: sell order rejected"
                        return

                    # Rule: Pop-and-Drop — full exit at % profit with time remaining
                    if gain_pct >= config.PROFIT_TAKE_PCT and secs_left > config.PROFIT_TAKE_MIN_SECS:
                        sell_qty = abs(pos_qty)
                        log_event("TRADE", f"Profit take: +{gain_pct:.0f}% gain ({current_value}c vs {avg_cost:.0f}c cost) >= {config.PROFIT_TAKE_PCT}% target, {secs_left:.0f}s left — selling all")
                        order = await self.close_position(ticker, sell_side, sell_price, sell_qty, exit_type="TP")
                        if order:
                            self.status["last_action"] = f"TP: sold {sell_qty}x {sell_side.upper()} @ {sell_price}c (+{gain_pct:.0f}%)"
                            self._took_profit.add(ticker)
                            try:
                                _mid = f"[PAPER] {ticker}" if self.paper_mode else ticker
                                _entry = get_entry_snapshot(_mid)
                                _entry_ts = datetime.fromisoformat(_entry["ts"]).timestamp() if _entry else None
                                _pnl = round((sell_price - avg_cost) * sell_qty, 1) if avg_cost else 0
                                record_snapshot({
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                    "trade_id": order.get("order_id", f"snap-{int(time.time()*1000)}"),
                                    "market_id": _mid, "action": "TP", "side": sell_side,
                                    "price_cents": sell_price, "quantity": sell_qty,
                                    "decision": "TP", "confidence": 0, "trigger_type": "profit_take",
                                    "position_qty": abs(pos_qty),
                                    "pnl_cents": _pnl,
                                    "hold_duration_s": round(time.time() - _entry_ts, 1) if _entry_ts else None,
                                    "entry_price_cents": _entry["price_cents"] if _entry else None,
                                    **_snap_ctx,
                                })
                            except Exception:
                                pass
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
                            try:
                                _mid = f"[PAPER] {ticker}" if self.paper_mode else ticker
                                _entry = get_entry_snapshot(_mid)
                                _entry_ts = datetime.fromisoformat(_entry["ts"]).timestamp() if _entry else None
                                _pnl = round((sell_price - avg_cost) * half_qty, 1) if avg_cost else 0
                                record_snapshot({
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                    "trade_id": order.get("order_id", f"snap-{int(time.time()*1000)}"),
                                    "market_id": _mid, "action": "SELL", "side": sell_side,
                                    "price_cents": sell_price, "quantity": half_qty,
                                    "decision": "SELL", "confidence": 0, "trigger_type": "free_roll",
                                    "position_qty": abs(pos_qty),
                                    "pnl_cents": _pnl,
                                    "hold_duration_s": round(time.time() - _entry_ts, 1) if _entry_ts else None,
                                    "entry_price_cents": _entry["price_cents"] if _entry else None,
                                    **_snap_ctx,
                                })
                            except Exception:
                                pass
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
                "strike_price": self.status.get("strike_price", 0),
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
            _trigger_type = "rules"
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
                # Only overrides if actual edge exists (Kalshi hasn't caught up).
                strike = self._extract_strike(market)
                if config.LEAD_LAG_ENABLED and strike and strike > 0:
                    signal, diff = self.alpha.get_signal(strike)
                    self.status["alpha_signal"] = signal
                    self.status["alpha_signal_diff"] = diff
                    yes_edge = dashboard.get("yes_edge", 0)
                    no_edge = dashboard.get("no_edge", 0)
                    min_edge = config.MIN_EDGE_CENTS
                    if signal == "BULLISH" and yes_edge >= min_edge:
                        alpha_override = "BUY_YES"
                        _trigger_type = "lead_lag"
                        log_event("ALPHA", f"Lead-lag BUY_YES: global ${self.alpha.get_weighted_global_price():.2f} > strike ${strike:.2f} by ${diff:.2f} (edge {yes_edge}c)")
                    elif signal == "BEARISH" and no_edge >= min_edge:
                        alpha_override = "BUY_NO"
                        _trigger_type = "lead_lag"
                        log_event("ALPHA", f"Lead-lag BUY_NO: global ${self.alpha.get_weighted_global_price():.2f} < strike ${strike:.2f} by ${abs(diff):.2f} (edge {no_edge}c)")
                    elif signal != "NEUTRAL":
                        log_event("ALPHA", f"Lead-lag {signal} but no edge (YES:{yes_edge}c NO:{no_edge}c < {min_edge}c) — deferring to rules")

                # Override 1: Front-Run (delta momentum — deviation from rolling baseline)
                # Only fires if lead-lag didn't already trigger, and only with edge
                if not alpha_override:
                    yes_edge = dashboard.get("yes_edge", 0)
                    no_edge = dashboard.get("no_edge", 0)
                    min_edge = config.MIN_EDGE_CENTS
                    if momentum > config.DELTA_THRESHOLD and yes_edge >= min_edge:
                        alpha_override = "BUY_YES"
                        _trigger_type = "momentum"
                        log_event("ALPHA", f"Front-run BUY_YES: momentum={momentum:+.2f} > {config.DELTA_THRESHOLD} (edge {yes_edge}c)")
                    elif momentum < -config.DELTA_THRESHOLD and no_edge >= min_edge:
                        alpha_override = "BUY_NO"
                        _trigger_type = "momentum"
                        log_event("ALPHA", f"Front-run BUY_NO: momentum={momentum:+.2f} < -{config.DELTA_THRESHOLD} (edge {no_edge}c)")

                # Override 2: Anchor Defense (near expiry + holding position)
                if secs_left < config.ANCHOR_SECONDS_THRESHOLD and my_pos:
                    if strike and strike > 0:
                        projection_wins = self.alpha.get_settlement_projection(strike, secs_left)
                        pos_val = my_pos.get("position", 0) or 0
                        yes_qty = pos_val if pos_val > 0 else 0
                        no_qty = abs(pos_val) if pos_val < 0 else 0
                        if yes_qty and not projection_wins:
                            alpha_override = "BUY_NO"
                            _trigger_type = "anchor"
                            log_event("ALPHA", f"Anchor defense: proj {self.alpha.projected_settlement:.2f} < strike {strike}, forcing BUY_NO")
                        elif no_qty and projection_wins:
                            alpha_override = "BUY_YES"
                            _trigger_type = "anchor"
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
                    decision = self.agent.analyze_market(market_data, my_pos, alpha_monitor=self.alpha)
                self.status["last_decision"] = decision
                return

            # 7. Alpha override or rule-based decision
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
                decision = self.agent.analyze_market(market_data, my_pos, alpha_monitor=self.alpha)
                self.status["last_decision"] = decision

                action = decision["decision"]
                confidence = decision["confidence"]

                if action == "HOLD" or confidence < config.RULE_MIN_CONFIDENCE:
                    self.status["last_action"] = f"Rules: {action} ({confidence:.0%})"
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

            # Take-profit guard: never re-enter a contract after we've taken profit
            if ticker in self._took_profit:
                log_event("GUARD", f"TP guard: already took profit on {ticker} — no re-entry")
                self.status["last_action"] = "Already took profit — no re-entry"
                return

            # Edge-exit re-entry guard: cooldown + premium edge required
            if ticker in self._edge_exit_ts:
                cooldown_elapsed = time.time() - self._edge_exit_ts[ticker]
                if cooldown_elapsed < config.EDGE_EXIT_COOLDOWN_SECS:
                    remaining_cd = config.EDGE_EXIT_COOLDOWN_SECS - cooldown_elapsed
                    log_event("GUARD", f"Edge re-entry cooldown: {remaining_cd:.0f}s remaining")
                    self.status["last_action"] = f"Edge re-entry cooldown ({remaining_cd:.0f}s left)"
                    return
                # After cooldown, require extra edge premium for re-entry
                entry_side_edge = dashboard.get("yes_edge", 0) if side == "yes" else dashboard.get("no_edge", 0)
                required_edge = config.MIN_EDGE_CENTS + config.REENTRY_EDGE_PREMIUM
                if entry_side_edge < required_edge:
                    log_event("GUARD", f"Edge re-entry premium: edge {entry_side_edge}c < required {required_edge}c (MIN_EDGE {config.MIN_EDGE_CENTS} + premium {config.REENTRY_EDGE_PREMIUM})")
                    self.status["last_action"] = f"Insufficient edge for re-entry ({entry_side_edge}c < {required_edge}c)"
                    return

            # Entry pricing strategy based on signal urgency:
            # - Extreme momentum: cross spread immediately (market-take)
            # - Alpha override (lead-lag/momentum/anchor): cross spread (time-sensitive edge)
            # - Rule-based: midpoint of spread (balanced fill vs. price improvement)
            extreme_momentum = (
                self.alpha
                and abs(self.alpha.delta_momentum) > config.EXTREME_DELTA_THRESHOLD
            )
            if extreme_momentum and best_ask < 100 and best_bid > 0:
                # Cross the spread — hit the ask (YES) or bid (NO)
                price_cents = best_ask if side == "yes" else (100 - best_bid)
                log_event("ALPHA", f"Extreme momentum ({self.alpha.delta_momentum:+.2f}) — crossing spread at {price_cents}c")
            elif alpha_override and best_ask < 100 and best_bid > 0:
                # Alpha signals are time-sensitive — cross the spread to ensure fill
                price_cents = best_ask if side == "yes" else (100 - best_bid)
                log_event("ALPHA", f"Alpha override — crossing spread at {price_cents}c")
            elif best_ask < 100 and best_bid > 0:
                if self.paper_mode:
                    # Paper mode: cross spread to get realistic fills (paper can't simulate resting orders)
                    price_cents = best_ask if side == "yes" else (100 - best_bid)
                else:
                    # Live: start at midpoint for faster fills with some price improvement
                    if side == "yes":
                        price_cents = (best_bid + best_ask + 1) // 2
                    else:
                        no_bid = 100 - best_ask
                        no_ask = 100 - best_bid
                        price_cents = (no_bid + no_ask + 1) // 2
                    price_cents = max(1, min(99, price_cents))
            else:
                # One-sided market fallback
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
                # Record entry time and edge for edge-exit logic
                self._entry_ts[ticker] = time.time()
                entry_edge = dashboard.get("yes_edge", 0) if side == "yes" else dashboard.get("no_edge", 0)
                self._entry_edge[ticker] = entry_edge
                try:
                    _mid = f"[PAPER] {ticker}" if self.paper_mode else ticker
                    record_snapshot({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "trade_id": order.get("order_id", f"snap-{int(time.time()*1000)}"),
                        "market_id": _mid, "action": "BUY", "side": side,
                        "price_cents": price_cents,
                        "quantity": order.get("filled_count", qty),
                        "decision": action, "confidence": confidence,
                        "trigger_type": _trigger_type,
                        "position_qty": current_qty,
                        **_snap_ctx,
                    })
                except Exception:
                    pass
                # Fill-or-cancel: skip retries for spread-crossing orders (already at best price)
                if not extreme_momentum and not alpha_override:
                    order_id = order.get("order_id")
                    if order_id:
                        await self._wait_and_retry(ticker, order_id, side, price_cents, qty, initial_order=order)
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
