"""
Alpha Engine — multi-exchange price feeds, weighted consensus, and settlement projection.

Maintains WebSocket connections to 6 exchanges via ccxt.pro:
  Lead Exchanges (60% weight — where aggressive price discovery happens):
    - Binance Futures  (35%) — highest volume, ultimate price leader
    - Bybit Futures    (20%) — volatility leader, often moves first
    - OKX Futures      (05%) — liquidity depth, confirms real moves

  Settlement Influencers (40% weight — directly impact CME CF BRTI index):
    - Coinbase Spot    (18%) — high BRTI influence, directly affects Kalshi settlement
    - Kraken Spot      (08%) — BRTI index component, fine-tuning
    - Deribit Futures   (07%) — whale sentiment, predictive for 15-min moves

Plus Kalshi WebSocket for real-time ticker, orderbook, and fill data.

Key signals:
  - get_weighted_global_price(): consensus BTC price across all 6 exchanges
  - get_signal(strike_price): BULLISH/BEARISH/NEUTRAL based on global vs strike
  - get_lead_vs_settlement(): spread between fast (futures) and slow (spot/index) exchanges
  - delta_momentum: legacy Binance-Coinbase deviation signal (preserved)
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

# Try to import ccxt.pro for multi-exchange WebSocket feeds
try:
    import ccxt.pro as ccxtpro
    HAS_CCXT = True
except ImportError:
    ccxtpro = None
    HAS_CCXT = False


# ---------------------------------------------------------------------------
# Exchange configuration
# ---------------------------------------------------------------------------

EXCHANGE_CONFIG = {
    'binance': {
        'weight': 0.35,
        'tier': 1,
        'role': 'lead',
        'symbol': 'BTC/USDT:USDT',
        'label': 'Binance Futures',
        'ccxt_options': {'defaultType': 'future'},
    },
    'bybit': {
        'weight': 0.20,
        'tier': 1,
        'role': 'lead',
        'symbol': 'BTC/USDT:USDT',
        'label': 'Bybit Futures',
        'ccxt_options': {'defaultType': 'swap'},
    },
    'coinbase': {
        'weight': 0.18,
        'tier': 2,
        'role': 'settlement',
        'symbol': 'BTC/USD',
        'label': 'Coinbase Spot',
        'ccxt_options': {},
    },
    'okx': {
        'weight': 0.12,
        'tier': 2,
        'role': 'lead',
        'symbol': 'BTC/USDT:USDT',
        'label': 'OKX Perpetual',
        'ccxt_options': {'defaultType': 'swap'},
    },
    'kraken': {
        'weight': 0.08,
        'tier': 3,
        'role': 'settlement',
        'symbol': 'BTC/USD',
        'label': 'Kraken Spot',
        'ccxt_options': {},
    },
    'deribit': {
        'weight': 0.07,
        'tier': 3,
        'role': 'lead',
        'symbol': 'BTC/USD:BTC',
        'label': 'Deribit Futures',
        'ccxt_options': {},
    },
}

LEAD_EXCHANGES = {k for k, v in EXCHANGE_CONFIG.items() if v['role'] == 'lead'}
SETTLEMENT_EXCHANGES = {k for k, v in EXCHANGE_CONFIG.items() if v['role'] == 'settlement'}


class AlphaMonitor:
    """Long-lived async service that tracks cross-exchange BTC prices."""

    RECONNECT_BASE_DELAY = 1.0
    RECONNECT_MAX_DELAY = 30.0
    RECONNECT_JITTER = 0.5
    DELTA_WINDOW_SECONDS = 60

    def __init__(self):
        # Per-exchange prices and connection status
        self.prices: dict[str, float] = {ex: 0.0 for ex in EXCHANGE_CONFIG}
        self._exchange_connected: dict[str, bool] = {ex: False for ex in EXCHANGE_CONFIG}

        # Weighted global price (updated on every tick)
        self._weighted_price: float = 0.0
        self.lead_lag_spread: float = 0.0  # lead_price - settlement_price

        # Legacy fields (backward compat with trader.py)
        self.binance_price: float = 0.0
        self.coinbase_price: float = 0.0
        self.latency_delta: float = 0.0

        # Momentum tracking
        self._delta_history: list[tuple[float, float]] = []
        self.delta_baseline: float = 0.0
        self.delta_momentum: float = 0.0

        # Settlement projection (BRTI proxy)
        self._minute_prices: list[tuple[float, float]] = []
        self._current_minute: int = -1
        self.projected_settlement: float = 0.0

        # Kalshi real-time data
        self.kalshi_connected: bool = False
        self.kalshi_ticker: dict[str, dict] = {}
        self.kalshi_orderbook: dict[str, dict] = {}
        self.kalshi_fills: list[dict] = []
        self._kalshi_subscribed_ob: set[str] = set()
        self._kalshi_ws = None

        self._tasks: list[asyncio.Task] = []
        self._running: bool = False

    # Legacy properties for backward compat
    @property
    def binance_connected(self) -> bool:
        return self._exchange_connected.get('binance', False)

    @binance_connected.setter
    def binance_connected(self, val: bool):
        self._exchange_connected['binance'] = val

    @property
    def coinbase_connected(self) -> bool:
        return self._exchange_connected.get('coinbase', False)

    @coinbase_connected.setter
    def coinbase_connected(self, val: bool):
        self._exchange_connected['coinbase'] = val

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        if self._running:
            return
        self._running = True

        if HAS_CCXT:
            exchanges = list(EXCHANGE_CONFIG.keys())
            log_event("ALPHA", f"Alpha Engine starting — {len(exchanges)} exchanges via ccxt.pro + Kalshi WS")
            self._tasks = [
                asyncio.create_task(self._stream_exchange(ex), name=f"alpha-{ex}")
                for ex in exchanges
            ]
        else:
            log_event("ALPHA", "Alpha Engine starting — ccxt not available, fallback to raw WS (Binance + Coinbase)")
            self._tasks = [
                asyncio.create_task(self._binance_loop_fallback(), name="alpha-binance"),
                asyncio.create_task(self._coinbase_loop_fallback(), name="alpha-coinbase"),
            ]

        self._tasks.append(
            asyncio.create_task(self._kalshi_loop(), name="alpha-kalshi")
        )

    async def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        for ex in EXCHANGE_CONFIG:
            self._exchange_connected[ex] = False
        self.kalshi_connected = False
        self._kalshi_ws = None
        log_event("ALPHA", "Alpha Engine stopped")

    # ------------------------------------------------------------------
    # ccxt.pro exchange streams
    # ------------------------------------------------------------------

    async def _stream_exchange(self, exchange_id: str):
        """Stream prices from a single exchange via ccxt.pro with auto-reconnect."""
        cfg = EXCHANGE_CONFIG[exchange_id]
        delay = self.RECONNECT_BASE_DELAY

        while self._running:
            exchange = None
            try:
                exchange_class = getattr(ccxtpro, exchange_id)
                exchange = exchange_class({
                    'enableRateLimit': True,
                    'options': cfg.get('ccxt_options', {}),
                })
                symbol = cfg['symbol']

                self._exchange_connected[exchange_id] = True
                delay = self.RECONNECT_BASE_DELAY
                log_event("ALPHA", f"{cfg['label']} connected")

                while self._running:
                    ticker = await exchange.watch_ticker(symbol)
                    price = ticker.get('last')
                    if price and float(price) > 0:
                        p = float(price)
                        self.prices[exchange_id] = p

                        # Legacy fields
                        if exchange_id == 'binance':
                            self.binance_price = p
                        elif exchange_id == 'coinbase':
                            self.coinbase_price = p

                        # BRTI exchanges feed settlement projection
                        if exchange_id in SETTLEMENT_EXCHANGES:
                            self._record_minute_price(p)

                        self._update_weighted_price()
                        self._update_delta()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._exchange_connected[exchange_id] = False
                log_event("ALPHA", f"{cfg['label']} error: {exc} — reconnecting in {delay:.1f}s")
                jitter = delay * self.RECONNECT_JITTER * random.random()
                await asyncio.sleep(delay + jitter)
                delay = min(delay * 2, self.RECONNECT_MAX_DELAY)
            finally:
                if exchange:
                    try:
                        await exchange.close()
                    except Exception:
                        pass

        self._exchange_connected[exchange_id] = False

    # ------------------------------------------------------------------
    # Fallback raw WebSocket loops (when ccxt.pro is not installed)
    # ------------------------------------------------------------------

    BINANCE_WS_URL = "wss://fstream.binance.com/ws/btcusdt@trade"
    COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"

    async def _binance_loop_fallback(self):
        delay = self.RECONNECT_BASE_DELAY
        while self._running:
            try:
                async with websockets.connect(
                    self.BINANCE_WS_URL, ping_interval=60, ping_timeout=30, close_timeout=10,
                ) as ws:
                    self._exchange_connected['binance'] = True
                    delay = self.RECONNECT_BASE_DELAY
                    log_event("ALPHA", "Binance WS connected (fallback)")
                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw_msg)
                            price = float(data.get("p", 0))
                            if price > 0:
                                self.binance_price = price
                                self.prices['binance'] = price
                                self._update_weighted_price()
                                self._update_delta()
                        except (json.JSONDecodeError, ValueError, KeyError):
                            pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._exchange_connected['binance'] = False
                log_event("ALPHA", f"Binance WS error: {exc} — reconnecting in {delay:.1f}s")
                jitter = delay * self.RECONNECT_JITTER * random.random()
                await asyncio.sleep(delay + jitter)
                delay = min(delay * 2, self.RECONNECT_MAX_DELAY)
        self._exchange_connected['binance'] = False

    async def _coinbase_loop_fallback(self):
        delay = self.RECONNECT_BASE_DELAY
        while self._running:
            try:
                async with websockets.connect(
                    self.COINBASE_WS_URL, ping_interval=60, ping_timeout=30, close_timeout=10,
                ) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "product_ids": ["BTC-USD"],
                        "channels": ["ticker"],
                    }))
                    self._exchange_connected['coinbase'] = True
                    delay = self.RECONNECT_BASE_DELAY
                    log_event("ALPHA", "Coinbase WS connected (fallback)")
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
                                self.prices['coinbase'] = price
                                self._record_minute_price(price)
                                self._update_weighted_price()
                                self._update_delta()
                        except (json.JSONDecodeError, ValueError, KeyError):
                            pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._exchange_connected['coinbase'] = False
                log_event("ALPHA", f"Coinbase WS error: {exc} — reconnecting in {delay:.1f}s")
                jitter = delay * self.RECONNECT_JITTER * random.random()
                await asyncio.sleep(delay + jitter)
                delay = min(delay * 2, self.RECONNECT_MAX_DELAY)
        self._exchange_connected['coinbase'] = False

    # ------------------------------------------------------------------
    # Kalshi WebSocket (real-time ticker + orderbook + fills)
    # ------------------------------------------------------------------

    def _kalshi_ws_url(self) -> str:
        return config.KALSHI_HOST.replace("https://", "wss://") + "/trade-api/ws/v2"

    def _kalshi_auth_headers(self) -> dict:
        import os

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
                    ping_interval=None,
                    close_timeout=10,
                ) as ws:
                    self._kalshi_ws = ws
                    self.kalshi_connected = True
                    self._kalshi_subscribed_ob = set()
                    delay = self.RECONNECT_BASE_DELAY
                    log_event("ALPHA", "Kalshi WS connected")

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
        return self.kalshi_orderbook.get(ticker)

    def get_live_ticker(self, ticker: str) -> dict | None:
        return self.kalshi_ticker.get(ticker)

    # ------------------------------------------------------------------
    # Weighted price computation
    # ------------------------------------------------------------------

    def _update_weighted_price(self):
        self._weighted_price = self.get_weighted_global_price()

        # Compute lead vs settlement spread
        lead_price, settle_price, spread = self.get_lead_vs_settlement()
        self.lead_lag_spread = spread

    def get_weighted_global_price(self) -> float:
        """Weighted consensus price across all connected exchanges."""
        valid = {k: v for k, v in self.prices.items() if v > 0}
        if not valid:
            return 0.0
        total_weight = sum(EXCHANGE_CONFIG[k]['weight'] for k in valid)
        if total_weight <= 0:
            return 0.0
        return sum(valid[k] * EXCHANGE_CONFIG[k]['weight'] for k in valid) / total_weight

    def get_lead_vs_settlement(self) -> tuple[float, float, float]:
        """Compare lead exchange prices to settlement exchange prices.

        Returns (lead_price, settlement_price, spread).
        Positive spread = leads above settlement (bullish move incoming).
        Negative spread = leads below settlement (bearish move incoming).
        """
        lead_valid = {k: self.prices[k] for k in LEAD_EXCHANGES if self.prices.get(k, 0) > 0}
        settle_valid = {k: self.prices[k] for k in SETTLEMENT_EXCHANGES if self.prices.get(k, 0) > 0}

        if not lead_valid or not settle_valid:
            return 0.0, 0.0, 0.0

        lead_w = {k: EXCHANGE_CONFIG[k]['weight'] for k in lead_valid}
        lead_total = sum(lead_w.values())
        lead_price = sum(lead_valid[k] * lead_w[k] for k in lead_valid) / lead_total

        settle_w = {k: EXCHANGE_CONFIG[k]['weight'] for k in settle_valid}
        settle_total = sum(settle_w.values())
        settle_price = sum(settle_valid[k] * settle_w[k] for k in settle_valid) / settle_total

        return lead_price, settle_price, lead_price - settle_price

    def get_signal(self, kalshi_strike_price: float, threshold: float = None) -> tuple[str, float]:
        """Generate a trade signal: BULLISH, BEARISH, or NEUTRAL.

        Compares the weighted global price to the Kalshi strike.
        The 'edge' is the difference between where the market actually IS
        and where Kalshi's contract strike sits.

        Args:
            kalshi_strike_price: The strike price of the current Kalshi contract.
            threshold: USD difference needed to trigger a signal.
                       Defaults to config.LEAD_LAG_THRESHOLD.

        Returns:
            (signal, diff) where signal is "BULLISH"/"BEARISH"/"NEUTRAL"
            and diff is the raw dollar difference (positive = above strike).
        """
        if threshold is None:
            threshold = config.LEAD_LAG_THRESHOLD

        global_price = self.get_weighted_global_price()
        if not global_price or not kalshi_strike_price:
            return "NEUTRAL", 0.0

        diff = global_price - kalshi_strike_price

        if diff > threshold:
            return "BULLISH", diff
        elif diff < -threshold:
            return "BEARISH", diff
        return "NEUTRAL", diff

    # ------------------------------------------------------------------
    # Delta computation (legacy + enhanced)
    # ------------------------------------------------------------------

    def _update_delta(self):
        # Legacy: Binance - Coinbase
        if self.binance_price > 0 and self.coinbase_price > 0:
            self.latency_delta = self.binance_price - self.coinbase_price

        # Momentum tracking uses lead-lag spread when available,
        # falls back to legacy binance-coinbase delta
        signal_value = self.lead_lag_spread if self.lead_lag_spread != 0.0 else self.latency_delta
        if signal_value == 0.0:
            return

        now = time.time()
        self._delta_history.append((now, signal_value))

        cutoff = now - self.DELTA_WINDOW_SECONDS
        self._delta_history = [
            (ts, d) for ts, d in self._delta_history if ts >= cutoff
        ]

        if len(self._delta_history) >= 2:
            self.delta_baseline = (
                sum(d for _, d in self._delta_history) / len(self._delta_history)
            )
            self.delta_momentum = signal_value - self.delta_baseline
        else:
            self.delta_baseline = signal_value
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

        if self._minute_prices:
            self.projected_settlement = sum(p for _, p in self._minute_prices) / len(self._minute_prices)

    def get_settlement_projection(
        self, strike_price: float, seconds_remaining: float
    ) -> bool:
        """Project whether the settlement price will beat the strike.

        Uses BRTI-proxy prices (Coinbase + Kraken) for the projection.
        Returns True if projected average >= strike (YES wins).
        """
        # Use settlement exchange price as the "current" reference
        ref_price = 0.0
        for ex in SETTLEMENT_EXCHANGES:
            if self.prices.get(ex, 0) > 0:
                ref_price = self.prices[ex]
                break
        if ref_price <= 0:
            ref_price = self.coinbase_price

        if not self._minute_prices or ref_price <= 0:
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
            + (ref_price * max(seconds_remaining, 0))
        ) / total_window

        self.projected_settlement = projected_avg
        return projected_avg >= strike_price

    # ------------------------------------------------------------------
    # Status snapshot (for dashboard)
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        connected_count = sum(1 for v in self._exchange_connected.values() if v)
        total_count = len(EXCHANGE_CONFIG)

        return {
            # Legacy fields
            "binance_price": self.binance_price,
            "coinbase_price": self.coinbase_price,
            "latency_delta": self.latency_delta,
            "delta_baseline": self.delta_baseline,
            "delta_momentum": self.delta_momentum,
            "projected_settlement": self.projected_settlement,
            "binance_connected": self.binance_connected,
            "coinbase_connected": self.coinbase_connected,
            "kalshi_connected": self.kalshi_connected,
            # New multi-exchange fields
            "weighted_global_price": self._weighted_price,
            "lead_lag_spread": self.lead_lag_spread,
            "exchanges_connected": connected_count,
            "exchanges_total": total_count,
            "exchange_prices": {
                ex: {
                    "price": self.prices[ex],
                    "connected": self._exchange_connected[ex],
                    "weight": EXCHANGE_CONFIG[ex]['weight'],
                    "tier": EXCHANGE_CONFIG[ex]['tier'],
                    "role": EXCHANGE_CONFIG[ex]['role'],
                    "label": EXCHANGE_CONFIG[ex]['label'],
                }
                for ex in EXCHANGE_CONFIG
            },
            "has_ccxt": HAS_CCXT,
        }
