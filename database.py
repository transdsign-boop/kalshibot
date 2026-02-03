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
            CREATE TABLE IF NOT EXISTS trade_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT NOT NULL,
                trade_id        TEXT NOT NULL,
                market_id       TEXT NOT NULL,
                action          TEXT NOT NULL,
                side            TEXT NOT NULL,
                price_cents     INTEGER NOT NULL,
                quantity        INTEGER NOT NULL,
                btc_price       REAL,
                strike_price    REAL,
                btc_vs_strike   REAL,
                secs_left       REAL,
                time_factor     REAL,
                best_bid        INTEGER,
                best_ask        INTEGER,
                spread          INTEGER,
                fair_yes_cents  INTEGER,
                fair_yes_prob   REAL,
                yes_edge        INTEGER,
                no_edge         INTEGER,
                vol_dollar_per_min REAL,
                vol_regime      TEXT,
                delta_momentum  REAL,
                velocity_1m     REAL,
                direction_1m    INTEGER,
                price_change_1m REAL,
                decision        TEXT,
                confidence      REAL,
                trigger_type    TEXT,
                position_qty    INTEGER,
                balance         REAL,
                exposure        REAL,
                pnl_cents       REAL,
                hold_duration_s REAL,
                entry_price_cents INTEGER
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


def _trades_from_snapshots(mode: str = "") -> tuple:
    """Build trade-log-compatible records from trade_snapshots table.

    Returns (trades, wins, losses, pending, net_pnl, total_completed, win_rate).
    """
    mode_filter = ""
    if mode == "paper":
        mode_filter = "WHERE market_id LIKE '[PAPER]%'"
    elif mode == "live":
        mode_filter = "WHERE market_id NOT LIKE '[PAPER]%'"
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT ts, market_id, side, action, price_cents, quantity, pnl_cents "
            f"FROM trade_snapshots {mode_filter} ORDER BY id DESC"
        ).fetchall()

    trades = []
    for r in rows:
        d = dict(r)
        trades.append({
            "ts": d["ts"],
            "market_id": d["market_id"],
            "side": d["side"],
            "action": d["action"],
            "price": d["price_cents"] / 100.0,
            "quantity": d["quantity"],
            "pnl": d["pnl_cents"] / 100.0 if d["pnl_cents"] is not None else None,
        })

    # Compute summary from exit records
    wins = 0
    losses = 0
    pending = 0
    net_pnl = 0.0
    seen_markets = set()
    for t in trades:
        mid = t["market_id"]
        if t["action"] in ("SELL", "SL", "TP", "SETTLE", "SETTLED", "EDGE"):
            if t["pnl"] is not None and mid not in seen_markets:
                seen_markets.add(mid)
                net_pnl += t["pnl"]
                if t["pnl"] > 0:
                    wins += 1
                else:
                    losses += 1
        elif t["action"] == "BUY" and mid not in seen_markets:
            # Check if this market has an exit
            has_exit = any(
                x["market_id"] == mid and x["action"] in ("SELL", "SL", "TP", "SETTLE", "SETTLED", "EDGE")
                for x in trades
            )
            if not has_exit:
                seen_markets.add(mid)
                pending += 1

    total_completed = wins + losses
    win_rate = wins / total_completed if total_completed > 0 else 0.0
    return trades, wins, losses, pending, net_pnl, total_completed, win_rate


def get_trades_with_pnl(limit: int = 0, mode: str = "") -> dict:
    """Return trades with per-market P&L and summary stats.

    By default returns ALL trades (limit=0). Pass a positive limit to cap results.
    mode: "paper" = only [PAPER] trades, "live" = only non-[PAPER] trades, "" = all.
    """
    with get_db() as conn:
        where = ""
        if mode == "paper":
            where = "WHERE market_id LIKE '[PAPER]%'"
        elif mode == "live":
            where = "WHERE market_id NOT LIKE '[PAPER]%'"
        if limit > 0:
            rows = conn.execute(
                f"SELECT ts, market_id, side, action, price, quantity FROM trades {where} ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT ts, market_id, side, action, price, quantity FROM trades {where} ORDER BY id DESC",
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
        elif t["action"] in ("SELL", "SETTLED", "SL", "TP", "SETTLE", "EDGE"):
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
        if t["action"] in ("SELL", "SETTLED", "SL", "TP", "SETTLE", "EDGE") and mid in market_pnl:
            t["pnl"] = market_pnl[mid]
        else:
            t["pnl"] = None

    # If no trades found, fall back to trade_snapshots (covers live mode where
    # trades table may be empty but snapshots recorded the activity)
    if not trades:
        trades, wins, losses, pending, net_pnl, total_completed, win_rate = _trades_from_snapshots(mode)

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


_SNAPSHOT_COLS = [
    "ts", "trade_id", "market_id", "action", "side", "price_cents", "quantity",
    "btc_price", "strike_price", "btc_vs_strike", "secs_left", "time_factor",
    "best_bid", "best_ask", "spread",
    "fair_yes_cents", "fair_yes_prob", "yes_edge", "no_edge",
    "vol_dollar_per_min", "vol_regime",
    "delta_momentum", "velocity_1m", "direction_1m", "price_change_1m",
    "decision", "confidence", "trigger_type",
    "position_qty", "balance", "exposure",
    "pnl_cents", "hold_duration_s", "entry_price_cents",
]


def record_snapshot(snapshot: dict):
    """Record a trade context snapshot. Missing keys default to None."""
    values = [snapshot.get(c) for c in _SNAPSHOT_COLS]
    placeholders = ", ".join("?" * len(_SNAPSHOT_COLS))
    col_names = ", ".join(_SNAPSHOT_COLS)
    with get_db() as conn:
        conn.execute(
            f"INSERT INTO trade_snapshots ({col_names}) VALUES ({placeholders})",
            values,
        )


def get_completed_snapshots(limit: int = 0, mode: str = "") -> list[dict]:
    """Return completed round-trip snapshots (exit joined with entry conditions).

    mode: "paper" = only [PAPER] trades, "live" = only non-[PAPER] trades, "" = all.
    """
    mode_filter = ""
    if mode == "paper":
        mode_filter = "AND e.market_id LIKE '[PAPER]%'"
    elif mode == "live":
        mode_filter = "AND e.market_id NOT LIKE '[PAPER]%'"
    with get_db() as conn:
        query = f"""
            SELECT e.*,
                   b.btc_price       AS entry_btc_price,
                   b.fair_yes_cents  AS entry_fair_yes_cents,
                   b.yes_edge        AS entry_yes_edge,
                   b.no_edge         AS entry_no_edge,
                   b.vol_regime      AS entry_vol_regime,
                   b.vol_dollar_per_min AS entry_vol,
                   b.confidence      AS entry_confidence,
                   b.trigger_type    AS entry_trigger,
                   b.secs_left       AS entry_secs_left,
                   b.time_factor     AS entry_time_factor,
                   b.spread          AS entry_spread
            FROM trade_snapshots e
            LEFT JOIN trade_snapshots b
                ON b.market_id = e.market_id
                AND b.action = 'BUY'
            WHERE e.action IN ('SELL', 'SL', 'TP', 'SETTLE', 'EDGE')
              AND e.pnl_cents IS NOT NULL
              {mode_filter}
            ORDER BY e.id DESC
        """
        if limit > 0:
            query += f" LIMIT {limit}"
        rows = conn.execute(query).fetchall()
    return [dict(r) for r in rows]


def get_entry_snapshot(market_id: str) -> dict | None:
    """Look up the BUY snapshot for a market (for computing exit P&L and hold duration)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT ts, price_cents FROM trade_snapshots "
            "WHERE market_id = ? AND action = 'BUY' ORDER BY id DESC LIMIT 1",
            (market_id,),
        ).fetchone()
    return dict(row) if row else None


def get_all_unsettled_live_entries() -> list[dict]:
    """Return all live-mode BUY snapshots that have no corresponding exit snapshot.

    Used for backfilling settlement records for historical trades.
    """
    with get_db() as conn:
        # Get all live-mode market_ids with BUY snapshots
        buy_markets = conn.execute(
            "SELECT DISTINCT market_id FROM trade_snapshots "
            "WHERE action = 'BUY' AND market_id NOT LIKE '[PAPER]%'"
        ).fetchall()

        results = []
        for row in buy_markets:
            mid = row["market_id"]
            has_exit = conn.execute(
                "SELECT 1 FROM trade_snapshots WHERE market_id = ? "
                "AND action IN ('SELL', 'SL', 'TP', 'SETTLE', 'SETTLED', 'EDGE') LIMIT 1",
                (mid,),
            ).fetchone()
            if has_exit:
                continue
            entry = conn.execute(
                "SELECT ts, market_id, price_cents, side, quantity, position_qty "
                "FROM trade_snapshots "
                "WHERE market_id = ? AND action = 'BUY' ORDER BY id DESC LIMIT 1",
                (mid,),
            ).fetchone()
            if entry:
                results.append(dict(entry))
    return results


def backfill_buy_trades_from_snapshots() -> list[str]:
    """Backfill BUY records in the trades table from trade_snapshots.

    For any live-mode market that has a SETTLE/SETTLED in the trades table but
    no BUY, copy the BUY from trade_snapshots so round-trip PnL works.
    Returns list of market_ids that were backfilled.
    """
    backfilled = []
    with get_db() as conn:
        # Find live markets in trades table that have SETTLE but no BUY
        settle_markets = conn.execute(
            "SELECT DISTINCT market_id FROM trades "
            "WHERE action IN ('SETTLE', 'SETTLED', 'SL', 'TP', 'EDGE') "
            "AND market_id NOT LIKE '[PAPER]%'"
        ).fetchall()

        for row in settle_markets:
            mid = row["market_id"]
            has_buy = conn.execute(
                "SELECT 1 FROM trades WHERE market_id = ? AND action = 'BUY' LIMIT 1",
                (mid,),
            ).fetchone()
            if has_buy:
                continue

            # Get BUY snapshots for this market
            buy_snaps = conn.execute(
                "SELECT ts, market_id, side, price_cents, quantity FROM trade_snapshots "
                "WHERE market_id = ? AND action = 'BUY' ORDER BY id",
                (mid,),
            ).fetchall()

            for snap in buy_snaps:
                s = dict(snap)
                conn.execute(
                    "INSERT INTO trades (ts, market_id, side, action, price, quantity, order_id) "
                    "VALUES (?, ?, ?, 'BUY', ?, ?, ?)",
                    (s["ts"], s["market_id"], s["side"],
                     s["price_cents"] / 100.0, s["quantity"],
                     f"backfill-buy-{mid}"),
                )
            if buy_snaps:
                backfilled.append(mid)
    return backfilled


def get_unsettled_entry(market_id: str) -> dict | None:
    """Return the BUY snapshot for a market only if no exit snapshot exists.

    Used to detect live positions that expired without an active exit.
    """
    with get_db() as conn:
        has_exit = conn.execute(
            "SELECT 1 FROM trade_snapshots WHERE market_id = ? "
            "AND action IN ('SELL', 'SL', 'TP', 'SETTLE', 'SETTLED', 'EDGE') LIMIT 1",
            (market_id,),
        ).fetchone()
        if has_exit:
            return None
        row = conn.execute(
            "SELECT ts, price_cents, side, quantity, position_qty FROM trade_snapshots "
            "WHERE market_id = ? AND action = 'BUY' ORDER BY id DESC LIMIT 1",
            (market_id,),
        ).fetchone()
    return dict(row) if row else None


def get_legacy_round_trips(mode: str = "") -> list[dict]:
    """Build round-trip trade records from the trades table for analytics fallback.

    Returns one dict per completed round-trip (BUY + exit) with fields compatible
    with the analytics engine: side, action (exit type), pnl_cents, hold_duration_s,
    entry_price_cents, quantity.
    mode: "paper" = only [PAPER] trades, "live" = only non-[PAPER] trades, "" = all.
    """
    with get_db() as conn:
        where = ""
        if mode == "paper":
            where = "WHERE market_id LIKE '[PAPER]%'"
        elif mode == "live":
            where = "WHERE market_id NOT LIKE '[PAPER]%'"
        rows = conn.execute(
            f"SELECT ts, market_id, side, action, price, quantity FROM trades {where} ORDER BY id"
        ).fetchall()

    trades_list = [dict(r) for r in rows]

    # Group by market_id
    markets: dict[str, list] = {}
    for t in trades_list:
        mid = t["market_id"]
        markets.setdefault(mid, []).append(t)

    results = []
    for mid, market_trades in markets.items():
        buys = [t for t in market_trades if t["action"] == "BUY"]
        exits = [t for t in market_trades
                 if t["action"] in ("SELL", "SL", "TP", "SETTLE", "SETTLED", "EDGE")]

        if not buys or not exits:
            continue

        buy = buys[0]
        exit_trade = exits[-1]

        buy_cost = sum(t["price"] * t["quantity"] for t in buys)
        sell_proceeds = sum(t["price"] * t["quantity"] for t in exits)
        total_qty = sum(t["quantity"] for t in buys)
        pnl_dollars = sell_proceeds - buy_cost

        # Hold duration from timestamps
        hold_s = 0.0
        try:
            buy_ts = datetime.fromisoformat(buy["ts"])
            exit_ts = datetime.fromisoformat(exit_trade["ts"])
            hold_s = (exit_ts - buy_ts).total_seconds()
        except Exception:
            pass

        results.append({
            "market_id": mid,
            "side": buy["side"],
            "action": exit_trade["action"],
            "price_cents": round(buy["price"] * 100),
            "entry_price_cents": round(buy["price"] * 100),
            "quantity": total_qty,
            "pnl_cents": round(pnl_dollars * 100, 2),
            "hold_duration_s": hold_s,
        })

    return results


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
