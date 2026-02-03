import os
import base64
import tempfile
from dotenv import load_dotenv

load_dotenv()

# --- Supported Assets ---
SUPPORTED_ASSETS = ["btc", "eth", "sol"]

ASSET_CONFIG = {
    "btc": {
        "market_series": "KXBTC15M",
        "display_name": "BTC",
        "strike_threshold": 1000,  # Prices > 1000 are in dollars, otherwise cents
    },
    "eth": {
        "market_series": "KXETH15M",
        "display_name": "ETH",
        "strike_threshold": 100,  # ETH prices ~$3000, threshold lower
    },
    "sol": {
        "market_series": "KXSOL15M",
        "display_name": "SOL",
        "strike_threshold": 10,   # SOL prices ~$200, threshold lowest
    },
}

# Asset-specific defaults calibrated from volatility research:
# - ETH: ~1.6x more volatile than BTC (%-wise), price ~3% of BTC → ~5% of BTC absolute moves
# - SOL: ~2.0x more volatile than BTC (%-wise), price ~0.13% of BTC → ~0.26% of BTC absolute moves
# Current prices: BTC ~$75k, ETH ~$2.1k, SOL ~$100
ASSET_DEFAULTS = {
    "btc": {
        # Volatility thresholds ($/min tick path - tick path is ~5x candle close-to-close)
        "VOL_HIGH_THRESHOLD": 400.0,       # BTC moves ~$80-100/min candle → ~$400-500 tick path
        "VOL_LOW_THRESHOLD": 200.0,        # BTC quiet ~$40/min candle → ~$200 tick path
        # Price movement thresholds
        "LEAD_LAG_THRESHOLD": 75.0,        # USD lead-lag signal trigger
        "DELTA_THRESHOLD": 20.0,           # USD momentum deviation trigger
        "EXTREME_DELTA_THRESHOLD": 50.0,   # USD aggressive execution trigger
        "TREND_FOLLOW_VELOCITY": 2.0,      # $/sec for trend bonus (~$120/min)
    },
    "eth": {
        # ETH: ~5% of BTC absolute moves (3% price ratio * 1.6x volatility)
        # ETH moves ~$4-5/min candle → ~$20-25 tick path
        "VOL_HIGH_THRESHOLD": 20.0,        # 400 * 0.05
        "VOL_LOW_THRESHOLD": 10.0,         # 200 * 0.05
        "LEAD_LAG_THRESHOLD": 4.0,         # 75 * 0.05
        "DELTA_THRESHOLD": 1.0,            # 20 * 0.05
        "EXTREME_DELTA_THRESHOLD": 2.5,    # 50 * 0.05
        "TREND_FOLLOW_VELOCITY": 0.10,     # 2.0 * 0.05
    },
    "sol": {
        # SOL: ~0.26% of BTC absolute moves (0.13% price ratio * 2x volatility)
        # SOL moves ~$0.20-0.30/min candle → ~$1.0-1.5 tick path
        "VOL_HIGH_THRESHOLD": 1.0,         # 400 * 0.0026
        "VOL_LOW_THRESHOLD": 0.5,          # 200 * 0.0026
        "LEAD_LAG_THRESHOLD": 0.2,         # 75 * 0.0026
        "DELTA_THRESHOLD": 0.05,           # 20 * 0.0026
        "EXTREME_DELTA_THRESHOLD": 0.15,   # 50 * 0.0026
        "TREND_FOLLOW_VELOCITY": 0.005,    # 2.0 * 0.0026
    },
}


# --- Handle base64-encoded private keys (for Fly.io deployment) ---
def _decode_pem_if_needed(path_env_var: str, b64_env_var: str) -> str:
    """
    If a base64-encoded PEM is provided via env var, decode it to a temp file.
    Otherwise, use the path from the environment.
    """
    b64_key = os.getenv(b64_env_var)
    if b64_key:
        # Decode base64 and write to temporary file
        pem_content = base64.b64decode(b64_key)
        temp_file = tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.pem')
        temp_file.write(pem_content)
        temp_file.close()
        return temp_file.name
    else:
        # Use the path from environment
        return os.getenv(path_env_var, "")

# --- Per-environment Kalshi credentials ---
KALSHI_LIVE_API_KEY_ID = os.getenv("KALSHI_LIVE_API_KEY_ID", "")
KALSHI_LIVE_PRIVATE_KEY_PATH = _decode_pem_if_needed(
    "KALSHI_LIVE_PRIVATE_KEY_PATH",
    "KALSHI_LIVE_PRIVATE_KEY_B64"
)

KALSHI_DEMO_API_KEY_ID = os.getenv("KALSHI_DEMO_API_KEY_ID", "")
KALSHI_DEMO_PRIVATE_KEY_PATH = _decode_pem_if_needed(
    "KALSHI_DEMO_PRIVATE_KEY_PATH",
    "KALSHI_DEMO_PRIVATE_KEY_B64"
)

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
MIN_CONTRACT_PRICE = 5
MAX_CONTRACT_PRICE = 85           # avoid buying above this (bad risk/reward)
STOP_LOSS_CENTS = 15              # exit position if down this many cents/contract

# Profit-taking
HIT_RUN_PCT = float(os.getenv("HIT_RUN_PCT", "0"))  # % gain — instant exit when hit (no time restrictions)
PROFIT_TAKE_PCT = 50              # % gain from entry — full exit when profit exceeds this
FREE_ROLL_PRICE = 90              # cents — sell half to lock in capital
PROFIT_TAKE_MIN_SECS = 300        # only take full profit if >5 min remain
HOLD_EXPIRY_SECS = 120            # don't sell in last 2 minutes — ride to settlement

# Edge-based exit (exit when edge evaporates, re-enter when new edge appears)
EDGE_EXIT_ENABLED = os.getenv("EDGE_EXIT_ENABLED", "true").lower() == "true"
EDGE_EXIT_THRESHOLD_CENTS = int(os.getenv("EDGE_EXIT_THRESHOLD_CENTS", "2"))    # remaining edge threshold (scaled by time_factor)
EDGE_EXIT_MIN_HOLD_SECS = int(os.getenv("EDGE_EXIT_MIN_HOLD_SECS", "30"))      # min hold before edge-exit can fire
EDGE_EXIT_COOLDOWN_SECS = int(os.getenv("EDGE_EXIT_COOLDOWN_SECS", "30"))      # cooldown before re-entry after edge-exit
REENTRY_EDGE_PREMIUM = int(os.getenv("REENTRY_EDGE_PREMIUM", "3"))             # extra edge (c) required for re-entry

# Alpha Engine thresholds
DELTA_THRESHOLD = 20.0            # USD — front-run trigger (momentum deviation)
EXTREME_DELTA_THRESHOLD = 50.0    # USD — aggressive execution trigger
ANCHOR_SECONDS_THRESHOLD = 60     # seconds — anchor defense trigger
LEAD_LAG_THRESHOLD = 75.0         # USD — lead-lag signal trigger (global price vs strike). BTC moves ~$77/min avg.
LEAD_LAG_ENABLED = os.getenv("LEAD_LAG_ENABLED", "false").lower() == "true"  # Enable/disable lead-lag signal

# Rule-based strategy (replaces Claude AI fallback)
VOL_HIGH_THRESHOLD = float(os.getenv("VOL_HIGH_THRESHOLD", "400.0"))          # $/min tick path — above = high vol (trend-follow). Tick path ~5x candle; BTC avg candle ~$87 ≈ $500 tick.
VOL_LOW_THRESHOLD = float(os.getenv("VOL_LOW_THRESHOLD", "200.0"))            # $/min tick path — below = low vol (sit out). BTC quiet candle ~$40 ≈ $200 tick.
FAIR_VALUE_K = float(os.getenv("FAIR_VALUE_K", "0.6"))                       # logistic steepness — 0.6 = moderate. Lower = less extreme probabilities, finds more edge in 15-85c range
MIN_EDGE_CENTS = int(os.getenv("MIN_EDGE_CENTS", "5"))                      # min mispricing to trade (5c = good balance for 15m binaries)
TREND_FOLLOW_VELOCITY = float(os.getenv("TREND_FOLLOW_VELOCITY", "2.0"))     # $/sec — BTC ~$120/min = $2/sec triggers trend bonus
RULE_SIT_OUT_LOW_VOL = os.getenv("RULE_SIT_OUT_LOW_VOL", "true").lower() == "true"
RULE_MIN_CONFIDENCE = float(os.getenv("RULE_MIN_CONFIDENCE", "0.6"))         # min confidence to execute (0.6 = needs real edge + time)

# Paper trading (demo mode uses live API but simulates trades)
PAPER_STARTING_BALANCE = float(os.getenv("PAPER_STARTING_BALANCE", "100.0"))
PAPER_FILL_FRACTION = float(os.getenv("PAPER_FILL_FRACTION", "1.0"))  # fraction of book depth filled (1.0 = full depth, crossing orders fill against all resting liquidity)

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
    "MIN_CONTRACT_PRICE":   {"type": "int",   "min": 1,  "max": 55},
    "MAX_CONTRACT_PRICE":   {"type": "int",   "min": 50, "max": 99},
    "STOP_LOSS_CENTS":      {"type": "int",   "min": 0,  "max": 99},
    "HIT_RUN_PCT":          {"type": "float", "min": 0,  "max": 500},
    "PROFIT_TAKE_PCT":      {"type": "int",   "min": 5,  "max": 500},
    "FREE_ROLL_PRICE":      {"type": "int",   "min": 75, "max": 99},
    "PROFIT_TAKE_MIN_SECS": {"type": "int",   "min": 60, "max": 600},
    "HOLD_EXPIRY_SECS":     {"type": "int",   "min": 30, "max": 300},
    "POLL_INTERVAL_SECONDS":{"type": "int",   "min": 5,  "max": 120},
    "DELTA_THRESHOLD":          {"type": "float", "min": 0.01, "max": 200.0},  # SOL needs 0.05
    "EXTREME_DELTA_THRESHOLD":  {"type": "float", "min": 0.01, "max": 500.0},  # SOL needs 0.15
    "ANCHOR_SECONDS_THRESHOLD": {"type": "int",   "min": 15,   "max": 120},
    "LEAD_LAG_THRESHOLD":       {"type": "float", "min": 0.01, "max": 500.0},  # SOL needs 0.2
    "LEAD_LAG_ENABLED":         {"type": "bool"},
    "VOL_HIGH_THRESHOLD":       {"type": "float", "min": 0.1,  "max": 2000.0},  # SOL needs 1.0
    "VOL_LOW_THRESHOLD":        {"type": "float", "min": 0.1,  "max": 1000.0},  # SOL needs 0.5
    "FAIR_VALUE_K":             {"type": "float", "min": 0.1,  "max": 3.0},
    "MIN_EDGE_CENTS":           {"type": "int",   "min": 1,    "max": 30},
    "TREND_FOLLOW_VELOCITY":    {"type": "float", "min": 0.001, "max": 20.0},  # SOL needs 0.005
    "RULE_SIT_OUT_LOW_VOL":     {"type": "bool"},
    "RULE_MIN_CONFIDENCE":      {"type": "float", "min": 0.3, "max": 0.95},
    "EDGE_EXIT_ENABLED":        {"type": "bool"},
    "EDGE_EXIT_THRESHOLD_CENTS":{"type": "int",   "min": 0,  "max": 15},
    "EDGE_EXIT_MIN_HOLD_SECS":  {"type": "int",   "min": 10, "max": 120},
    "EDGE_EXIT_COOLDOWN_SECS":  {"type": "int",   "min": 10, "max": 120},
    "REENTRY_EDGE_PREMIUM":     {"type": "int",   "min": 0,  "max": 15},
    "PAPER_STARTING_BALANCE":   {"type": "float", "min": 10,  "max": 100000},
    "PAPER_FILL_FRACTION":      {"type": "float", "min": 0.05, "max": 1.0},
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


class BotConfig:
    """Per-bot configuration that allows independent settings for each asset/mode combination."""

    def __init__(self, mode: str, asset: str = "btc"):
        """Initialize with mode ('paper' or 'live') and asset ('btc', 'eth', 'sol').

        Copies current global values, then applies asset-specific defaults.
        """
        if mode not in ("paper", "live"):
            raise ValueError(f"Invalid mode: {mode}")
        if asset not in SUPPORTED_ASSETS:
            raise ValueError(f"Invalid asset: {asset}")
        self.mode = mode
        self.asset = asset
        # Copy all tunable values from global config
        import config as _self
        for key in TUNABLE_FIELDS:
            setattr(self, key, getattr(_self, key))

        # Apply asset-specific defaults for vol/threshold params
        asset_defaults = ASSET_DEFAULTS.get(asset, {})
        for key, value in asset_defaults.items():
            if key in TUNABLE_FIELDS:
                setattr(self, key, value)

    def get_all(self) -> dict:
        """Return all tunable config values as a dict."""
        return {k: getattr(self, k) for k in TUNABLE_FIELDS}

    def update(self, updates: dict) -> dict:
        """Update config values with validation. Returns applied updates."""
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
                setattr(self, key, value)
                applied[key] = value
            except (ValueError, TypeError):
                continue
        return applied

    def save(self):
        """Persist config to DB with asset+mode prefix (config_{asset}_{mode}_{key})."""
        from database import set_setting
        for key in TUNABLE_FIELDS:
            set_setting(f"config_{self.asset}_{self.mode}_{key}", str(getattr(self, key)))

    @classmethod
    def load(cls, mode: str, asset: str = "btc") -> "BotConfig":
        """Load config from DB with asset+mode prefix, falling back to asset defaults.

        Only loads values that were explicitly saved for this asset+mode.
        Does NOT fall back to BTC config for ETH/SOL - uses ASSET_DEFAULTS instead.
        """
        from database import get_setting
        cfg = cls(mode, asset)  # This already applies ASSET_DEFAULTS
        for key, spec in TUNABLE_FIELDS.items():
            # Only load from asset-specific key pattern
            saved = get_setting(f"config_{asset}_{mode}_{key}")
            # For BTC only, also check old key pattern (migration compatibility)
            if saved is None and asset == "btc":
                saved = get_setting(f"config_{mode}_{key}")
            if saved is None:
                continue  # Keep the asset-specific default from __init__
            try:
                if spec["type"] == "bool":
                    setattr(cfg, key, saved.lower() in ("true", "1"))
                elif spec["type"] == "int":
                    setattr(cfg, key, int(saved))
                elif spec["type"] == "float":
                    setattr(cfg, key, float(saved))
            except (ValueError, TypeError):
                continue
        return cfg


def migrate_to_dual_config():
    """One-time migration: copy existing config_X to both config_paper_X and config_live_X."""
    from database import get_setting, set_setting
    migrated = False
    for key in TUNABLE_FIELDS:
        # Check if already migrated
        paper_val = get_setting(f"config_paper_{key}")
        live_val = get_setting(f"config_live_{key}")
        if paper_val is not None or live_val is not None:
            continue  # Already migrated

        # Copy from old format
        old_val = get_setting(f"config_{key}")
        if old_val is not None:
            set_setting(f"config_paper_{key}", old_val)
            set_setting(f"config_live_{key}", old_val)
            migrated = True

    return migrated


def migrate_to_multi_asset():
    """One-time migration: copy existing BTC config to multi-asset key pattern.

    Copies config_{mode}_{key} -> config_btc_{mode}_{key} for backward compat.
    Also copies paper_balance -> btc_paper_balance.
    """
    from database import get_setting, set_setting
    migrated = False

    # Migrate config keys: config_{mode}_{key} -> config_btc_{mode}_{key}
    for mode in ("paper", "live"):
        for key in TUNABLE_FIELDS:
            old_key = f"config_{mode}_{key}"
            new_key = f"config_btc_{mode}_{key}"
            # Skip if already migrated
            if get_setting(new_key) is not None:
                continue
            old_val = get_setting(old_key)
            if old_val is not None:
                set_setting(new_key, old_val)
                migrated = True

    # Migrate paper trading state: paper_X -> btc_paper_X
    paper_keys = ["paper_balance", "paper_positions", "paper_last_ticker"]
    for old_key in paper_keys:
        new_key = f"btc_{old_key}"
        if get_setting(new_key) is not None:
            continue
        old_val = get_setting(old_key)
        if old_val is not None:
            set_setting(new_key, old_val)
            migrated = True

    return migrated


def reset_asset_specific_defaults():
    """Reset ETH and SOL configs to their proper asset-specific defaults.

    This clears any incorrectly-saved BTC values for ETH/SOL and re-saves
    the correct asset-specific defaults from ASSET_DEFAULTS.
    """
    from database import set_setting, get_setting
    reset_count = 0

    # Keys that should have asset-specific values (from ASSET_DEFAULTS)
    asset_specific_keys = set()
    for asset in ["eth", "sol"]:
        asset_specific_keys.update(ASSET_DEFAULTS.get(asset, {}).keys())

    for asset in ["eth", "sol"]:
        asset_defaults = ASSET_DEFAULTS.get(asset, {})
        for mode in ["paper", "live"]:
            for key, value in asset_defaults.items():
                db_key = f"config_{asset}_{mode}_{key}"
                # Always overwrite with asset-specific default
                set_setting(db_key, str(value))
                reset_count += 1

    return reset_count


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
