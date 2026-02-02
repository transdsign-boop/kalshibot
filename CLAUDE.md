# Kalshibot - Kalshi BTC 15-Minute Binary Options Trading Bot

## Architecture
- **Backend**: Python 3.12, FastAPI/Uvicorn on port 8000
- **Frontend**: React/JSX (Vite), served as static files from `frontend/dist/`
- **Database**: SQLite at `/data/kalshibot.db` (Fly.io persistent volume)
- **Deployment**: Fly.io (`flyctl deploy --app kalshibot --remote-only`)
- **Market**: KXBTC15M series — 15-minute binary options on BTC price

## Key Files
- `trader.py` — Core trading bot: cycle loop, order execution, paper trading, exit logic, dashboard data
- `alpha_engine.py` — Multi-exchange price monitoring (6 exchanges via WebSocket), volatility, fair value
- `agent.py` — Rule-based strategy engine: `analyze_market()` with edge, trend, vol regime, time decay
- `config.py` — All tunable settings with runtime persistence via database
- `web.py` — FastAPI API endpoints, REST orderbook caching, dashboard patching
- `frontend/src/components/AlphaDashboard.jsx` — Strategy dashboard with inline-editable thresholds
- `frontend/src/components/ConfigPanel.jsx` — Configuration panel (non-dashboard settings only)

## Trading Pipeline
1. Alpha engine collects BTC prices from 6 exchanges (Binance, Bybit, Coinbase, OKX, Kraken, Deribit)
2. Computes weighted global price, projected settlement, volatility (tick-by-tick path length)
3. Fair value: projected settlement -> z-score (vs dollar_vol) -> logistic function (k) -> probability -> cents
4. Edge = fair_value_cents - market_price. Only trades when edge >= MIN_EDGE_CENTS
5. Alpha overrides (lead-lag, momentum) are edge-gated — won't fire without actual mispricing
6. Rule engine scores YES/NO based on edge ratio + trend bonus + time decay -> confidence

## BTC-Specific Calibration (critical context)
- `vol_dollar_per_min` uses **tick-by-tick path length**, NOT candle close-to-close
- Tick path is ~5-6x higher than candle data (e.g., candle $87/min = tick $500/min)
- VOL thresholds must be calibrated for tick scale, not candle scale
- BTC 1-min candle stats: mean $87, median $63, 75th $120, 90th $190
- BTC 15-min movements: mean $291, median $255, 75th $378, 90th $612
- BTC velocity: mean $1.45/sec, median $1.04/sec, 75th $2.01/sec

## Known Issues & Design Decisions
- **Kalshi WS orderbook_delta often stops sending updates** — bot uses REST API first for orderbook, WS as fallback only
- **Paper mode can't simulate resting orders** — paper fills cross the spread immediately for realistic quantities
- **FAIR_VALUE_K must stay moderate (0.4-0.8)** — higher values push fair value to extremes (1c/99c) where market already prices correctly, eliminating all edge
- **Config values persist in SQLite** — `restore_tunables()` loads saved values on startup, so code defaults only apply to fresh installs

## Deployment
- Fly.io app: `kalshibot`, region: `gru`
- Fly token saved at scratchpad; use `FLY_API_TOKEN="$(cat <token_file>)" flyctl deploy`
- Always build frontend first: `cd frontend && npm run build`
- Runtime config can be pushed without deploy: `POST /api/config {"KEY": value}`

## Commands
- `cd frontend && npm run build` — Build frontend
- `flyctl deploy --app kalshibot --remote-only` — Deploy to Fly.io
- `curl https://kalshibot.fly.dev/api/status` — Check live status
- `curl -X POST https://kalshibot.fly.dev/api/config -H "Content-Type: application/json" -d '{"KEY": value}'` — Update live config
