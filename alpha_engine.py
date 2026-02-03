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
import math
import random
import time
from datetime import datetime, timezone

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config
from database import log_event, record_trade

# Try to import ccxt.pro for multi-exchange WebSocket feeds
try:
    import ccxt.pro as ccxtpro
    HAS_CCXT = True
except ImportError:
    ccxtpro = None
    HAS_CCXT = False


# ---------------------------------------------------------------------------
# Exchange configuration (per-asset symbols)
# ---------------------------------------------------------------------------

# Base exchange config (weights, roles, options — shared across assets)
EXCHANGE_BASE_CONFIG = {
    'binance': {
        'weight': 0.35,
        'tier': 1,
        'role': 'lead',
        'label': 'Binance Futures',
        'ccxt_options': {'defaultType': 'future'},
    },
    'bybit': {
        'weight': 0.20,
        'tier': 1,
        'role': 'lead',
        'label': 'Bybit Futures',
        'ccxt_options': {'defaultType': 'swap'},
    },
    'coinbase': {
        'weight': 0.18,
        'tier': 2,
        'role': 'settlement',
        'label': 'Coinbase Spot',
        'ccxt_options': {},
    },
    'okx': {
        'weight': 0.12,
        'tier': 2,
        'role': 'lead',
        'label': 'OKX Perpetual',
        'ccxt_options': {'defaultType': 'swap'},
    },
    'kraken': {
        'weight': 0.08,
        'tier': 3,
        'role': 'settlement',
        'label': 'Kraken Spot',
        'ccxt_options': {},
    },
    'deribit': {
        'weight': 0.07,
        'tier': 3,
        'role': 'lead',
        'label': 'Deribit Futures',
        'ccxt_options': {},
    },
}

# Per-asset exchange symbols
ASSET_EXCHANGE_SYMBOLS = {
    'btc': {
        'binance': 'BTC/USDT:USDT',
        'bybit': 'BTC/USDT:USDT',
        'coinbase': 'BTC/USD',
        'okx': 'BTC/USDT:USDT',
        'kraken': 'BTC/USD',
        'deribit': 'BTC/USD:BTC',
    },
    'eth': {
        'binance': 'ETH/USDT:USDT',
        'bybit': 'ETH/USDT:USDT',
        'coinbase': 'ETH/USD',
        'okx': 'ETH/USDT:USDT',
        'kraken': 'ETH/USD',
        'deribit': 'ETH/USD:ETH',
    },
    'sol': {
        'binance': 'SOL/USDT:USDT',
        'bybit': 'SOL/USDT:USDT',
        'coinbase': 'SOL/USD',
        'okx': 'SOL/USDT:USDT',
        'kraken': 'SOL/USD',
        # Deribit doesn't have SOL perpetuals - skip
    },
}


def get_exchange_config(asset: str = 'btc') -> dict:
    """Build full exchange config with asset-specific symbols.

    Only includes exchanges that have a symbol defined for this asset.
    """
    symbols = ASSET_EXCHANGE_SYMBOLS.get(asset, ASSET_EXCHANGE_SYMBOLS['btc'])
    result = {}
    for ex_id, base in EXCHANGE_BASE_CONFIG.items():
        # Only include exchange if it has a symbol for this asset
        if ex_id not in symbols:
            continue
        result[ex_id] = {
            **base,
            'symbol': symbols[ex_id],
        }
    return result


# Default config for backward compatibility
EXCHANGE_CONFIG = get_exchange_config('btc')

LEAD_EXCHANGES = {k for k, v in EXCHANGE_BASE_CONFIG.items() if v['role'] == 'lead'}
SETTLEMENT_EXCHANGES = {k for k, v in EXCHANGE_BASE_CONFIG.items() if v['role'] == 'settlement'}


class AlphaMonitor:
    """Long-lived async service that tracks cross-exchange prices for a single asset."""

    RECONNECT_BASE_DELAY = 1.0
    RECONNECT_MAX_DELAY = 30.0
    RECONNECT_JITTER = 0.5
    DELTA_WINDOW_SECONDS = 60

    def __init__(self, asset: str = "btc"):
        """Initialize alpha monitor for a specific asset.

        Args:
            asset: Asset to track ('btc', 'eth', or 'sol')
        """
        self.asset = asset
        self._exchange_config = get_exchange_config(asset)

        # Per-exchange prices and connection status
        self.prices: dict[str, float] = {ex: 0.0 for ex in self._exchange_config}
        self._exchange_connected: dict[str, bool] = {ex: False for ex in self._exchange_config}

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

        # Rolling price history (15-min window for trend/volatility analysis)
        self._price_history: list[tuple[float, float]] = []  # (timestamp, weighted_global_price)
        self.PRICE_HISTORY_WINDOW = 900  # 15 minutes in seconds

        # Full-contract settlement tracking (persists across minute boundaries)
        self._contract_settlement_prices: list[tuple[float, float]] = []
        self._contract_start_ts: float = 0.0

        # Kalshi real-time data
        self.kalshi_connected: bool = False
        self.kalshi_ticker: dict[str, dict] = {}
        self.kalshi_orderbook: dict[str, dict] = {}
        self._kalshi_ob_ts: dict[str, float] = {}  # last update timestamp per ticker
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

        asset_label = self.asset.upper()

        if HAS_CCXT:
            exchanges = list(self._exchange_config.keys())
            log_event("ALPHA", f"Alpha Engine [{asset_label}] starting — {len(exchanges)} exchanges via ccxt.pro + Kalshi WS", self.asset)
            self._tasks = [
                asyncio.create_task(self._stream_exchange(ex), name=f"alpha-{self.asset}-{ex}")
                for ex in exchanges
            ]
        else:
            log_event("ALPHA", f"Alpha Engine [{asset_label}] starting — ccxt not available, fallback to raw WS (Binance + Coinbase)", self.asset)
            self._tasks = [
                asyncio.create_task(self._binance_loop_fallback(), name=f"alpha-{self.asset}-binance"),
                asyncio.create_task(self._coinbase_loop_fallback(), name=f"alpha-{self.asset}-coinbase"),
            ]

        self._tasks.append(
            asyncio.create_task(self._kalshi_loop(), name=f"alpha-{self.asset}-kalshi")
        )

    async def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        for ex in self._exchange_config:
            self._exchange_connected[ex] = False
        self.kalshi_connected = False
        self._kalshi_ws = None
        log_event("ALPHA", f"Alpha Engine [{self.asset.upper()}] stopped", self.asset)

    # ------------------------------------------------------------------
    # ccxt.pro exchange streams
    # ------------------------------------------------------------------

    async def _stream_exchange(self, exchange_id: str):
        """Stream prices from a single exchange via ccxt.pro with auto-reconnect."""
        cfg = self._exchange_config[exchange_id]
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

                # Load markets before watching tickers
                await exchange.load_markets()

                # Check if symbol exists on this exchange
                if symbol not in exchange.markets:
                    log_event("ALPHA", f"{cfg['label']} does not have {symbol} — skipping", self.asset)
                    return  # Exit cleanly, don't retry

                self._exchange_connected[exchange_id] = True
                delay = self.RECONNECT_BASE_DELAY
                log_event("ALPHA", f"{cfg['label']} connected ({symbol})", self.asset)

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
                # Check for BadSymbol errors - don't retry indefinitely
                exc_name = type(exc).__name__
                if 'BadSymbol' in exc_name or 'symbol' in str(exc).lower():
                    log_event("ALPHA", f"{cfg['label']} symbol {cfg['symbol']} not found — skipping", self.asset)
                    return  # Exit cleanly
                log_event("ALPHA", f"{cfg['label']} error: {exc} — reconnecting in {delay:.1f}s", self.asset)
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
                    log_event("ALPHA", "Binance WS connected (fallback)", self.asset)
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
                log_event("ALPHA", f"Binance WS error: {exc} — reconnecting in {delay:.1f}s", self.asset)
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
                    log_event("ALPHA", "Coinbase WS connected (fallback)", self.asset)
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
                log_event("ALPHA", f"Coinbase WS error: {exc} — reconnecting in {delay:.1f}s", self.asset)
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
                    log_event("ALPHA", "Kalshi WS connected", self.asset)

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
                                    self._kalshi_ob_ts[ticker] = time.time()

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
                                    self._kalshi_ob_ts[ticker] = time.time()

                            elif msg_type == "fill":
                                self.kalshi_fills.append(msg)
                                self.kalshi_fills = self.kalshi_fills[-50:]
                                log_event("TRADE", f"WS fill: {msg.get('side','')} {msg.get('count',0)}x @ {msg.get('yes_price', msg.get('no_price','?'))}c on {msg.get('ticker','')}", self.asset)

                                # Record fill to database
                                try:
                                    side = msg.get('side', '').lower()
                                    count = msg.get('count', 0)
                                    price_cents = msg.get('yes_price') if side == 'yes' else msg.get('no_price')
                                    ticker = msg.get('ticker', '')
                                    action = msg.get('action', '').upper()  # BUY or SELL

                                    if side and count and price_cents and ticker and action:
                                        record_trade(
                                            market_id=ticker,
                                            side=side,
                                            action=action,
                                            price=price_cents / 100.0,
                                            quantity=count,
                                            order_id=msg.get('order_id'),
                                            exit_type=None  # Will be set by close_position if it's an exit
                                        )
                                except Exception as e:
                                    log_event("ERROR", f"Failed to record WS fill: {e}", self.asset)

                        except (json.JSONDecodeError, ValueError, KeyError):
                            pass

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.kalshi_connected = False
                self._kalshi_ws = None
                log_event("ALPHA", f"Kalshi WS error: {exc} — reconnecting in {delay:.1f}s", self.asset)
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
                log_event("ALPHA", f"Subscribed to orderbook for {ticker}", self.asset)
            except Exception as exc:
                log_event("ALPHA", f"Failed to subscribe orderbook for {ticker}: {exc}", self.asset)

    def get_live_orderbook(self, ticker: str, max_age: float = 5.0) -> dict | None:
        """Return WS orderbook only if it was updated within max_age seconds."""
        ob = self.kalshi_orderbook.get(ticker)
        if not ob:
            return None
        last_ts = self._kalshi_ob_ts.get(ticker, 0)
        if time.time() - last_ts > max_age:
            return None  # Stale — let caller fall back to REST
        return ob

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

        # Record for rolling price history (trend/volatility analysis)
        self._record_price_history(self._weighted_price)

    def get_weighted_global_price(self) -> float:
        """Weighted consensus price across all connected exchanges."""
        valid = {k: v for k, v in self.prices.items() if v > 0}
        if not valid:
            return 0.0
        total_weight = sum(self._exchange_config[k]['weight'] for k in valid)
        if total_weight <= 0:
            return 0.0
        return sum(valid[k] * self._exchange_config[k]['weight'] for k in valid) / total_weight

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

        lead_w = {k: self._exchange_config[k]['weight'] for k in lead_valid}
        lead_total = sum(lead_w.values())
        lead_price = sum(lead_valid[k] * lead_w[k] for k in lead_valid) / lead_total

        settle_w = {k: self._exchange_config[k]['weight'] for k in settle_valid}
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
        self._record_contract_settlement(price)

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
    # Rolling price history and derived metrics (for rule-based strategy)
    # ------------------------------------------------------------------

    def _record_price_history(self, weighted_price: float):
        """Record weighted global price for trend/volatility calculations."""
        if weighted_price <= 0:
            return
        now = time.time()
        self._price_history.append((now, weighted_price))
        cutoff = now - self.PRICE_HISTORY_WINDOW
        self._price_history = [(ts, p) for ts, p in self._price_history if ts >= cutoff]

    def _record_contract_settlement(self, price: float):
        """Record settlement-exchange price for full-contract BRTI projection."""
        now = time.time()
        self._contract_settlement_prices.append((now, price))
        cutoff = now - self.PRICE_HISTORY_WINDOW
        self._contract_settlement_prices = [
            (ts, p) for ts, p in self._contract_settlement_prices if ts >= cutoff
        ]

    def reset_contract_window(self):
        """Reset full-contract settlement tracking for a new contract."""
        self._contract_settlement_prices = []
        self._contract_start_ts = time.time()

    def get_price_velocity(self) -> dict:
        """Compute price rate-of-change over 1-min and 5-min windows.

        Returns dict with velocity ($/sec), direction (+1/-1/0), and absolute change.
        """
        now = time.time()
        result = {
            "velocity_1m": 0.0, "velocity_5m": 0.0,
            "direction_1m": 0, "direction_5m": 0,
            "price_change_1m": 0.0, "price_change_5m": 0.0,
        }
        if len(self._price_history) < 2:
            return result

        current_price = self._price_history[-1][1]

        for window_key, window_secs in [("1m", 60), ("5m", 300)]:
            cutoff = now - window_secs
            older = [p for ts, p in self._price_history if ts <= cutoff + 5]
            if older:
                old_price = older[-1]
                change = current_price - old_price
                result[f"velocity_{window_key}"] = change / window_secs
                result[f"direction_{window_key}"] = 1 if change > 0 else (-1 if change < 0 else 0)
                result[f"price_change_{window_key}"] = change

        return result

    def get_volatility(self) -> dict:
        """Compute realized volatility from price history.

        Returns:
          - volatility_5m: return stdev (used internally by fair value calc)
          - vol_dollar_per_min: average absolute BTC movement in $/min (intuitive metric)
          - regime: "high", "medium", or "low" based on $/min thresholds
        """
        now = time.time()
        result = {"volatility_1m": 0.0, "volatility_5m": 0.0,
                  "vol_dollar_per_min": 0.0, "regime": "low"}

        for suffix, window_secs in [("1m", 60), ("5m", 300)]:
            cutoff = now - window_secs
            window = [(ts, p) for ts, p in self._price_history if ts >= cutoff]
            if len(window) < 10:
                continue

            returns = []
            for i in range(1, len(window)):
                if window[i][0] - window[i - 1][0] < 0.1:
                    continue
                ret = (window[i][1] - window[i - 1][1]) / window[i - 1][1]
                returns.append(ret)

            if len(returns) >= 5:
                mean = sum(returns) / len(returns)
                variance = sum((r - mean) ** 2 for r in returns) / len(returns)
                result[f"volatility_{suffix}"] = math.sqrt(variance)

        # Compute $/min: total absolute price path length / duration in minutes
        # This is intuitive: "BTC is moving about $X per minute on average"
        cutoff_5m = now - 300
        window_5m = [(ts, p) for ts, p in self._price_history if ts >= cutoff_5m]
        if len(window_5m) >= 10:
            first_ts = window_5m[0][0]
            last_ts = window_5m[-1][0]
            duration_min = (last_ts - first_ts) / 60.0
            if duration_min > 0.5:
                total_movement = sum(
                    abs(window_5m[i][1] - window_5m[i - 1][1])
                    for i in range(1, len(window_5m))
                )
                result["vol_dollar_per_min"] = total_movement / duration_min

        # Regime classification using $/min (config thresholds are in $/min)
        vol_dpm = result["vol_dollar_per_min"]
        if vol_dpm > config.VOL_HIGH_THRESHOLD:
            result["regime"] = "high"
        elif vol_dpm > config.VOL_LOW_THRESHOLD:
            result["regime"] = "medium"
        else:
            result["regime"] = "low"

        return result

    def get_fair_value(self, strike_price: float, seconds_remaining: float) -> dict:
        """Estimate fair YES probability using projected settlement vs strike.

        Uses full-contract settlement prices + current weighted price, converted
        to probability via a logistic function scaled by realized volatility.
        """
        gwp = self.get_weighted_global_price()
        if gwp <= 0 or strike_price <= 0:
            return {"fair_yes_prob": 0.5, "fair_yes_cents": 50,
                    "btc_vs_strike": 0.0, "projected_settlement": 0.0}

        btc_vs_strike = gwp - strike_price

        # Full-contract settlement projection
        if self._contract_settlement_prices:
            avg_settlement = (
                sum(p for _, p in self._contract_settlement_prices)
                / len(self._contract_settlement_prices)
            )
        else:
            settle_valid = {
                k: self.prices[k] for k in SETTLEMENT_EXCHANGES
                if self.prices.get(k, 0) > 0
            }
            avg_settlement = (
                sum(settle_valid.values()) / len(settle_valid) if settle_valid else gwp
            )

        # Blend historical settlement avg with current price
        # Weight current price more as we approach expiry
        if seconds_remaining > 0 and self._contract_start_ts > 0:
            elapsed = max(time.time() - self._contract_start_ts, 1.0)
            total = elapsed + seconds_remaining
            current_weight = min(0.85, seconds_remaining / total + 0.3)
            projected = avg_settlement * (1 - current_weight) + gwp * current_weight
        else:
            projected = avg_settlement

        settlement_vs_strike = projected - strike_price

        # Convert $ distance to probability using logistic function
        vol_data = self.get_volatility()
        vol = vol_data["volatility_5m"] if vol_data["volatility_5m"] > 0 else 0.0001

        # Dollar volatility over remaining contract time
        dollar_vol = gwp * vol * math.sqrt(max(seconds_remaining, 1) / 5)
        dollar_vol = max(dollar_vol, 1.0)

        z_score = settlement_vs_strike / dollar_vol

        k = config.FAIR_VALUE_K
        try:
            fair_prob = 1.0 / (1.0 + math.exp(-k * z_score))
        except OverflowError:
            fair_prob = 1.0 if z_score > 0 else 0.0

        fair_prob = max(0.01, min(0.99, fair_prob))

        return {
            "fair_yes_prob": fair_prob,
            "fair_yes_cents": round(fair_prob * 100),
            "btc_vs_strike": btc_vs_strike,
            "projected_settlement": projected,
        }

    # ------------------------------------------------------------------
    # Status snapshot (for dashboard)
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        connected_count = sum(1 for v in self._exchange_connected.values() if v)
        total_count = len(self._exchange_config)

        return {
            # Asset identifier
            "asset": self.asset,
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
                    "weight": self._exchange_config[ex]['weight'],
                    "tier": self._exchange_config[ex]['tier'],
                    "role": self._exchange_config[ex]['role'],
                    "label": self._exchange_config[ex]['label'],
                }
                for ex in self._exchange_config
            },
            "has_ccxt": HAS_CCXT,
            # Rule-based strategy metrics
            "price_velocity": self.get_price_velocity(),
            "volatility": self.get_volatility(),
            "price_history_len": len(self._price_history),
        }
