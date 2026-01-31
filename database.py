import os
import sqlite3
import json
from datetime import datetime, timezone
from contextlib import contextmanager

# Use persistent volume on Fly.io (/data), fall back to local for dev
_VOLUME_DIR = "/data"
if os.path.isdir(_VOLUME_DIR):
    DB_PATH = os.path.join(_VOLUME_DIR, "kalshibot.db")
else:
    DB_PATH = "kalshibot.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                market_id   TEXT NOT NULL,
                side        TEXT NOT NULL,
                action      TEXT NOT NULL,
                price       REAL NOT NULL,
                quantity    INTEGER NOT NULL,
                order_id    TEXT,
                status      TEXT DEFAULT 'placed'
            );
            CREATE TABLE IF NOT EXISTS logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                level       TEXT NOT NULL,
                message     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_decisions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                market_id   TEXT,
                decision    TEXT NOT NULL,
                confidence  REAL NOT NULL,
                reasoning   TEXT NOT NULL,
                executed    INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)


def log_event(level: str, message: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO logs (ts, level, message) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), level, message),
        )


def record_trade(market_id: str, side: str, action: str, price: float,
                  quantity: int, order_id: str | None = None, exit_type: str | None = None):
    """Record a trade with optional exit type (SL, TP, SETTLE for sell actions)."""
    # For sell actions, use exit_type in the action field for better labeling
    if action in ("SELL", "SETTLED") and exit_type:
        action = exit_type
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (ts, market_id, side, action, price, quantity, order_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), market_id, side, action,
             price, quantity, order_id),
        )


def record_decision(market_id: str | None, decision: str, confidence: float,
                     reasoning: str, executed: bool = False):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_decisions (ts, market_id, decision, confidence, reasoning, executed) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), market_id, decision,
             confidence, reasoning, int(executed)),
        )


def get_recent_logs(limit: int = 50) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ts, level, message FROM logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_trades(limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_decision() -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM agent_decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_todays_trades() -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE ts LIKE ? ORDER BY id DESC",
            (f"{today}%",),
        ).fetchall()
    return [dict(r) for r in rows]


def get_trades_with_pnl(limit: int = 50) -> dict:
    """Return recent trades with per-market P&L and summary stats."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ts, market_id, side, action, price, quantity FROM trades ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    trades = [dict(r) for r in rows]

    # Group by market_id to compute round-trip P&L
    markets: dict[str, dict] = {}
    for t in trades:
        mid = t["market_id"]
        if mid not in markets:
            markets[mid] = {"buy_cost": 0.0, "sell_proceeds": 0.0, "has_buy": False, "has_sell": False}
        m = markets[mid]
        cost = t["price"] * t["quantity"]
        if t["action"] == "BUY":
            m["buy_cost"] += cost
            m["has_buy"] = True
        elif t["action"] in ("SELL", "SETTLED"):
            m["sell_proceeds"] += cost
            m["has_sell"] = True

    # Compute summary
    wins = 0
    losses = 0
    pending = 0
    net_pnl = 0.0
    market_pnl: dict[str, float | None] = {}

    for mid, m in markets.items():
        if m["has_buy"] and m["has_sell"]:
            pnl = m["sell_proceeds"] - m["buy_cost"]
            market_pnl[mid] = pnl
            net_pnl += pnl
            if pnl > 0:
                wins += 1
            else:
                losses += 1
        elif m["has_buy"]:
            market_pnl[mid] = None  # still open
            pending += 1

    total_completed = wins + losses
    win_rate = wins / total_completed if total_completed > 0 else 0.0

    # Attach pnl to sell/settled rows
    for t in trades:
        mid = t["market_id"]
        if t["action"] in ("SELL", "SETTLED") and mid in market_pnl:
            t["pnl"] = market_pnl[mid]
        else:
            t["pnl"] = None

    return {
        "trades": trades,
        "summary": {
            "total_trades": total_completed,
            "wins": wins,
            "losses": losses,
            "pending": pending,
            "net_pnl": round(net_pnl, 2),
            "win_rate": round(win_rate, 3),
        },
    }


def get_setting(key: str, default: str | None = None) -> str | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
