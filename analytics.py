"""Trade analytics engine: performance stats, segmentation, and parameter suggestions."""

import math

import config
from database import get_completed_snapshots, get_legacy_round_trips


def _safe_pf(gross_win: float, gross_loss: float):
    """Compute profit factor, returning None instead of inf for JSON safety."""
    if gross_loss > 0:
        return round(gross_win / gross_loss, 2)
    return None if gross_win > 0 else 0

MIN_SAMPLE_SIZE = 10  # minimum trades per bucket before making suggestions


def compute_analytics(mode: str = "") -> dict:
    """Compute performance analytics and parameter suggestions.

    Always uses the trades table for summary and basic segments (captures all history).
    Adds snapshot-specific segments (vol, edge, confidence, time, trigger) and
    parameter suggestions when enough trade_snapshots exist.
    mode: "paper" = only paper trades, "live" = only live trades, "" = all.
    """
    round_trips = get_legacy_round_trips(mode=mode)
    snapshots = get_completed_snapshots(mode=mode)

    if not round_trips and not snapshots:
        return {"summary": {}, "segments": {}, "suggestions": [], "total_snapshots": 0}

    # Use legacy trades for summary and basic segments (captures ALL trades)
    primary = round_trips if round_trips else snapshots
    summary = _compute_summary(primary)
    segments = _compute_legacy_segments(round_trips) if round_trips else {}

    # Add snapshot-specific segments when available (vol, edge, confidence, etc.)
    suggestions = []
    if snapshots:
        snapshot_segments = _compute_segments(snapshots)
        for key in ("vol_regime", "edge", "confidence", "time", "trigger"):
            if key in snapshot_segments:
                segments[key] = snapshot_segments[key]
        suggestions = _generate_suggestions(snapshots, snapshot_segments)

    return {
        "summary": summary,
        "segments": segments,
        "suggestions": suggestions,
        "total_snapshots": len(primary),
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _compute_summary(snapshots: list[dict]) -> dict:
    pnls = [s.get("pnl_cents", 0) or 0 for s in snapshots]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    durations = [s.get("hold_duration_s", 0) or 0 for s in snapshots if s.get("hold_duration_s")]

    return {
        "total_trades": len(snapshots),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(snapshots), 3) if snapshots else 0,
        "avg_pnl_cents": round(sum(pnls) / len(pnls), 2) if pnls else 0,
        "total_pnl_cents": round(sum(pnls), 2),
        "profit_factor": _safe_pf(gross_win, gross_loss),
        "avg_hold_seconds": round(sum(durations) / len(durations), 1) if durations else 0,
        "best_trade_cents": round(max(pnls), 2) if pnls else 0,
        "worst_trade_cents": round(min(pnls), 2) if pnls else 0,
    }


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def _bucket_stats(snapshots: list[dict], key) -> dict:
    """Group snapshots by key function and compute stats per bucket."""
    buckets: dict[str, list] = {}
    for s in snapshots:
        label = key(s)
        buckets.setdefault(label, []).append(s)

    result = {}
    for label, trades in buckets.items():
        pnls = [t.get("pnl_cents", 0) or 0 for t in trades]
        wins = [p for p in pnls if p > 0]
        losses_list = [p for p in pnls if p <= 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses_list))

        result[label] = {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses_list),
            "win_rate": round(len(wins) / len(trades), 3) if trades else 0,
            "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0,
            "total_pnl": round(sum(pnls), 2),
            "profit_factor": _safe_pf(gross_win, gross_loss),
        }
    return result


def _compute_segments(snapshots: list[dict]) -> dict:
    segments = {}

    # Vol regime
    segments["vol_regime"] = _bucket_stats(
        snapshots,
        key=lambda s: s.get("entry_vol_regime") or "unknown",
    )

    # Edge bucket (use relevant edge based on side)
    def edge_bucket(s):
        side = s.get("side", "")
        edge = (s.get("entry_yes_edge", 0) or 0) if side == "yes" else (s.get("entry_no_edge", 0) or 0)
        if edge < 5:
            return "0-4c"
        elif edge < 10:
            return "5-9c"
        elif edge < 15:
            return "10-14c"
        else:
            return "15c+"

    segments["edge"] = _bucket_stats(snapshots, key=edge_bucket)

    # Confidence bucket
    def conf_bucket(s):
        c = s.get("entry_confidence", 0) or 0
        if c < 0.6:
            return "<60%"
        elif c < 0.7:
            return "60-69%"
        elif c < 0.8:
            return "70-79%"
        else:
            return "80%+"

    segments["confidence"] = _bucket_stats(snapshots, key=conf_bucket)

    # Time bucket
    def time_bucket(s):
        t = s.get("entry_secs_left", 0) or 0
        if t < 180:
            return "90-180s"
        elif t < 360:
            return "180-360s"
        elif t < 600:
            return "360-600s"
        else:
            return "600s+"

    segments["time"] = _bucket_stats(snapshots, key=time_bucket)

    # Trigger type
    segments["trigger"] = _bucket_stats(
        snapshots,
        key=lambda s: s.get("entry_trigger") or "rules",
    )

    # Exit type
    segments["exit_type"] = _bucket_stats(
        snapshots,
        key=lambda s: s.get("action") or "unknown",
    )

    return segments


def _compute_legacy_segments(trades: list[dict]) -> dict:
    """Compute segments from legacy trades table data (no snapshot context)."""
    segments = {}

    # By side
    segments["side"] = _bucket_stats(
        trades,
        key=lambda s: (s.get("side") or "unknown").upper(),
    )

    # By entry price
    def price_bucket(s):
        p = s.get("entry_price_cents", 50) or 50
        if p <= 30:
            return "1-30c"
        elif p <= 50:
            return "31-50c"
        elif p <= 70:
            return "51-70c"
        else:
            return "71-99c"

    segments["entry_price"] = _bucket_stats(trades, key=price_bucket)

    # By exit type
    segments["exit_type"] = _bucket_stats(
        trades,
        key=lambda s: s.get("action") or "unknown",
    )

    # By position size
    def size_bucket(s):
        q = s.get("quantity", 0) or 0
        if q <= 5:
            return "1-5"
        elif q <= 15:
            return "6-15"
        elif q <= 30:
            return "16-30"
        else:
            return "31+"

    segments["position_size"] = _bucket_stats(trades, key=size_bucket)

    # By hold duration
    def hold_bucket(s):
        d = s.get("hold_duration_s", 0) or 0
        if d < 120:
            return "<2min"
        elif d < 300:
            return "2-5min"
        elif d < 600:
            return "5-10min"
        else:
            return "10min+"

    segments["hold_time"] = _bucket_stats(trades, key=hold_bucket)

    return segments


# ---------------------------------------------------------------------------
# Suggestion Engine
# ---------------------------------------------------------------------------

def _generate_suggestions(snapshots: list[dict], segments: dict) -> list[dict]:
    suggestions = []
    _suggest_min_edge(segments.get("edge", {}), suggestions)
    _suggest_min_confidence(segments.get("confidence", {}), suggestions)
    _suggest_min_time(segments.get("time", {}), suggestions)
    _suggest_vol_threshold(segments.get("vol_regime", {}), snapshots, suggestions)
    _suggest_stop_loss(segments.get("exit_type", {}), snapshots, suggestions)
    _suggest_fair_value_k(snapshots, suggestions)
    return suggestions


def _confidence_level(sample_size: int) -> str:
    if sample_size >= 30:
        return "high"
    elif sample_size >= 15:
        return "medium"
    return "low"


def _suggest_min_edge(edge_data: dict, suggestions: list):
    """If low-edge trades lose money on average, suggest raising MIN_EDGE_CENTS."""
    current = config.MIN_EDGE_CENTS

    # Parse bucket boundaries and find losing/profitable buckets
    bucket_info = []
    for label, stats in edge_data.items():
        if stats["trades"] < MIN_SAMPLE_SIZE:
            continue
        try:
            lower = int(label.split("-")[0].replace("c", "").replace("+", ""))
        except ValueError:
            continue
        bucket_info.append((lower, label, stats))

    bucket_info.sort(key=lambda x: x[0])

    # Find lowest profitable bucket
    losing_count = 0
    losing_pnl = 0
    profitable_edge = None
    for lower, label, stats in bucket_info:
        if stats["avg_pnl"] < 0:
            losing_count += stats["trades"]
            losing_pnl += stats["total_pnl"]
        elif profitable_edge is None:
            profitable_edge = lower

    if profitable_edge and profitable_edge > current and losing_count >= MIN_SAMPLE_SIZE:
        suggestions.append({
            "param": "MIN_EDGE_CENTS",
            "current_value": current,
            "suggested_value": profitable_edge,
            "reasoning": (
                f"Trades with edge <{profitable_edge}c lose ${abs(losing_pnl)/100:.2f} total "
                f"({losing_count} trades). Raising from {current}c to {profitable_edge}c "
                f"filters out losing entries."
            ),
            "sample_size": losing_count,
            "confidence": _confidence_level(losing_count),
        })


def _suggest_min_confidence(conf_data: dict, suggestions: list):
    """If low-confidence trades underperform, suggest raising RULE_MIN_CONFIDENCE."""
    current = config.RULE_MIN_CONFIDENCE

    # Ordered bucket labels from low to high
    ordered = ["<60%", "60-69%", "70-79%", "80%+"]
    thresholds = [0.6, 0.6, 0.7, 0.8]

    losing_count = 0
    profitable_threshold = None
    for label, thresh in zip(ordered, thresholds):
        bucket = conf_data.get(label, {})
        if bucket.get("trades", 0) < MIN_SAMPLE_SIZE:
            continue
        if bucket.get("avg_pnl", 0) < 0:
            losing_count += bucket["trades"]
        elif profitable_threshold is None:
            profitable_threshold = thresh
            break

    if profitable_threshold and profitable_threshold > current and losing_count >= MIN_SAMPLE_SIZE:
        suggestions.append({
            "param": "RULE_MIN_CONFIDENCE",
            "current_value": current,
            "suggested_value": profitable_threshold,
            "reasoning": (
                f"Trades below {profitable_threshold:.0%} confidence have negative P&L "
                f"({losing_count} trades). Raising from {current:.0%} to {profitable_threshold:.0%} "
                f"filters out unprofitable low-confidence entries."
            ),
            "sample_size": losing_count,
            "confidence": _confidence_level(losing_count),
        })


def _suggest_min_time(time_data: dict, suggestions: list):
    """If trades near expiry lose money, suggest raising MIN_SECONDS_TO_CLOSE."""
    current = config.MIN_SECONDS_TO_CLOSE

    short_time = time_data.get("90-180s", {})
    if short_time.get("trades", 0) < MIN_SAMPLE_SIZE or short_time.get("avg_pnl", 0) >= 0:
        return

    # Check if longer-time trades are profitable
    longer_profitable = any(
        time_data.get(label, {}).get("avg_pnl", 0) > 0
        for label in ["180-360s", "360-600s", "600s+"]
        if time_data.get(label, {}).get("trades", 0) >= MIN_SAMPLE_SIZE
    )
    if not longer_profitable:
        return

    suggestions.append({
        "param": "MIN_SECONDS_TO_CLOSE",
        "current_value": current,
        "suggested_value": 180,
        "reasoning": (
            f"Trades entered with <180s left lose avg "
            f"${abs(short_time['avg_pnl'])/100:.2f}/trade ({short_time['trades']} trades). "
            f"Trades with more time are profitable."
        ),
        "sample_size": short_time["trades"],
        "confidence": _confidence_level(short_time["trades"]),
    })


def _suggest_vol_threshold(vol_data: dict, snapshots: list, suggestions: list):
    """If low-vol trades lose money, suggest adjusting VOL_LOW_THRESHOLD."""
    current = config.VOL_LOW_THRESHOLD
    low_vol = vol_data.get("low", {})

    if low_vol.get("trades", 0) < MIN_SAMPLE_SIZE or low_vol.get("avg_pnl", 0) >= 0:
        return

    # Find actual vol values of low-vol losers
    low_vol_trades = [s for s in snapshots if (s.get("entry_vol_regime") or "") == "low"]
    if not low_vol_trades:
        return

    max_low_vol = max(s.get("entry_vol", 0) or 0 for s in low_vol_trades)
    suggested = min(round(max_low_vol * 1.1), round(config.VOL_HIGH_THRESHOLD * 0.8))
    if suggested <= current:
        return

    suggestions.append({
        "param": "VOL_LOW_THRESHOLD",
        "current_value": current,
        "suggested_value": round(suggested, 0),
        "reasoning": (
            f"Low-vol trades lose ${abs(low_vol['total_pnl'])/100:.2f} total "
            f"({low_vol['trades']} trades, {low_vol['win_rate']:.0%} win rate). "
            f"Raise threshold from ${current:.0f} to ${suggested:.0f}/min to sit out more."
        ),
        "sample_size": low_vol["trades"],
        "confidence": _confidence_level(low_vol["trades"]),
    })


def _suggest_stop_loss(exit_data: dict, snapshots: list, suggestions: list):
    """If stop-loss fires too often, suggest widening."""
    current = config.STOP_LOSS_CENTS
    sl_exits = exit_data.get("SL", {})

    if not sl_exits or sl_exits.get("trades", 0) < MIN_SAMPLE_SIZE:
        return

    total = sum(s.get("trades", 0) for s in exit_data.values())
    if total == 0:
        return
    sl_rate = sl_exits["trades"] / total

    if sl_rate <= 0.30:
        return

    # Check avg time left when SL fires
    sl_snapshots = [s for s in snapshots if s.get("action") == "SL"]
    avg_time_left = (
        sum(s.get("secs_left", 0) or 0 for s in sl_snapshots) / len(sl_snapshots)
        if sl_snapshots else 0
    )

    suggestions.append({
        "param": "STOP_LOSS_CENTS",
        "current_value": current,
        "suggested_value": current + 5,
        "reasoning": (
            f"Stop-loss fires on {sl_rate:.0%} of exits ({sl_exits['trades']}/{total} trades). "
            f"Avg {avg_time_left:.0f}s remaining when triggered. "
            f"Consider widening from {current}c to {current + 5}c to reduce whipsaws."
        ),
        "sample_size": sl_exits["trades"],
        "confidence": _confidence_level(sl_exits["trades"]),
    })


def _suggest_fair_value_k(snapshots: list, suggestions: list):
    """If fair value predictions are inaccurate, suggest adjusting K."""
    current_k = config.FAIR_VALUE_K

    accurate = 0
    inaccurate = 0

    for s in snapshots:
        fv = s.get("entry_fair_yes_cents", 0) or 0
        side = s.get("side", "")
        pnl = s.get("pnl_cents", 0) or 0

        if side == "yes" and fv > 50 and pnl > 0:
            accurate += 1
        elif side == "no" and fv < 50 and pnl > 0:
            accurate += 1
        elif pnl <= 0:
            inaccurate += 1

    total = accurate + inaccurate
    if total < MIN_SAMPLE_SIZE:
        return

    accuracy_rate = accurate / total

    if accuracy_rate < 0.45:
        suggested = max(0.3, round(current_k - 0.1, 2))
        if suggested >= current_k:
            return
        suggestions.append({
            "param": "FAIR_VALUE_K",
            "current_value": current_k,
            "suggested_value": suggested,
            "reasoning": (
                f"Fair value predictions correct {accuracy_rate:.0%} of the time "
                f"({accurate}/{total} trades). A lower K ({suggested}) produces "
                f"less extreme probabilities, which may improve edge detection."
            ),
            "sample_size": total,
            "confidence": "low",
        })
