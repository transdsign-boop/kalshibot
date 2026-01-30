import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import config
from config import get_tunables, set_tunables, restore_tunables, TUNABLE_FIELDS
from database import init_db, get_recent_logs, get_latest_decision, get_todays_trades
from alpha_engine import AlphaMonitor
from trader import TradingBot

alpha_monitor = AlphaMonitor()
bot = TradingBot(alpha_monitor=alpha_monitor)
bot_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    restore_tunables()
    await alpha_monitor.start()
    yield
    # Shutdown: stop bot if running
    if bot.running:
        bot.stop()
        if bot_task and not bot_task.done():
            bot_task.cancel()
    await alpha_monitor.stop()


app = FastAPI(title="Kalshi BTC Auto-Trader", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ------------------------------------------------------------------
# API — consumed by HTMX partials & JSON clients
# ------------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    decision = bot.status.get("last_decision") or get_latest_decision()
    pos = bot.status.get("active_position")
    pos_label = "None"
    if pos:
        yes_q = pos.get("yes_quantity", pos.get("position", 0))
        no_q = pos.get("no_quantity", 0)
        if yes_q:
            avg = pos.get("yes_average_price", 0)
            pos_label = f"{yes_q}x YES @ {avg}"
        elif no_q:
            avg = pos.get("no_average_price", 0)
            pos_label = f"{no_q}x NO @ {avg}"

    return {
        "running": bot.status["running"],
        "balance": f"${bot.status['balance']:.2f}",
        "day_pnl": bot.status.get("day_pnl", 0.0),
        "position_pnl": bot.status.get("position_pnl", 0.0),
        "position": pos_label,
        "market": bot.status.get("current_market") or "—",
        "last_action": bot.status.get("last_action", "Idle"),
        "cycle_count": bot.status["cycle_count"],
        "decision": decision.get("decision", "—") if decision else "—",
        "confidence": decision.get("confidence", 0) if decision else 0,
        "reasoning": decision.get("reasoning", "") if decision else "",
        "trading_enabled": config.TRADING_ENABLED,
        "env": config.KALSHI_ENV,
        "alpha": alpha_monitor.get_status(),
        "alpha_override": bot.status.get("alpha_override"),
        "orderbook": bot.status.get("orderbook"),
    }


@app.get("/api/logs")
async def api_logs():
    return get_recent_logs(80)


# ------------------------------------------------------------------
# Controls
# ------------------------------------------------------------------

@app.post("/api/start")
async def start_bot():
    global bot_task
    if bot.running:
        return {"ok": False, "msg": "Already running"}
    bot_task = asyncio.create_task(bot.run())
    return {"ok": True}


@app.post("/api/stop")
async def stop_bot():
    if not bot.running:
        return {"ok": False, "msg": "Not running"}
    bot.stop()
    return {"ok": True}


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
# Helpers
# ------------------------------------------------------------------

def _compute_day_pnl() -> str:
    trades = get_todays_trades()
    total = 0.0
    for t in trades:
        # Simplified: sum of prices for sells minus buys
        if t["action"] == "SELL":
            total += t["price"] * t["quantity"]
        else:
            total -= t["price"] * t["quantity"]
    sign = "+" if total >= 0 else ""
    return f"{sign}${total:.2f}"
