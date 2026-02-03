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

# Simple TTL cache for REST orderbook fetches (avoids hammering Kalshi API)
_ob_cache: dict = {"ticker": "", "data": None, "ts": 0.0}
_OB_CACHE_TTL = 2.0  # seconds
from config import get_tunables, set_tunables, restore_tunables, TUNABLE_FIELDS
from database import init_db, get_recent_logs, get_latest_decision, get_todays_trades, get_trades_with_pnl, get_setting, set_setting, get_all_unsettled_live_entries, backfill_buy_trades_from_snapshots, get_db
from alpha_engine import AlphaMonitor
from trader import TradingBot

FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"

alpha_monitor = AlphaMonitor()
bot = TradingBot(alpha_monitor=alpha_monitor)
bot_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    restore_tunables()
    # Restore paper trading state (balance, positions) from DB
    if bot.paper_mode:
        bot._restore_paper_state()
    await alpha_monitor.start()
    # Auto-start bot if it was running before restart/deploy
    if get_setting("bot_running") == "1":
        global bot_task
        bot_task = asyncio.create_task(bot.run())
    yield
    # Shutdown: stop bot if running
    if bot.running:
        bot.stop()
        if bot_task and not bot_task.done():
            bot_task.cancel()
    await alpha_monitor.stop()


app = FastAPI(title="Kalshi BTC Auto-Trader", lifespan=lifespan)


# ------------------------------------------------------------------
# API — consumed by React frontend & JSON clients
# ------------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    decision = bot.status.get("last_decision") or get_latest_decision()
    pos = bot.status.get("active_position")
    pos_label = "None"
    ticker = bot.status.get("current_market") or ""

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
                live_ob = await bot.fetch_orderbook(ticker)
                _ob_cache["ticker"] = ticker
                _ob_cache["data"] = live_ob
                _ob_cache["ts"] = now
                ob_source = "rest"
            except Exception:
                # REST failed — try WS as fallback
                live_ob = alpha_monitor.get_live_orderbook(ticker) if ticker else None
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
        ob_snapshot = bot.status.get("orderbook") or {}
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
    balance_num = bot.status.get("balance", 0.0)
    total_account = bot.status.get("total_account_value", balance_num)
    # If we have fresher MTM from live orderbook, adjust total_account
    if live_ob and pos:
        cycle_mtm = bot.status.get("position_pnl", 0.0)  # from last cycle
        total_account = total_account - cycle_mtm + position_pnl

    start_bal = bot.status.get("start_balance")
    if start_bal is None:
        start_bal = config.PAPER_STARTING_BALANCE if bot.paper_mode else (bot._start_balance or 100.0)

    day_pnl = bot.status.get("day_pnl", 0.0)
    # Refresh day_pnl with live position P&L
    cycle_pos_pnl = bot.status.get("position_pnl", 0.0)
    live_day_pnl = day_pnl - cycle_pos_pnl + position_pnl

    return {
        "running": bot.status["running"],
        "balance": balance_num,
        "day_pnl": live_day_pnl,
        "position_pnl": position_pnl,
        "position_pnl_pct": position_pnl_pct,
        "total_account_value": total_account,
        "start_balance": start_bal,
        "position": pos_label,
        "active_position": pos,
        "market": ticker or "—",
        "last_action": bot.status.get("last_action", "Idle"),
        "cycle_count": bot.status["cycle_count"],
        "decision": decision.get("decision", "—") if decision else "—",
        "confidence": decision.get("confidence", 0) if decision else 0,
        "reasoning": decision.get("reasoning", "") if decision else "",
        "trading_enabled": config.TRADING_ENABLED,
        "env": config.KALSHI_ENV,
        "paper_mode": config.KALSHI_ENV == "demo",
        "alpha": alpha_monitor.get_status(),
        "alpha_override": bot.status.get("alpha_override"),
        "alpha_signal": bot.status.get("alpha_signal"),
        "alpha_signal_diff": bot.status.get("alpha_signal_diff"),
        "orderbook": ob_snapshot,
        "seconds_to_close": bot.status.get("seconds_to_close"),
        "strike_price": bot.status.get("strike_price"),
        "close_time": bot.status.get("close_time"),
        "market_title": bot.status.get("market_title"),
        "dashboard": _patch_dashboard(bot.status.get("dashboard"), best_bid, best_ask),
    }


def _patch_dashboard(db: dict | None, best_bid: int, best_ask: int) -> dict | None:
    """Patch dashboard with live data so guards/exits always reflect current config."""
    if not db:
        return db
    # Shallow copy to avoid mutating bot.status
    db = {**db}
    if db.get("guards"):
        guards = {**db["guards"]}
        spread_val = best_ask - best_bid
        if guards.get("spread"):
            guards["spread"] = {**guards["spread"], "value": spread_val, "blocked": spread_val > config.MAX_SPREAD_CENTS}
        db["guards"] = guards
    # Patch exit rule thresholds with current config values
    if db.get("exits"):
        exits = {**db["exits"]}
        if exits.get("stop_loss"):
            exits["stop_loss"] = {**exits["stop_loss"], "threshold": config.STOP_LOSS_CENTS}
        if exits.get("hit_and_run"):
            exits["hit_and_run"] = {**exits["hit_and_run"], "threshold": config.HIT_RUN_PCT, "enabled": config.HIT_RUN_PCT > 0}
        if exits.get("profit_take"):
            exits["profit_take"] = {**exits["profit_take"], "threshold": config.PROFIT_TAKE_PCT}
        if exits.get("free_roll"):
            exits["free_roll"] = {**exits["free_roll"], "threshold": config.FREE_ROLL_PRICE}
        if exits.get("edge_exit"):
            exits["edge_exit"] = {**exits["edge_exit"], "enabled": config.EDGE_EXIT_ENABLED, "min_hold": config.EDGE_EXIT_MIN_HOLD_SECS}
        db["exits"] = exits
    # Patch edge-exit config values
    db["edge_exit_enabled"] = config.EDGE_EXIT_ENABLED
    db["edge_exit_threshold"] = config.EDGE_EXIT_THRESHOLD_CENTS
    db["edge_exit_cooldown"] = config.EDGE_EXIT_COOLDOWN_SECS
    db["reentry_edge_premium"] = config.REENTRY_EDGE_PREMIUM
    return db


@app.get("/api/debug/market")
async def api_debug_market():
    """Expose raw market data for debugging strike extraction."""
    return bot.status.get("_raw_market") or {}


@app.get("/api/logs")
async def api_logs():
    return get_recent_logs(80)


@app.get("/api/trades")
async def api_trades(mode: str = ""):
    return get_trades_with_pnl(mode=mode)


@app.get("/api/analytics")
async def api_analytics(mode: str = ""):
    from analytics import compute_analytics
    return compute_analytics(mode=mode)


class ApplySuggestionRequest(BaseModel):
    param: str
    value: float | int | bool | str


@app.post("/api/analytics/apply")
async def apply_suggestion(req: ApplySuggestionRequest):
    if req.param not in TUNABLE_FIELDS:
        return {"ok": False, "msg": f"Unknown parameter: {req.param}"}
    applied = set_tunables({req.param: req.value})
    if applied:
        from database import log_event
        log_event("CONFIG", f"Analytics suggestion applied: {req.param} → {req.value}")
        return {"ok": True, "applied": applied}
    return {"ok": False, "msg": "Failed to apply"}


@app.post("/api/backfill/settlements")
async def backfill_settlements():
    """One-time backfill: find all unsettled live trades and query Kalshi for results."""
    unsettled = get_all_unsettled_live_entries()
    if not unsettled:
        return {"ok": True, "msg": "No unsettled live trades found", "settled": 0}
    results = []
    for entry in unsettled:
        ticker = entry["market_id"]
        try:
            await bot._settle_live_positions(ticker)
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
async def kalshi_fills(since: str = ""):
    """Query Kalshi for actual fills since a given ISO timestamp."""
    params = {"limit": 100}
    if since:
        params["min_ts"] = since
    try:
        data = await bot._get("/portfolio/fills", params=params)
        return data
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/api/reconcile")
async def reconcile_trades(since_utc: str = "2026-02-02T20:00:00Z"):
    """Reconcile trade log with actual Kalshi fills.

    Fetches all fills from Kalshi, queries market results for expired markets,
    rebuilds the trades table with correct data.
    """
    from datetime import datetime, timezone
    from database import log_event
    from itertools import chain
    cutoff = datetime.fromisoformat(since_utc.replace("Z", "+00:00"))

    # 1. Fetch all Kalshi fills (paginated)
    all_fills = []
    cursor = None
    for _ in range(10):  # max 10 pages
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            data = await bot._get("/portfolio/fills", params=params)
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

    # Filter to fills since cutoff, only KXBTC15M markets
    recent = [
        f for f in all_fills
        if f["ticker"].startswith("KXBTC15M-")
        and datetime.fromisoformat(f["created_time"].replace("Z", "+00:00")) >= cutoff
    ]

    # 2. Group fills by market
    markets: dict[str, list] = {}
    for f in recent:
        t = f["ticker"]
        if t not in markets:
            markets[t] = []
        markets[t].append(f)

    # 3. For each market: compute position, cost, and query result
    results = []
    for ticker, fills in sorted(markets.items()):
        fills.sort(key=lambda x: x["created_time"])

        # Track position and cash flows
        yes_bought = 0
        no_bought = 0
        total_cost_cents = 0  # cash out
        sell_fills_detail = []

        for f in fills:
            if f["action"] == "buy":
                if f["side"] == "yes":
                    yes_bought += f["count"]
                    total_cost_cents += f["count"] * f["yes_price"]
                else:
                    no_bought += f["count"]
                    total_cost_cents += f["count"] * f["no_price"]
            elif f["action"] == "sell":
                sell_fills_detail.append(f)

        # Process sells: track what was sold on each side
        yes_sold = sum(f["count"] for f in sell_fills_detail if f["side"] == "yes")
        no_sold = sum(f["count"] for f in sell_fills_detail if f["side"] == "no")

        # Revenue from sells: on Kalshi, sells are reported at the fill price
        # For "sell NO" when holding YES: auto-netting means revenue = (100 - no_price) per contract
        # For "sell YES": revenue = yes_price per contract
        # The simplest correct model: sell revenue = yes_price * count for ALL sells
        # because yes_price reflects the YES-equivalent value in every fill
        sell_revenue_cents = sum(f["count"] * f["yes_price"] for f in sell_fills_detail)

        # Remaining position after sells (with auto-netting)
        # Sell YES reduces YES. Sell NO auto-nets with YES (or creates short NO).
        yes_remaining = yes_bought - yes_sold
        # NO sold when holding YES = auto-netted with YES
        auto_netted = min(no_sold, max(yes_remaining, 0))
        yes_remaining -= auto_netted
        # Any excess NO sold beyond netting = short NO
        short_no = no_sold - auto_netted

        # NO remaining from NO buys (less any YES sells that netted)
        no_remaining = no_bought
        if yes_sold > yes_bought:
            # Sold more YES than bought = sold YES to net with NO
            excess_yes_sold = yes_sold - yes_bought
            yes_net_with_no = min(excess_yes_sold, no_remaining)
            no_remaining -= yes_net_with_no

        # Query Kalshi for market result
        market_result = ""
        try:
            mkt_data = await bot._get(f"/markets/{ticker}")
            market_data = mkt_data.get("market", mkt_data)
            market_result = market_data.get("result", "")
        except Exception:
            pass

        # Compute settlement value for remaining positions
        settle_cents = 0
        if market_result:
            if market_result.lower() == "yes":
                settle_cents += yes_remaining * 100  # YES pays 100c
                settle_cents += no_remaining * 0     # NO pays 0
                settle_cents -= short_no * 100       # Short NO costs 100c when YES wins
                # Actually short NO at settlement: bot owes 0 (NO is worthless when YES wins)
                settle_cents += short_no * 0  # short NO costs 0 when YES wins (NO=0, nothing owed)
                # Correction: re-compute
                settle_cents = yes_remaining * 100 + no_remaining * 0
                # Short NO when YES wins: the NO is worthless, short expires worthless → no cost
            elif market_result.lower() == "no":
                settle_cents = yes_remaining * 0 + no_remaining * 100
                # Short NO when NO wins: owe 100c per contract
                settle_cents -= short_no * 100
        else:
            # Market still open - remaining position unsettled
            settle_cents = 0

        total_revenue_cents = sell_revenue_cents + settle_cents
        pnl_cents = total_revenue_cents - total_cost_cents

        # Effective entry (weighted average cost per contract on the primary side)
        primary_side = "yes" if yes_bought >= no_bought else "no"
        if primary_side == "yes" and yes_bought > 0:
            avg_entry_cents = sum(f["count"] * f["yes_price"] for f in fills if f["action"] == "buy" and f["side"] == "yes") / yes_bought
        elif no_bought > 0:
            avg_entry_cents = sum(f["count"] * f["no_price"] for f in fills if f["action"] == "buy" and f["side"] == "no") / no_bought
        else:
            avg_entry_cents = 0

        total_qty = yes_bought + no_bought
        exit_qty = yes_sold + no_sold
        settled_qty = yes_remaining + no_remaining

        results.append({
            "ticker": ticker,
            "primary_side": primary_side,
            "buys": {"yes": yes_bought, "no": no_bought, "total_cost_cents": total_cost_cents},
            "sells": {"yes": yes_sold, "no": no_sold, "revenue_cents": sell_revenue_cents},
            "remaining": {"yes": yes_remaining, "no": no_remaining, "short_no": short_no},
            "result": market_result,
            "settle_cents": settle_cents,
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
                if mkt["remaining"]["yes"] > 0:
                    settle_price = 1.0 if mkt["result"].lower() == "yes" else 0.0
                    conn.execute(
                        "INSERT INTO trades (ts, market_id, side, action, price, quantity, order_id) "
                        "VALUES (?, ?, 'yes', 'SETTLE', ?, ?, ?)",
                        (datetime.now(timezone.utc).isoformat(), ticker, settle_price,
                         mkt["remaining"]["yes"], f"reconcile-settle-{ticker}"),
                    )
                if mkt["remaining"]["no"] > 0:
                    settle_price = 1.0 if mkt["result"].lower() == "no" else 0.0
                    conn.execute(
                        "INSERT INTO trades (ts, market_id, side, action, price, quantity, order_id) "
                        "VALUES (?, ?, 'no', 'SETTLE', ?, ?, ?)",
                        (datetime.now(timezone.utc).isoformat(), ticker, settle_price,
                         mkt["remaining"]["no"], f"reconcile-settle-{ticker}"),
                    )

    log_event("RECONCILE", f"Reconciled {len(results)} markets from Kalshi fills")

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
async def start_bot():
    global bot_task
    if bot.running:
        return {"ok": False, "msg": "Already running"}
    bot_task = asyncio.create_task(bot.run())
    set_setting("bot_running", "1")
    return {"ok": True}


@app.post("/api/stop")
async def stop_bot():
    if not bot.running:
        return {"ok": False, "msg": "Not running"}
    bot.stop()
    set_setting("bot_running", "0")
    return {"ok": True}


@app.post("/api/paper/reset")
async def reset_paper():
    if not bot.paper_mode:
        return {"ok": False, "msg": "Not in paper mode"}
    was_running = bot.running
    if was_running:
        bot.stop()
        set_setting("bot_running", "0")
        await asyncio.sleep(1)
    bot.reset_paper_trading()
    return {"ok": True, "balance": config.PAPER_STARTING_BALANCE}


class EnvRequest(BaseModel):
    env: str


@app.post("/api/env")
async def switch_env(req: EnvRequest):
    if req.env not in ("demo", "live"):
        return {"ok": False, "msg": "Invalid env"}
    if bot.running:
        bot.stop()
    await bot.switch_environment(req.env)
    return {"ok": True, "env": req.env}


class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat")
async def chat(req: ChatRequest):
    reply = await bot.agent.chat(req.message, bot.status)
    return {"reply": reply}


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

@app.get("/api/config")
async def get_config():
    values = get_tunables()
    meta = {k: {**TUNABLE_FIELDS[k], "value": values[k]} for k in TUNABLE_FIELDS}
    return meta


@app.post("/api/config")
async def update_config(updates: dict):
    applied = set_tunables(updates)
    from database import log_event
    for k, v in applied.items():
        log_event("CONFIG", f"{k} → {v}")
    return {"ok": True, "applied": applied}


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
