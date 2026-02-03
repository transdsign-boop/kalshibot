import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import config
from config import SUPPORTED_ASSETS

# Simple TTL cache for REST orderbook fetches (avoids hammering Kalshi API)
_ob_cache: dict = {"ticker": "", "data": None, "ts": 0.0}
_OB_CACHE_TTL = 2.0  # seconds
from config import get_tunables, set_tunables, restore_tunables, TUNABLE_FIELDS, BotConfig, migrate_to_dual_config, migrate_to_multi_asset, reset_asset_specific_defaults
from database import init_db, get_recent_logs, get_latest_decision, get_todays_trades, get_trades_with_pnl, get_setting, set_setting, get_all_unsettled_live_entries, backfill_buy_trades_from_snapshots, get_db, set_live_market_pnl
from alpha_engine import AlphaMonitor
from trader import TradingBot

FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"

# --- Multi-Asset Bot Registry ---
# Per-asset alpha monitors (one per asset — handles price feeds for that asset)
alpha_monitors: dict[str, AlphaMonitor] = {}

# Bot registry: (asset, mode) -> TradingBot
# 3 assets × 2 modes = 6 bot instances
bots: dict[tuple[str, str], TradingBot] = {}
bot_configs: dict[tuple[str, str], BotConfig] = {}
bot_tasks: dict[tuple[str, str], asyncio.Task] = {}


def get_bot(asset: str = "btc", mode: str = "paper") -> TradingBot | None:
    """Get the bot instance for the specified asset and mode."""
    return bots.get((asset, mode))


def get_alpha(asset: str = "btc") -> AlphaMonitor | None:
    """Get the alpha monitor for the specified asset."""
    return alpha_monitors.get(asset)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global alpha_monitors, bots, bot_configs, bot_tasks

    init_db()
    restore_tunables()

    # One-time migrations
    migrate_to_dual_config()
    migrate_to_multi_asset()

    # Reset ETH/SOL configs to asset-specific defaults (fixes any BTC values that were incorrectly saved)
    reset_asset_specific_defaults()

    # Create per-asset alpha monitors
    for asset in SUPPORTED_ASSETS:
        alpha_monitors[asset] = AlphaMonitor(asset=asset)

    # Create 6 bot instances: 3 assets × 2 modes
    for asset in SUPPORTED_ASSETS:
        for mode in ("paper", "live"):
            key = (asset, mode)
            bot_configs[key] = BotConfig.load(mode, asset)
            bots[key] = TradingBot(
                alpha_monitor=alpha_monitors[asset],
                bot_config=bot_configs[key],
                mode=mode,
                asset=asset,
            )
            # Restore paper trading state (per-asset)
            if mode == "paper":
                bots[key]._restore_paper_state()

    # Start all alpha engines
    for asset, alpha in alpha_monitors.items():
        await alpha.start()

    # Auto-start bots if they were running before restart/deploy
    for asset in SUPPORTED_ASSETS:
        for mode in ("paper", "live"):
            key = (asset, mode)
            setting_key = f"{asset}_{mode}_bot_running"
            # Also check old key format for BTC (migration)
            if asset == "btc":
                old_key = f"{mode}_bot_running"
                if get_setting(setting_key) == "1" or get_setting(old_key) == "1":
                    bot_tasks[key] = asyncio.create_task(bots[key].run())
            elif get_setting(setting_key) == "1":
                bot_tasks[key] = asyncio.create_task(bots[key].run())

    yield

    # Shutdown: stop all bots and alpha monitors
    for key, bot in bots.items():
        if bot and bot.running:
            bot.stop()
            task = bot_tasks.get(key)
            if task and not task.done():
                task.cancel()

    for alpha in alpha_monitors.values():
        await alpha.stop()


app = FastAPI(title="Kalshi BTC Auto-Trader", lifespan=lifespan)


# ------------------------------------------------------------------
# API — consumed by React frontend & JSON clients
# ------------------------------------------------------------------

@app.get("/api/status")
async def api_status(asset: str = "btc", bot: str = "paper"):
    """Get status for specified asset and mode."""
    target_bot = get_bot(asset, bot)
    alpha = get_alpha(asset)
    if not target_bot:
        return {"error": f"Bot not initialized for {asset}/{bot}"}

    decision = target_bot.status.get("last_decision") or get_latest_decision()
    pos = target_bot.status.get("active_position")
    pos_label = "None"
    ticker = target_bot.status.get("current_market") or ""

    # Orderbook: REST API (2s cache) → WS → cycle cache
    # REST is primary because Kalshi WS orderbook_delta often stops sending updates
    ob_source = "cycle"
    live_ob = None
    if ticker:
        now = time.monotonic()
        if _ob_cache["ticker"] == ticker and (now - _ob_cache["ts"]) < _OB_CACHE_TTL and _ob_cache["data"]:
            live_ob = _ob_cache["data"]
            ob_source = "rest_cached"
        else:
            try:
                live_ob = await target_bot.fetch_orderbook(ticker)
                _ob_cache["ticker"] = ticker
                _ob_cache["data"] = live_ob
                _ob_cache["ts"] = now
                ob_source = "rest"
            except Exception:
                # REST failed — try WS as fallback
                live_ob = alpha.get_live_orderbook(ticker) if alpha and ticker else None
                if live_ob:
                    ob_source = "ws"

    if live_ob:
        yes_orders = live_ob.get("yes", []) if isinstance(live_ob.get("yes"), list) else []
        no_orders = live_ob.get("no", []) if isinstance(live_ob.get("no"), list) else []
        best_bid = max((p for p, q in yes_orders), default=0) if yes_orders else 0
        best_ask = (100 - max((p for p, q in no_orders), default=0)) if no_orders else 100
        ob_snapshot = {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": best_ask - best_bid,
            "yes_depth": sum(q for _, q in yes_orders),
            "no_depth": sum(q for _, q in no_orders),
            "source": ob_source,
        }
    else:
        ob_snapshot = target_bot.status.get("orderbook") or {}
        ob_snapshot["source"] = ob_source
        best_bid = ob_snapshot.get("best_bid", 0)
        best_ask = ob_snapshot.get("best_ask", 100)

    # Calculate fresh position P&L on every request using live orderbook
    position_pnl = 0.0
    position_pnl_pct = 0.0
    mark_to_market = 0
    if pos:
        pos_val = pos.get("position", 0) or 0
        exposure = pos.get("market_exposure", 0) or 0
        if pos_val > 0:
            pos_label = f"{pos_val}x YES (${exposure/100:.2f})"
        elif pos_val < 0:
            pos_label = f"{abs(pos_val)}x NO (${exposure/100:.2f})"

        if pos_val != 0 and best_bid > 0:
            if pos_val > 0:
                mark_to_market = best_bid * pos_val
            else:
                mark_to_market = (100 - best_ask) * abs(pos_val)
            position_pnl = (mark_to_market - exposure) / 100.0
            if exposure > 0:
                position_pnl_pct = (position_pnl / (exposure / 100.0)) * 100.0

    # Total account value: use backend-computed value, but update position MTM with live data
    balance_num = target_bot.status.get("balance", 0.0)
    total_account = target_bot.status.get("total_account_value", balance_num)
    # If we have fresher MTM from live orderbook, adjust total_account
    if live_ob and pos:
        cycle_mtm = target_bot.status.get("position_pnl", 0.0)  # from last cycle
        total_account = total_account - cycle_mtm + position_pnl

    start_bal = target_bot.status.get("start_balance")
    if start_bal is None:
        start_bal = target_bot.cfg.PAPER_STARTING_BALANCE if target_bot.paper_mode else (target_bot._start_balance or 100.0)

    # Get realized P&L from trades database (accurate across restarts)
    mode = "live" if not target_bot.paper_mode else "paper"
    trades_data = get_trades_with_pnl(mode=mode, asset=asset)
    realized_pnl = trades_data["summary"]["net_pnl"]

    # Total day_pnl = realized + unrealized position P&L
    live_day_pnl = realized_pnl + position_pnl

    return {
        "running": target_bot.status["running"],
        "balance": balance_num,
        "day_pnl": live_day_pnl,
        "position_pnl": position_pnl,
        "position_pnl_pct": position_pnl_pct,
        "total_account_value": total_account,
        "start_balance": start_bal,
        "position": pos_label,
        "active_position": pos,
        "market": ticker or "—",
        "last_action": target_bot.status.get("last_action", "Idle"),
        "cycle_count": target_bot.status["cycle_count"],
        "decision": decision.get("decision", "—") if decision else "—",
        "confidence": decision.get("confidence", 0) if decision else 0,
        "reasoning": decision.get("reasoning", "") if decision else "",
        "trading_enabled": target_bot.cfg.TRADING_ENABLED,
        "asset": asset,
        "mode": target_bot.mode,
        "paper_mode": target_bot.paper_mode,
        "alpha": alpha.get_status() if alpha else {},
        "alpha_override": target_bot.status.get("alpha_override"),
        "alpha_signal": target_bot.status.get("alpha_signal"),
        "alpha_signal_diff": target_bot.status.get("alpha_signal_diff"),
        "orderbook": ob_snapshot,
        "seconds_to_close": target_bot.status.get("seconds_to_close"),
        "strike_price": target_bot.status.get("strike_price"),
        "close_time": target_bot.status.get("close_time"),
        "market_title": target_bot.status.get("market_title"),
        "dashboard": _patch_dashboard(target_bot.status.get("dashboard"), best_bid, best_ask, target_bot.cfg),
    }


def _patch_dashboard(db: dict | None, best_bid: int, best_ask: int, cfg=None) -> dict | None:
    """Patch dashboard with live data so guards/exits always reflect current config."""
    if not db:
        return db
    # Use per-bot config if provided, otherwise fall back to global
    if cfg is None:
        cfg = config
    # Shallow copy to avoid mutating bot.status
    db = {**db}
    if db.get("guards"):
        guards = {**db["guards"]}
        spread_val = best_ask - best_bid
        if guards.get("spread"):
            guards["spread"] = {**guards["spread"], "value": spread_val, "blocked": spread_val > cfg.MAX_SPREAD_CENTS}
        db["guards"] = guards
    # Patch exit rule thresholds with current config values
    if db.get("exits"):
        exits = {**db["exits"]}
        if exits.get("stop_loss"):
            exits["stop_loss"] = {**exits["stop_loss"], "threshold": cfg.STOP_LOSS_CENTS}
        if exits.get("hit_and_run"):
            exits["hit_and_run"] = {**exits["hit_and_run"], "threshold": cfg.HIT_RUN_PCT, "enabled": cfg.HIT_RUN_PCT > 0}
        if exits.get("profit_take"):
            exits["profit_take"] = {**exits["profit_take"], "threshold": cfg.PROFIT_TAKE_PCT}
        if exits.get("free_roll"):
            exits["free_roll"] = {**exits["free_roll"], "threshold": cfg.FREE_ROLL_PRICE}
        if exits.get("edge_exit"):
            exits["edge_exit"] = {**exits["edge_exit"], "enabled": cfg.EDGE_EXIT_ENABLED, "min_hold": cfg.EDGE_EXIT_MIN_HOLD_SECS}
        db["exits"] = exits
    # Patch edge-exit config values
    db["edge_exit_enabled"] = cfg.EDGE_EXIT_ENABLED
    db["edge_exit_threshold"] = cfg.EDGE_EXIT_THRESHOLD_CENTS
    db["edge_exit_cooldown"] = cfg.EDGE_EXIT_COOLDOWN_SECS
    db["reentry_edge_premium"] = cfg.REENTRY_EDGE_PREMIUM
    return db


@app.get("/api/debug/market")
async def api_debug_market(asset: str = "btc", bot: str = "paper"):
    """Expose raw market data for debugging strike extraction."""
    target_bot = get_bot(asset, bot)
    if not target_bot:
        return {}
    return target_bot.status.get("_raw_market") or {}


@app.get("/api/logs")
async def api_logs(asset: str = ""):
    return get_recent_logs(80, asset=asset)


@app.get("/api/trades")
async def api_trades(mode: str = "", asset: str = ""):
    return get_trades_with_pnl(mode=mode, asset=asset)


@app.get("/api/analytics")
async def api_analytics(mode: str = "", asset: str = ""):
    from analytics import compute_analytics
    return compute_analytics(mode=mode, asset=asset)


class ApplySuggestionRequest(BaseModel):
    param: str
    value: float | int | bool | str


@app.post("/api/analytics/apply")
async def apply_suggestion(req: ApplySuggestionRequest, asset: str = "btc", bot: str = "paper"):
    """Apply analytics suggestion to a specific asset/mode bot config."""
    if req.param not in TUNABLE_FIELDS:
        return {"ok": False, "msg": f"Unknown parameter: {req.param}"}
    target_bot = get_bot(asset, bot)
    if not target_bot:
        return {"ok": False, "msg": f"Bot not initialized for {asset}/{bot}"}
    applied = target_bot.cfg.update({req.param: req.value})
    if applied:
        target_bot.cfg.save()  # Persist to DB
        from database import log_event
        log_event("CONFIG", f"[{asset.upper()}/{bot}] Analytics suggestion: {req.param} → {req.value}", asset)
        return {"ok": True, "applied": applied, "asset": asset, "bot": bot}
    return {"ok": False, "msg": "Failed to apply"}


@app.post("/api/backfill/settlements")
async def backfill_settlements(asset: str = "btc"):
    """One-time backfill: find all unsettled live trades and query Kalshi for results."""
    unsettled = get_all_unsettled_live_entries()
    if not unsettled:
        return {"ok": True, "msg": "No unsettled live trades found", "settled": 0}
    results = []
    # Use live bot for settlement (has Kalshi API access)
    live_bot = get_bot(asset, "live")
    if not live_bot:
        return {"ok": False, "msg": f"Live bot not initialized for {asset}"}
    for entry in unsettled:
        ticker = entry["market_id"]
        try:
            await live_bot._settle_live_positions(ticker)
            results.append({"ticker": ticker, "status": "settled"})
        except Exception as exc:
            results.append({"ticker": ticker, "status": "error", "error": str(exc)})
    # Also backfill BUY records so PnL round-trips work
    buy_backfilled = backfill_buy_trades_from_snapshots()
    return {
        "ok": True,
        "settled": len([r for r in results if r["status"] == "settled"]),
        "buy_backfilled": buy_backfilled,
        "results": results,
    }


@app.post("/api/backfill/buys")
async def backfill_buys():
    """Backfill missing BUY records in trades table from snapshots."""
    backfilled = backfill_buy_trades_from_snapshots()
    return {"ok": True, "backfilled": backfilled, "count": len(backfilled)}


@app.get("/api/kalshi/fills")
async def kalshi_fills(asset: str = "btc", since: str = ""):
    """Query Kalshi for actual fills since a given ISO timestamp."""
    params = {"limit": 100}
    if since:
        params["min_ts"] = since
    try:
        # Use live bot for Kalshi API access
        live_bot = get_bot(asset, "live")
        if not live_bot:
            return {"error": f"Live bot not initialized for {asset}"}
        data = await live_bot._get("/portfolio/fills", params=params)
        return data
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/api/reconcile")
async def reconcile_trades(asset: str = "btc", since_utc: str = "2026-02-01T00:00:00Z"):
    """Reconcile trade log with actual Kalshi fills.

    Fetches all fills from Kalshi, queries market results for expired markets,
    rebuilds the trades table with correct data.
    """
    from datetime import datetime, timezone
    from database import log_event
    from itertools import chain
    from config import ASSET_CONFIG

    live_bot = get_bot(asset, "live")
    if not live_bot:
        return {"error": f"Live bot not initialized for {asset}"}

    market_series = ASSET_CONFIG.get(asset, {}).get("market_series", "KXBTC15M")
    cutoff = datetime.fromisoformat(since_utc.replace("Z", "+00:00"))

    # 1. Fetch all Kalshi fills (paginated)
    all_fills = []
    cursor = None
    for _ in range(50):  # max 50 pages (5000 fills)
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            data = await live_bot._get("/portfolio/fills", params=params)
        except Exception as exc:
            return {"error": f"Failed to fetch fills: {exc}"}
        fills = data.get("fills", [])
        if not fills:
            break
        all_fills.extend(fills)
        cursor = data.get("cursor")
        # Stop if oldest fill is before cutoff
        oldest = datetime.fromisoformat(fills[-1]["created_time"].replace("Z", "+00:00"))
        if oldest < cutoff:
            break
        if not cursor:
            break

    # Filter to fills since cutoff, only for this asset's market series
    recent = [
        f for f in all_fills
        if f["ticker"].startswith(f"{market_series}-")
        and datetime.fromisoformat(f["created_time"].replace("Z", "+00:00")) >= cutoff
    ]

    # 2. Group fills by market
    markets: dict[str, list] = {}
    for f in recent:
        t = f["ticker"]
        if t not in markets:
            markets[t] = []
        markets[t].append(f)

    # 3. For each market: compute position, cost, fees, and query result
    results = []
    for ticker, fills in sorted(markets.items()):
        fills.sort(key=lambda x: x["created_time"])

        # Simple P&L model: track cash in (sells + settlement) vs cash out (buys)
        # Kalshi handles auto-netting internally, so we just track net position
        total_cost_cents = 0  # money spent on buys
        total_sell_revenue_cents = 0  # money received from sells
        total_fees_cents = 0  # fees paid
        yes_position = 0  # net YES contracts held
        no_position = 0   # net NO contracts held

        for f in fills:
            count = f["count"]
            # Track fees (fee_cost is a string like "0.0000")
            fee_str = f.get("fee_cost", "0")
            fee_dollars = float(fee_str) if fee_str else 0
            total_fees_cents += fee_dollars * 100

            if f["action"] == "buy":
                if f["side"] == "yes":
                    total_cost_cents += count * f["yes_price"]
                    yes_position += count
                else:
                    total_cost_cents += count * f["no_price"]
                    no_position += count
            elif f["action"] == "sell":
                # Sell revenue: for YES sells, get yes_price; for NO sells, get no_price
                if f["side"] == "yes":
                    total_sell_revenue_cents += count * f["yes_price"]
                    yes_position -= count
                else:
                    total_sell_revenue_cents += count * f["no_price"]
                    no_position -= count

        # Query Kalshi for market result
        market_result = ""
        try:
            mkt_data = await live_bot._get(f"/markets/{ticker}")
            market_data = mkt_data.get("market", mkt_data)
            market_result = market_data.get("result", "")
        except Exception:
            pass

        # Settlement payout for remaining position
        settle_cents = 0
        if market_result:
            yes_wins = market_result.lower() == "yes"
            # YES position pays 100c if YES wins, 0 if NO wins
            if yes_position > 0:
                settle_cents += yes_position * (100 if yes_wins else 0)
            # NO position pays 100c if NO wins, 0 if YES wins
            if no_position > 0:
                settle_cents += no_position * (100 if not yes_wins else 0)

        total_revenue_cents = total_sell_revenue_cents + settle_cents
        # P&L excludes fees (fees tracked separately)
        pnl_cents = total_revenue_cents - total_cost_cents

        # Determine primary side and average entry
        yes_bought = sum(f["count"] for f in fills if f["action"] == "buy" and f["side"] == "yes")
        no_bought = sum(f["count"] for f in fills if f["action"] == "buy" and f["side"] == "no")
        primary_side = "yes" if yes_bought >= no_bought else "no"

        if primary_side == "yes" and yes_bought > 0:
            avg_entry_cents = sum(f["count"] * f["yes_price"] for f in fills if f["action"] == "buy" and f["side"] == "yes") / yes_bought
        elif no_bought > 0:
            avg_entry_cents = sum(f["count"] * f["no_price"] for f in fills if f["action"] == "buy" and f["side"] == "no") / no_bought
        else:
            avg_entry_cents = 0

        results.append({
            "ticker": ticker,
            "primary_side": primary_side,
            "buys": {"yes": yes_bought, "no": no_bought, "total_cost_cents": total_cost_cents},
            "sells": {"yes": sum(f["count"] for f in fills if f["action"] == "sell" and f["side"] == "yes"),
                      "no": sum(f["count"] for f in fills if f["action"] == "sell" and f["side"] == "no"),
                      "revenue_cents": total_sell_revenue_cents},
            "remaining": {"yes": max(0, yes_position), "no": max(0, no_position)},
            "result": market_result,
            "settle_cents": settle_cents,
            "total_revenue_cents": total_revenue_cents,
            "fees_cents": total_fees_cents,
            "pnl_cents": pnl_cents,
            "avg_entry_cents": round(avg_entry_cents, 1),
        })

    # 4. Rebuild trades table for live mode
    with get_db() as conn:
        # Clear all live trades
        conn.execute("DELETE FROM trades WHERE market_id NOT LIKE '[PAPER]%'")

        for mkt in results:
            ticker = mkt["ticker"]
            side = mkt["primary_side"]

            # Get fills for this market to record individual buys
            mkt_fills = markets[ticker]
            for f in sorted(mkt_fills, key=lambda x: x["created_time"]):
                if f["action"] == "buy":
                    fill_side = f["side"]
                    fill_price = f["yes_price"] / 100.0 if fill_side == "yes" else f["no_price"] / 100.0
                    conn.execute(
                        "INSERT INTO trades (ts, market_id, side, action, price, quantity, order_id) "
                        "VALUES (?, ?, ?, 'BUY', ?, ?, ?)",
                        (f["created_time"], ticker, fill_side, fill_price, f["count"], f["order_id"]),
                    )
                elif f["action"] == "sell":
                    fill_side = f["side"]
                    # Revenue: use yes_price for YES sells, for NO sells use (100-no_price)/100 = yes_price/100
                    fill_price = f["yes_price"] / 100.0
                    conn.execute(
                        "INSERT INTO trades (ts, market_id, side, action, price, quantity, order_id) "
                        "VALUES (?, ?, ?, 'SELL', ?, ?, ?)",
                        (f["created_time"], ticker, fill_side, fill_price, f["count"], f["order_id"]),
                    )

            # Add SETTLE entry for remaining position
            if mkt["result"] and (mkt["remaining"]["yes"] > 0 or mkt["remaining"]["no"] > 0):
                # Extract settle time from ticker (e.g., KXBTC15M-26FEB030900-00 -> Feb 3 09:00 ET 2026)
                # Kalshi uses Eastern time for contract times
                settle_ts = datetime.now(timezone.utc).isoformat()  # fallback
                try:
                    import re
                    from zoneinfo import ZoneInfo
                    # Format: -YYMMMDDHHNN- where YY=year, MMM=month, DD=day, HH=hour, NN=minute
                    match = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-", ticker)
                    if match:
                        yr, mon_str, day, hr, mn = match.groups()
                        months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                                  "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
                        mon = months.get(mon_str, 1)
                        year = 2000 + int(yr)
                        # Parse as Eastern time, store in ISO format (will be converted to Pacific for display)
                        et = ZoneInfo("America/New_York")
                        settle_dt = datetime(year, mon, int(day), int(hr), int(mn), 0, tzinfo=et)
                        settle_ts = settle_dt.isoformat()
                except Exception:
                    pass

                if mkt["remaining"]["yes"] > 0:
                    settle_price = 1.0 if mkt["result"].lower() == "yes" else 0.0
                    conn.execute(
                        "INSERT INTO trades (ts, market_id, side, action, price, quantity, order_id) "
                        "VALUES (?, ?, 'yes', 'SETTLE', ?, ?, ?)",
                        (settle_ts, ticker, settle_price,
                         mkt["remaining"]["yes"], f"reconcile-settle-{ticker}"),
                    )
                if mkt["remaining"]["no"] > 0:
                    settle_price = 1.0 if mkt["result"].lower() == "no" else 0.0
                    conn.execute(
                        "INSERT INTO trades (ts, market_id, side, action, price, quantity, order_id) "
                        "VALUES (?, ?, 'no', 'SETTLE', ?, ?, ?)",
                        (settle_ts, ticker, settle_price,
                         mkt["remaining"]["no"], f"reconcile-settle-{ticker}"),
                    )

    # 5. Store actual Kalshi P&L and breakdown for each market
    for mkt in results:
        set_live_market_pnl(
            mkt["ticker"],
            mkt["pnl_cents"],
            mkt["result"],
            total_cost_cents=mkt["buys"]["total_cost_cents"],
            total_revenue_cents=mkt["total_revenue_cents"],
            fees_cents=mkt["fees_cents"],
        )

    log_event("RECONCILE", f"Reconciled {len(results)} markets from Kalshi fills", asset)

    # Summary
    total_pnl = sum(m["pnl_cents"] for m in results if m["result"]) / 100.0
    settled_count = sum(1 for m in results if m["result"])
    open_count = sum(1 for m in results if not m["result"])

    return {
        "ok": True,
        "total_markets": len(results),
        "settled": settled_count,
        "open": open_count,
        "total_pnl": round(total_pnl, 2),
        "markets": results,
    }


# ------------------------------------------------------------------
# Controls
# ------------------------------------------------------------------

@app.post("/api/start")
async def start_bot(asset: str = "btc", bot: str = "paper"):
    """Start the specified bot (asset + mode)."""
    global bot_tasks
    target_bot = get_bot(asset, bot)
    if not target_bot:
        return {"ok": False, "msg": f"Bot not initialized for {asset}/{bot}"}
    if target_bot.running:
        return {"ok": False, "msg": f"{asset.upper()} {bot.capitalize()} bot already running"}

    key = (asset, bot)
    task = asyncio.create_task(target_bot.run())
    bot_tasks[key] = task
    set_setting(f"{asset}_{bot}_bot_running", "1")
    return {"ok": True, "asset": asset, "bot": bot}


@app.post("/api/stop")
async def stop_bot(asset: str = "btc", bot: str = "paper"):
    """Stop the specified bot (asset + mode)."""
    target_bot = get_bot(asset, bot)
    if not target_bot:
        return {"ok": False, "msg": f"Bot not initialized for {asset}/{bot}"}
    if not target_bot.running:
        return {"ok": False, "msg": f"{asset.upper()} {bot.capitalize()} bot not running"}

    target_bot.stop()
    set_setting(f"{asset}_{bot}_bot_running", "0")
    return {"ok": True, "asset": asset, "bot": bot}


@app.post("/api/paper/reset")
async def reset_paper(asset: str = "btc"):
    """Reset paper trading state (balance, positions) for specified asset."""
    paper_bot = get_bot(asset, "paper")
    if not paper_bot:
        return {"ok": False, "msg": f"Paper bot not initialized for {asset}"}
    was_running = paper_bot.running
    if was_running:
        paper_bot.stop()
        set_setting(f"{asset}_paper_bot_running", "0")
        await asyncio.sleep(1)
    paper_bot.reset_paper_trading()
    return {"ok": True, "asset": asset, "balance": paper_bot.cfg.PAPER_STARTING_BALANCE}


@app.get("/api/status/both")
async def api_status_both(asset: str = "btc"):
    """Get status for both paper and live bots for specified asset."""
    paper_status = await api_status(asset=asset, bot="paper")
    live_status = await api_status(asset=asset, bot="live")
    return {
        "paper": paper_status,
        "live": live_status,
    }


@app.get("/api/status/all")
async def api_status_all():
    """Get status for all 6 bots (3 assets × 2 modes)."""
    result = {}
    for asset in SUPPORTED_ASSETS:
        result[asset] = {
            "paper": await api_status(asset=asset, bot="paper"),
            "live": await api_status(asset=asset, bot="live"),
        }
    return result


class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat")
async def chat(req: ChatRequest, asset: str = "btc", bot: str = "paper"):
    """Chat with the specified bot's agent."""
    target_bot = get_bot(asset, bot)
    if not target_bot:
        return {"reply": f"Bot not initialized for {asset}/{bot}"}
    reply = await target_bot.agent.chat(req.message, target_bot.status)
    return {"reply": reply}


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

@app.get("/api/config")
async def get_config(asset: str = "btc", bot: str = "paper"):
    """Get config for specified asset and mode."""
    target_bot = get_bot(asset, bot)
    if not target_bot:
        return {"error": f"Bot not initialized for {asset}/{bot}"}
    values = target_bot.cfg.get_all()
    meta = {k: {**TUNABLE_FIELDS[k], "value": values[k]} for k in TUNABLE_FIELDS}
    return meta


@app.post("/api/config")
async def update_config(updates: dict, asset: str = "btc", bot: str = "paper"):
    """Update config for specified asset and mode."""
    target_bot = get_bot(asset, bot)
    if not target_bot:
        return {"ok": False, "msg": f"Bot not initialized for {asset}/{bot}"}
    applied = target_bot.cfg.update(updates)
    target_bot.cfg.save()  # Persist to DB
    from database import log_event
    for k, v in applied.items():
        log_event("CONFIG", f"[{asset.upper()}/{bot}] {k} → {v}", asset)
    return {"ok": True, "applied": applied, "asset": asset, "bot": bot}


# ------------------------------------------------------------------
# Frontend serving
# ------------------------------------------------------------------

if FRONTEND_DIR.exists():
    # Serve built React app
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="static")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = FRONTEND_DIR / full_path
        if full_path and file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIR / "index.html")
else:
    # Fallback: serve old Jinja2 template if frontend not built
    templates = Jinja2Templates(directory="templates")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("index.html", {"request": request})
