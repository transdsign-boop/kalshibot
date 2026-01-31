import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import config
from config import get_tunables, set_tunables, restore_tunables, TUNABLE_FIELDS
from database import init_db, get_recent_logs, get_latest_decision, get_todays_trades, get_trades_with_pnl, get_setting, set_setting
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
    if pos:
        pos_val = pos.get("position", 0) or 0
        exposure = pos.get("market_exposure", 0) or 0
        if pos_val > 0:
            pos_label = f"{pos_val}x YES (${exposure/100:.2f})"
        elif pos_val < 0:
            pos_label = f"{abs(pos_val)}x NO (${exposure/100:.2f})"

    return {
        "running": bot.status["running"],
        "balance": f"${bot.status['balance']:.2f}",
        "day_pnl": bot.status.get("day_pnl", 0.0),
        "position_pnl": bot.status.get("position_pnl", 0.0),
        "position": pos_label,
        "active_position": pos,
        "market": bot.status.get("current_market") or "—",
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
        "orderbook": bot.status.get("orderbook"),
        "seconds_to_close": bot.status.get("seconds_to_close"),
        "strike_price": bot.status.get("strike_price"),
        "close_time": bot.status.get("close_time"),
        "market_title": bot.status.get("market_title"),
    }


@app.get("/api/debug/market")
async def api_debug_market():
    """Expose raw market data for debugging strike extraction."""
    return bot.status.get("_raw_market") or {}


@app.get("/api/logs")
async def api_logs():
    return get_recent_logs(80)


@app.get("/api/trades")
async def api_trades():
    return get_trades_with_pnl(100)


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
