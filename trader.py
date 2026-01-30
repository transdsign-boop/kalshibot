import asyncio
import base64
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


def _load_private_key(env: str | None = None):
    """Load the RSA private key for the given (or current) environment."""
    import os
    env = env or config.KALSHI_ENV

    # Fly.io: check inline env var first (per-environment, then generic fallback for live only)
    inline_var = f"KALSHI_{env.upper()}_PRIVATE_KEY"
    raw = os.getenv(inline_var)
    if not raw and env == "live":
        raw = os.getenv("KALSHI_PRIVATE_KEY")
    if raw:
        return serialization.load_pem_private_key(raw.encode(), password=None)

    path = (config.KALSHI_LIVE_PRIVATE_KEY_PATH if env == "live"
            else config.KALSHI_DEMO_PRIVATE_KEY_PATH)
    with open(path, "rb") as f:
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
        }

    @property
    def base_host(self) -> str:
        return config.HOSTS.get(config.KALSHI_ENV, config.HOSTS["demo"])

    async def switch_environment(self, env: str):
        """Switch between 'demo' and 'live'. Stops the bot, swaps creds, resets client."""
        was_running = self.running
        if was_running:
            self.stop()
            # Give the loop a moment to exit
            await asyncio.sleep(1)

        config.switch_env(env)
        self.private_key = _load_private_key(env)
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
        log_event("INFO", f"Switched to {env.upper()} environment")

    # ------------------------------------------------------------------
    # HTTP helpers (Kalshi REST API via httpx + RSA-PSS auth)
    # ------------------------------------------------------------------

    async def _ensure_client(self):
        if self.http is None or self.http.is_closed:
            self.http = httpx.AsyncClient(
                base_url=self.base_host,
                timeout=15.0,
            )

    def _full_path(self, path: str) -> str:
        """Prepend the API prefix to a relative path."""
        return f"{self.PATH_PREFIX}{path}"

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._ensure_client()
        full = self._full_path(path)
        headers = _sign_request(self.private_key, "GET", full)
        resp = await self.http.get(full, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, body: dict) -> dict:
        await self._ensure_client()
        full = self._full_path(path)
        headers = _sign_request(self.private_key, "POST", full)
        resp = await self.http.post(full, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> dict:
        await self._ensure_client()
        full = self._full_path(path)
        headers = _sign_request(self.private_key, "DELETE", full)
        resp = await self.http.delete(full, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Kalshi API wrappers
    # ------------------------------------------------------------------

    async def fetch_balance(self) -> float:
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
        data = await self._get("/portfolio/positions", params={"limit": 20})
        return data.get("market_positions", [])

    async def cancel_all_orders(self):
        try:
            await _safe(self._post("/portfolio/orders/batched", {"action": "cancel_all"}))
        except Exception:
            # Endpoint may not exist in demo; swallow
            pass

    async def place_order(
        self, ticker: str, side: str, price_cents: int, quantity: int
    ) -> dict | None:
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

    async def close_position(
        self, ticker: str, side: str, price_cents: int, quantity: int
    ) -> dict | None:
        """Sell an existing position at the given price."""
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
        """Extract the strike price from a KXBTC15M market.

        Tries structured fields first (floor_strike / strike_price),
        then falls back to parsing from the title, e.g.
        'BTC above $95,000 at 12:15 PM ET'.
        """
        strike = market.get("floor_strike") or market.get("strike_price")
        if strike:
            try:
                return float(strike) / 100.0  # Kalshi often uses cents
            except (ValueError, TypeError):
                pass

        title = market.get("title", "")
        match = re.search(r'\$([0-9,]+)', title)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                pass
        return None

    async def _wait_and_retry(self, ticker: str, order_id: str, side: str,
                               price_cents: int, qty: int):
        """Wait 3s for a fill; cancel and retry 1c more aggressive if unfilled."""
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
                log_event("INFO", "No active KXBTC15M market found")
                return

            ticker = market.get("ticker", "")
            self.status["current_market"] = ticker

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
            self.status["day_pnl"] = settled_pnl

            # Daily loss circuit breaker (percentage of starting balance)
            max_daily_loss = self._start_balance * config.MAX_DAILY_LOSS_PCT / 100.0
            if settled_pnl < -max_daily_loss:
                log_event("GUARD", f"Daily loss guard: ${settled_pnl:.2f} exceeds -{config.MAX_DAILY_LOSS_PCT:.1f}% (${max_daily_loss:.2f}) limit")
                self.status["last_action"] = f"Daily loss limit hit (${settled_pnl:.2f})"
                return

            # 4. Time guard
            if not self._time_guard(market):
                self.status["last_action"] = "Time guard — sleeping"
                # Cancel open orders near expiry
                await self.cancel_all_orders()
                return

            # 5. Orderbook + spread guard
            ob = await self.fetch_orderbook(ticker)
            spread_ok, best_bid, best_ask = self._spread_guard(ob)

            # Store orderbook snapshot for dashboard (sorted best-first)
            yes_orders = ob.get("yes", []) if isinstance(ob.get("yes"), list) else []
            no_orders = ob.get("no", []) if isinstance(ob.get("no"), list) else []
            yes_sorted = sorted(yes_orders, key=lambda x: x[0], reverse=True)
            no_sorted = sorted(no_orders, key=lambda x: x[0], reverse=True)
            self.status["orderbook"] = {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": best_ask - best_bid,
                "yes_levels": yes_sorted[:5],   # top 5 YES bid levels (best first)
                "no_levels": no_sorted[:5],      # top 5 NO bid levels (best first)
                "yes_depth": sum(q for _, q in yes_orders),
                "no_depth": sum(q for _, q in no_orders),
            }

            # Unrealized position P&L (mark-to-market vs cost)
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

                # Stop-loss check: exit if loss per contract exceeds threshold
                if config.STOP_LOSS_CENTS > 0 and abs(pos_qty) > 0 and config.TRADING_ENABLED:
                    loss_per_contract = (pos_exposure_cents - mark_to_market) / abs(pos_qty)
                    if loss_per_contract >= config.STOP_LOSS_CENTS:
                        sell_side = "yes" if pos_qty > 0 else "no"
                        sell_price = best_bid if pos_qty > 0 else (100 - best_ask)
                        sell_price = max(1, min(99, sell_price))
                        sell_qty = abs(pos_qty)
                        log_event("GUARD", f"Stop-loss triggered: down {loss_per_contract:.0f}c/contract (limit {config.STOP_LOSS_CENTS}c)")
                        order = await self.close_position(ticker, sell_side, sell_price, sell_qty)
                        if order:
                            self.status["last_action"] = f"SL: sold {sell_qty}x {sell_side.upper()} @ {sell_price}c"
                        else:
                            self.status["last_action"] = "SL: sell order rejected"
                        return
            else:
                self.status["position_pnl"] = 0.0

            if not spread_ok:
                self.status["last_action"] = "Spread too wide — holding"
                return

            # 6. Build data payload for the agent
            market_data = {
                "ticker": ticker,
                "title": market.get("title", ""),
                "seconds_to_close": market.get("_seconds_to_close", 0),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": best_ask - best_bid,
                "last_price": market.get("last_price", 0),
                "volume": market.get("volume", 0),
            }

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

                # Override 1: Front-Run (delta momentum — deviation from rolling baseline)
                # Positive momentum = Binance leading up (bullish), Negative = leading down (bearish)
                if momentum > config.DELTA_THRESHOLD:
                    alpha_override = "BUY_YES"
                    log_event("ALPHA", f"Front-run BUY_YES: momentum={momentum:+.2f} > {config.DELTA_THRESHOLD}")
                elif momentum < -config.DELTA_THRESHOLD:
                    alpha_override = "BUY_NO"
                    log_event("ALPHA", f"Front-run BUY_NO: momentum={momentum:+.2f} < -{config.DELTA_THRESHOLD}")

                # Override 2: Anchor Defense (near expiry + holding position)
                if secs_left < config.ANCHOR_SECONDS_THRESHOLD and my_pos:
                    strike = self._extract_strike(market)
                    if strike and strike > 0:
                        projection_wins = self.alpha.get_settlement_projection(strike, secs_left)
                        yes_qty = my_pos.get("yes_quantity", my_pos.get("position", 0))
                        no_qty = my_pos.get("no_quantity", 0)
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

            # 8. Execute
            side = "yes" if action == "BUY_YES" else "no"

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

            # Respect min price guard
            effective_price = price_cents if side == "yes" else (100 - price_cents)
            if effective_price < config.MIN_CONTRACT_PRICE:
                log_event("GUARD", f"Price guard: {effective_price}c < {config.MIN_CONTRACT_PRICE}c min")
                self.status["last_action"] = "Price too cheap — holding"
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

            # Check current position to avoid exceeding max
            current_qty = 0
            if my_pos:
                current_qty = (my_pos.get("yes_quantity", 0) or 0) + (my_pos.get("no_quantity", 0) or 0)
                if current_qty < 0:
                    current_qty = my_pos.get("position", 0) or 0
                    current_qty = abs(current_qty)
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
