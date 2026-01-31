import os
from dotenv import load_dotenv

load_dotenv()

# --- Per-environment Kalshi credentials ---
KALSHI_LIVE_API_KEY_ID = os.getenv("KALSHI_LIVE_API_KEY_ID", "")
KALSHI_LIVE_PRIVATE_KEY_PATH = os.getenv("KALSHI_LIVE_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")

KALSHI_DEMO_API_KEY_ID = os.getenv("KALSHI_DEMO_API_KEY_ID", "")
KALSHI_DEMO_PRIVATE_KEY_PATH = os.getenv("KALSHI_DEMO_PRIVATE_KEY_PATH", "./kalshi_demo_private_key.pem")

# Active environment: "demo" or "live"
KALSHI_ENV = os.getenv("KALSHI_ENV", "demo")

# Always use live credentials — demo mode is paper trading on the live API
KALSHI_API_KEY_ID = KALSHI_LIVE_API_KEY_ID
KALSHI_API_PRIVATE_KEY_PATH = KALSHI_LIVE_PRIVATE_KEY_PATH

KALSHI_HOST = "https://api.elections.kalshi.com"

# --- Anthropic ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Trading Rules (mutable at runtime) ---
# Percentage-based sizing: scales automatically with account balance
ORDER_SIZE_PCT = float(os.getenv("ORDER_SIZE_PCT", "5.0"))             # % of balance per order
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "15.0"))        # % of balance max position
MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "30.0"))  # % of balance max exposure
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "10.0"))    # % of balance max daily loss
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"

# Target market series
MARKET_SERIES = "KXBTC15M"

# Safety thresholds
MIN_SECONDS_TO_CLOSE = 90
MAX_SPREAD_CENTS = 25
MIN_AGENT_CONFIDENCE = 0.75
MIN_CONTRACT_PRICE = 5
MAX_CONTRACT_PRICE = 85           # avoid buying above this (bad risk/reward)
STOP_LOSS_CENTS = 15              # exit position if down this many cents/contract

# Profit-taking
PROFIT_TAKE_PCT = 50              # % gain from entry — full exit when profit exceeds this
FREE_ROLL_PRICE = 90              # cents — sell half to lock in capital
PROFIT_TAKE_MIN_SECS = 300        # only take full profit if >5 min remain
HOLD_EXPIRY_SECS = 120            # don't sell in last 2 minutes — ride to settlement

# Alpha Engine thresholds
DELTA_THRESHOLD = 20              # USD — front-run trigger
EXTREME_DELTA_THRESHOLD = 50      # USD — aggressive execution trigger
ANCHOR_SECONDS_THRESHOLD = 60     # seconds — anchor defense trigger

# Paper trading (demo mode uses live API but simulates trades)
PAPER_STARTING_BALANCE = float(os.getenv("PAPER_STARTING_BALANCE", "100.0"))

# Loop interval
POLL_INTERVAL_SECONDS = 10


# --- Runtime helpers ---
TUNABLE_FIELDS = {
    "TRADING_ENABLED":      {"type": "bool"},
    "ORDER_SIZE_PCT":       {"type": "float", "min": 0.5, "max": 50},
    "MAX_POSITION_PCT":     {"type": "float", "min": 1,   "max": 100},
    "MAX_TOTAL_EXPOSURE_PCT": {"type": "float", "min": 1, "max": 100},
    "MAX_DAILY_LOSS_PCT":   {"type": "float", "min": 1,   "max": 100},
    "MIN_SECONDS_TO_CLOSE": {"type": "int",   "min": 30, "max": 600},
    "MAX_SPREAD_CENTS":     {"type": "int",   "min": 1,  "max": 100},
    "MIN_AGENT_CONFIDENCE": {"type": "float", "min": 0,  "max": 1},
    "MIN_CONTRACT_PRICE":   {"type": "int",   "min": 1,  "max": 55},
    "MAX_CONTRACT_PRICE":   {"type": "int",   "min": 50, "max": 99},
    "STOP_LOSS_CENTS":      {"type": "int",   "min": 0,  "max": 50},
    "PROFIT_TAKE_PCT":      {"type": "int",   "min": 5,  "max": 500},
    "FREE_ROLL_PRICE":      {"type": "int",   "min": 75, "max": 99},
    "PROFIT_TAKE_MIN_SECS": {"type": "int",   "min": 60, "max": 600},
    "HOLD_EXPIRY_SECS":     {"type": "int",   "min": 30, "max": 300},
    "POLL_INTERVAL_SECONDS":{"type": "int",   "min": 5,  "max": 120},
    "DELTA_THRESHOLD":          {"type": "int",   "min": 5,   "max": 200},
    "EXTREME_DELTA_THRESHOLD":  {"type": "int",   "min": 10,  "max": 500},
    "ANCHOR_SECONDS_THRESHOLD": {"type": "int",   "min": 15,  "max": 120},
    "PAPER_STARTING_BALANCE":   {"type": "float", "min": 10,  "max": 100000},
}


def get_tunables() -> dict:
    return {k: getattr(__import__(__name__), k) for k in TUNABLE_FIELDS}


def set_tunables(updates: dict) -> dict:
    import config as _self
    from database import set_setting
    applied = {}
    for key, value in updates.items():
        spec = TUNABLE_FIELDS.get(key)
        if spec is None:
            continue
        try:
            if spec["type"] == "bool":
                value = value if isinstance(value, bool) else str(value).lower() in ("true", "1")
            elif spec["type"] == "int":
                value = max(spec["min"], min(spec["max"], int(value)))
            elif spec["type"] == "float":
                value = max(spec["min"], min(spec["max"], float(value)))
            setattr(_self, key, value)
            set_setting(f"config_{key}", str(value))
            applied[key] = value
        except (ValueError, TypeError):
            continue
    return applied


def restore_tunables():
    """Restore persisted tunable config values from the database."""
    import config as _self
    from database import get_setting
    for key, spec in TUNABLE_FIELDS.items():
        saved = get_setting(f"config_{key}")
        if saved is None:
            continue
        try:
            if spec["type"] == "bool":
                setattr(_self, key, saved.lower() in ("true", "1"))
            elif spec["type"] == "int":
                setattr(_self, key, int(saved))
            elif spec["type"] == "float":
                setattr(_self, key, float(saved))
        except (ValueError, TypeError):
            continue


def switch_env(env: str):
    """Switch active Kalshi environment and update resolved credentials.

    Both 'demo' (paper) and 'live' use the live Kalshi API.
    'demo' mode simulates trades without placing real orders.
    """
    import config as _self
    if env not in ("demo", "live"):
        raise ValueError(f"Invalid env: {env}")
    _self.KALSHI_ENV = env
    # Always use live credentials — demo mode is paper trading on the live API
    _self.KALSHI_API_KEY_ID = _self.KALSHI_LIVE_API_KEY_ID
    _self.KALSHI_API_PRIVATE_KEY_PATH = _self.KALSHI_LIVE_PRIVATE_KEY_PATH
    return env
