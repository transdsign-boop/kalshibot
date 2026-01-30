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
import json
import random
import time
from datetime import datetime, timezone

import websockets

from database import log_event


class AlphaMonitor:
    """Long-lived async service that tracks cross-exchange BTC prices."""

    BINANCE_WS_URL = "wss://fstream.binance.com/ws/btcusdt@trade"
    COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"

    RECONNECT_BASE_DELAY = 1.0
    RECONNECT_MAX_DELAY = 30.0
    RECONNECT_JITTER = 0.5

    def __init__(self):
        self.binance_price: float = 0.0
        self.coinbase_price: float = 0.0
        self.latency_delta: float = 0.0

        self.binance_connected: bool = False
        self.coinbase_connected: bool = False

        # Rolling prices for current minute (list of (timestamp, price))
        self._minute_prices: list[tuple[float, float]] = []
        self._current_minute: int = -1

        self.projected_settlement: float = 0.0

        self._tasks: list[asyncio.Task] = []
        self._running: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Launch both WebSocket listeners as background tasks."""
        if self._running:
            return
        self._running = True
        log_event("ALPHA", "Alpha Engine starting — connecting to Binance + Coinbase")
        self._tasks = [
            asyncio.create_task(self._binance_loop(), name="alpha-binance"),
            asyncio.create_task(self._coinbase_loop(), name="alpha-coinbase"),
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
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
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
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
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
    # Delta computation
    # ------------------------------------------------------------------

    def _update_delta(self):
        if self.binance_price > 0 and self.coinbase_price > 0:
            self.latency_delta = self.binance_price - self.coinbase_price

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
            "projected_settlement": self.projected_settlement,
            "binance_connected": self.binance_connected,
            "coinbase_connected": self.coinbase_connected,
        }
