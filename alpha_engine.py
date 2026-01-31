"""
Alpha Engine — real-time price feeds and settlement projection.

Maintains dual WebSocket connections:
  - Binance Futures (wss://fstream.binance.com/ws/btcusdt@trade) as the Lead
  - Coinbase (wss://ws-feed.exchange.coinbase.com) for BTC-USD as the Lag/Index

Exposes:
  - latency_delta: binance_price - coinbase_price
  - get_settlement_projection(strike_price, seconds_remaining) -> bool
  - binance_price, coinbase_price: latest raw prices
"""

import asyncio
import base64
import json
import random
import time
from datetime import datetime, timezone

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config
from database import log_event


class AlphaMonitor:
    """Long-lived async service that tracks cross-exchange BTC prices."""

    BINANCE_WS_URL = "wss://fstream.binance.com/ws/btcusdt@trade"
    COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"

    RECONNECT_BASE_DELAY = 1.0
    RECONNECT_MAX_DELAY = 30.0
    RECONNECT_JITTER = 0.5

    DELTA_WINDOW_SECONDS = 60  # rolling window for baseline delta

    def __init__(self):
        self.binance_price: float = 0.0
        self.coinbase_price: float = 0.0
        self.latency_delta: float = 0.0

        # Momentum tracking: filters out structural futures premium
        self._delta_history: list[tuple[float, float]] = []  # (timestamp, delta)
        self.delta_baseline: float = 0.0   # rolling average delta over window
        self.delta_momentum: float = 0.0   # current_delta - baseline (the trading signal)

        self.binance_connected: bool = False
        self.coinbase_connected: bool = False

        # Rolling prices for current minute (list of (timestamp, price))
        self._minute_prices: list[tuple[float, float]] = []
        self._current_minute: int = -1

        self.projected_settlement: float = 0.0

        # Kalshi real-time data (fed by WS ticker + orderbook_delta channels)
        self.kalshi_connected: bool = False
        self.kalshi_ticker: dict[str, dict] = {}   # ticker -> {yes_bid, yes_ask, volume, ...}
        self.kalshi_orderbook: dict[str, dict] = {} # ticker -> {yes: [[p,q],...], no: [[p,q],...]}
        self.kalshi_fills: list[dict] = []          # recent fills (last 50)
        self._kalshi_subscribed_ob: set[str] = set()  # tickers with active OB subscriptions
        self._kalshi_ws = None                      # reference to live WS for sending commands

        self._tasks: list[asyncio.Task] = []
        self._running: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Launch all WebSocket listeners as background tasks."""
        if self._running:
            return
        self._running = True
        log_event("ALPHA", "Alpha Engine starting — connecting to Binance + Coinbase + Kalshi")
        self._tasks = [
            asyncio.create_task(self._binance_loop(), name="alpha-binance"),
            asyncio.create_task(self._coinbase_loop(), name="alpha-coinbase"),
            asyncio.create_task(self._kalshi_loop(), name="alpha-kalshi"),
        ]

    async def stop(self):
        """Cancel all background tasks."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.binance_connected = False
        self.coinbase_connected = False
        self.kalshi_connected = False
        self._kalshi_ws = None
        log_event("ALPHA", "Alpha Engine stopped")

    # ------------------------------------------------------------------
    # Binance Futures WebSocket
    # ------------------------------------------------------------------

    async def _binance_loop(self):
        delay = self.RECONNECT_BASE_DELAY
        while self._running:
            try:
                async with websockets.connect(
                    self.BINANCE_WS_URL,
                    ping_interval=60,
                    ping_timeout=30,
                    close_timeout=10,
                ) as ws:
                    self.binance_connected = True
                    delay = self.RECONNECT_BASE_DELAY
                    log_event("ALPHA", "Binance WS connected")

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw_msg)
                            price = float(data.get("p", 0))
                            if price > 0:
                                self.binance_price = price
                                self._update_delta()
                        except (json.JSONDecodeError, ValueError, KeyError):
                            pass

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.binance_connected = False
                log_event("ALPHA", f"Binance WS error: {exc} — reconnecting in {delay:.1f}s")
                jitter = delay * self.RECONNECT_JITTER * random.random()
                await asyncio.sleep(delay + jitter)
                delay = min(delay * 2, self.RECONNECT_MAX_DELAY)

        self.binance_connected = False

    # ------------------------------------------------------------------
    # Coinbase WebSocket
    # ------------------------------------------------------------------

    async def _coinbase_loop(self):
        delay = self.RECONNECT_BASE_DELAY
        while self._running:
            try:
                async with websockets.connect(
                    self.COINBASE_WS_URL,
                    ping_interval=60,
                    ping_timeout=30,
                    close_timeout=10,
                ) as ws:
                    subscribe_msg = json.dumps({
                        "type": "subscribe",
                        "product_ids": ["BTC-USD"],
                        "channels": ["ticker"],
                    })
                    await ws.send(subscribe_msg)

                    self.coinbase_connected = True
                    delay = self.RECONNECT_BASE_DELAY
                    log_event("ALPHA", "Coinbase WS connected")

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw_msg)
                            if data.get("type") != "ticker":
                                continue
                            price = float(data.get("price", 0))
                            if price > 0:
                                self.coinbase_price = price
                                self._record_minute_price(price)
                                self._update_delta()
                        except (json.JSONDecodeError, ValueError, KeyError):
                            pass

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.coinbase_connected = False
                log_event("ALPHA", f"Coinbase WS error: {exc} — reconnecting in {delay:.1f}s")
                jitter = delay * self.RECONNECT_JITTER * random.random()
                await asyncio.sleep(delay + jitter)
                delay = min(delay * 2, self.RECONNECT_MAX_DELAY)

        self.coinbase_connected = False

    # ------------------------------------------------------------------
    # Kalshi WebSocket (real-time ticker + orderbook + fills)
    # ------------------------------------------------------------------

    def _kalshi_ws_url(self) -> str:
        return config.KALSHI_HOST.replace("https://", "wss://") + "/trade-api/ws/v2"

    def _kalshi_auth_headers(self) -> dict:
        """Build RSA-PSS auth headers for Kalshi WS handshake."""
        import os

        # Always use live credentials
        raw = os.getenv("KALSHI_LIVE_PRIVATE_KEY") or os.getenv("KALSHI_PRIVATE_KEY")
        if raw:
            private_key = serialization.load_pem_private_key(raw.encode(), password=None)
        else:
            with open(config.KALSHI_LIVE_PRIVATE_KEY_PATH, "rb") as f:
                private_key = serialization.load_pem_private_key(f.read(), password=None)

        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}GET/trade-api/ws/v2".encode("utf-8")

        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        }

    async def _kalshi_loop(self):
        delay = self.RECONNECT_BASE_DELAY
        while self._running:
            try:
                headers = self._kalshi_auth_headers()
                async with websockets.connect(
                    self._kalshi_ws_url(),
                    additional_headers=headers,
                    ping_interval=None,  # Kalshi sends pings every 10s, library auto-pongs
                    close_timeout=10,
                ) as ws:
                    self._kalshi_ws = ws
                    self.kalshi_connected = True
                    self._kalshi_subscribed_ob = set()
                    delay = self.RECONNECT_BASE_DELAY
                    log_event("ALPHA", "Kalshi WS connected")

                    # Subscribe to ticker (all markets) + fill (all our fills)
                    await ws.send(json.dumps({
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {"channels": ["ticker", "fill"]},
                    }))

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            payload = json.loads(raw_msg)
                            msg_type = payload.get("type", "")
                            msg = payload.get("msg", {})

                            if msg_type == "ticker":
                                ticker = msg.get("market_ticker", "")
                                if ticker:
                                    self.kalshi_ticker[ticker] = msg

                            elif msg_type == "orderbook_snapshot":
                                ticker = msg.get("market_ticker", "")
                                if ticker:
                                    self.kalshi_orderbook[ticker] = {
                                        "yes": msg.get("yes", []),
                                        "no": msg.get("no", []),
                                    }

                            elif msg_type == "orderbook_delta":
                                ticker = msg.get("market_ticker", "")
                                if ticker and ticker in self.kalshi_orderbook:
                                    # Apply delta: replace price levels
                                    for side in ("yes", "no"):
                                        deltas = msg.get(side, [])
                                        if not deltas:
                                            continue
                                        book = self.kalshi_orderbook[ticker].get(side, [])
                                        book_dict = {p: q for p, q in book}
                                        for p, q in deltas:
                                            if q == 0:
                                                book_dict.pop(p, None)
                                            else:
                                                book_dict[p] = q
                                        self.kalshi_orderbook[ticker][side] = [
                                            [p, q] for p, q in book_dict.items()
                                        ]

                            elif msg_type == "fill":
                                self.kalshi_fills.append(msg)
                                self.kalshi_fills = self.kalshi_fills[-50:]
                                log_event("TRADE", f"WS fill: {msg.get('side','')} {msg.get('count',0)}x @ {msg.get('yes_price', msg.get('no_price','?'))}c on {msg.get('ticker','')}")

                        except (json.JSONDecodeError, ValueError, KeyError):
                            pass

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.kalshi_connected = False
                self._kalshi_ws = None
                log_event("ALPHA", f"Kalshi WS error: {exc} — reconnecting in {delay:.1f}s")
                jitter = delay * self.RECONNECT_JITTER * random.random()
                await asyncio.sleep(delay + jitter)
                delay = min(delay * 2, self.RECONNECT_MAX_DELAY)

        self.kalshi_connected = False
        self._kalshi_ws = None

    async def subscribe_orderbook(self, ticker: str):
        """Subscribe to orderbook_delta for a specific market ticker."""
        if ticker in self._kalshi_subscribed_ob:
            return
        if self._kalshi_ws and self.kalshi_connected:
            try:
                await self._kalshi_ws.send(json.dumps({
                    "id": 2,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": [ticker],
                    },
                }))
                self._kalshi_subscribed_ob.add(ticker)
                log_event("ALPHA", f"Subscribed to orderbook for {ticker}")
            except Exception as exc:
                log_event("ALPHA", f"Failed to subscribe orderbook for {ticker}: {exc}")

    def get_live_orderbook(self, ticker: str) -> dict | None:
        """Get the live orderbook for a ticker, or None if not available."""
        return self.kalshi_orderbook.get(ticker)

    def get_live_ticker(self, ticker: str) -> dict | None:
        """Get the latest ticker data for a market."""
        return self.kalshi_ticker.get(ticker)

    # ------------------------------------------------------------------
    # Delta computation
    # ------------------------------------------------------------------

    def _update_delta(self):
        if self.binance_price > 0 and self.coinbase_price > 0:
            self.latency_delta = self.binance_price - self.coinbase_price

            # Record delta and compute momentum (deviation from rolling baseline)
            now = time.time()
            self._delta_history.append((now, self.latency_delta))

            # Trim to window
            cutoff = now - self.DELTA_WINDOW_SECONDS
            self._delta_history = [
                (ts, d) for ts, d in self._delta_history if ts >= cutoff
            ]

            if len(self._delta_history) >= 2:
                self.delta_baseline = (
                    sum(d for _, d in self._delta_history)
                    / len(self._delta_history)
                )
                self.delta_momentum = self.latency_delta - self.delta_baseline
            else:
                # Not enough data yet — no signal
                self.delta_baseline = self.latency_delta
                self.delta_momentum = 0.0

    # ------------------------------------------------------------------
    # Settlement projection (BRTI proxy)
    # ------------------------------------------------------------------

    def _record_minute_price(self, price: float):
        now = datetime.now(timezone.utc)
        current_minute = now.minute

        if current_minute != self._current_minute:
            self._minute_prices = []
            self._current_minute = current_minute

        self._minute_prices.append((time.time(), price))

        # Keep projected_settlement always up-to-date (rolling average of current minute)
        if self._minute_prices:
            self.projected_settlement = sum(p for _, p in self._minute_prices) / len(self._minute_prices)

    def get_settlement_projection(
        self, strike_price: float, seconds_remaining: float
    ) -> bool:
        """Project whether the settlement price will beat the strike.

        Computes a time-weighted average: recorded prices for elapsed time +
        current price assumed for the remaining seconds.

        Returns True if projected average >= strike (YES wins), False otherwise.
        """
        if not self._minute_prices or self.coinbase_price <= 0:
            return True  # no data — default to no action

        now = time.time()

        elapsed_prices = [p for _, p in self._minute_prices]
        avg_so_far = sum(elapsed_prices) / len(elapsed_prices)

        first_ts = self._minute_prices[0][0]
        elapsed_seconds = max(now - first_ts, 1.0)
        total_window = elapsed_seconds + max(seconds_remaining, 0)

        if total_window <= 0:
            total_window = 1.0

        projected_avg = (
            (avg_so_far * elapsed_seconds)
            + (self.coinbase_price * max(seconds_remaining, 0))
        ) / total_window

        self.projected_settlement = projected_avg
        return projected_avg >= strike_price

    # ------------------------------------------------------------------
    # Status snapshot (for dashboard)
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        return {
            "binance_price": self.binance_price,
            "coinbase_price": self.coinbase_price,
            "latency_delta": self.latency_delta,
            "delta_baseline": self.delta_baseline,
            "delta_momentum": self.delta_momentum,
            "projected_settlement": self.projected_settlement,
            "binance_connected": self.binance_connected,
            "coinbase_connected": self.coinbase_connected,
            "kalshi_connected": self.kalshi_connected,
        }
