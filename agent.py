import json
import config
from database import log_event, record_decision

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


class MarketAgent:
    def __init__(self):
        # Anthropic client is only needed for chat, not trading decisions
        self.client = None
        if HAS_ANTHROPIC and config.ANTHROPIC_API_KEY:
            self.client = anthropic.AsyncAnthropic(
                api_key=config.ANTHROPIC_API_KEY,
                timeout=60.0,
            )
        self.last_decision: dict | None = None

    # ------------------------------------------------------------------
    # Rule-based trading decision (replaces Claude API call)
    # ------------------------------------------------------------------

    def analyze_market(
        self, market_data: dict, current_position: dict | None = None,
        alpha_monitor=None,
    ) -> dict:
        """Rule-based trading decision using price history, volatility, and fair value.

        Evaluates 4 signal dimensions:
        1. Edge — is the contract mispriced vs fair value?
        2. Trend — is BTC price moving toward or away from strike?
        3. Volatility — high vol = trend-follow, low vol = sit out
        4. Time decay — conservative near expiry, aggressive with time

        Returns dict with keys: decision, confidence, reasoning.
        """
        strike = market_data.get("strike_price", 0)
        secs_left = market_data.get("seconds_to_close", 0)
        best_bid = market_data.get("best_bid", 0)
        best_ask = market_data.get("best_ask", 100)

        if not alpha_monitor or not strike or strike <= 0:
            return self._hold("No strike price or alpha data available")

        # 1. Fair value estimation
        fv = alpha_monitor.get_fair_value(strike, secs_left)
        fair_yes_cents = fv["fair_yes_cents"]
        fair_yes_prob = fv["fair_yes_prob"]
        btc_vs_strike = fv["btc_vs_strike"]

        # 2. Volatility regime
        vol = alpha_monitor.get_volatility()
        regime = vol["regime"]

        # 3. Price velocity / trend
        velocity = alpha_monitor.get_price_velocity()
        vel_1m = velocity["velocity_1m"]
        dir_1m = velocity["direction_1m"]
        change_1m = velocity["price_change_1m"]

        # 4. Time decay factor: 1.0 at contract open, 0.0 at expiry
        max_contract_secs = 900.0
        time_factor = min(1.0, max(0.0, secs_left / max_contract_secs))

        # Build reasoning trace
        reasons = []
        reasons.append(f"BTC {'above' if btc_vs_strike > 0 else 'below'} strike by ${abs(btc_vs_strike):.0f}")
        reasons.append(f"Fair: {fair_yes_cents}c YES ({fair_yes_prob:.0%})")
        reasons.append(f"Vol: {regime} (${vol['vol_dollar_per_min']:.1f}/min)")
        reasons.append(f"Trend: ${change_1m:+.0f}/1m")
        reasons.append(f"Time: {secs_left:.0f}s left")

        # Low-vol sit-out
        if config.RULE_SIT_OUT_LOW_VOL and regime == "low":
            return self._hold(f"Low vol — sitting out. {'; '.join(reasons)}")

        # Compute edge on each side
        yes_cost = best_ask
        no_cost = 100 - best_bid
        yes_edge = fair_yes_cents - yes_cost
        no_edge = (100 - fair_yes_cents) - no_cost

        reasons.append(f"YES edge: {yes_edge:+d}c (fair {fair_yes_cents} vs ask {yes_cost})")
        reasons.append(f"NO edge: {no_edge:+d}c (fair {100 - fair_yes_cents} vs cost {no_cost})")

        min_edge = config.MIN_EDGE_CENTS

        # Trend confirmation
        trend_confirms_yes = dir_1m > 0
        trend_confirms_no = dir_1m < 0

        # High vol: relax edge, add trend bonus
        if regime == "high":
            min_edge = max(3, min_edge - 3)

        # Score YES — edge/100 spreads confidence over a wider range
        yes_score = 0.0
        if yes_edge >= min_edge:
            yes_score = yes_edge / 100.0
            if trend_confirms_yes:
                yes_score += 0.10
                if regime == "high" and abs(vel_1m) > config.TREND_FOLLOW_VELOCITY:
                    yes_score += 0.05
            yes_score *= time_factor

        # Score NO
        no_score = 0.0
        if no_edge >= min_edge:
            no_score = no_edge / 100.0
            if trend_confirms_no:
                no_score += 0.10
                if regime == "high" and abs(vel_1m) > config.TREND_FOLLOW_VELOCITY:
                    no_score += 0.05
            no_score *= time_factor

        # Pick the best side
        decision = "HOLD"
        confidence = 0.0

        if yes_score > no_score and yes_score > 0:
            decision = "BUY_YES"
            confidence = min(0.95, 0.45 + yes_score)
            reasons.append(f"-> BUY YES (score {yes_score:.2f}, edge {yes_edge}c"
                           + (", trend OK" if trend_confirms_yes else "") + ")")
        elif no_score > yes_score and no_score > 0:
            decision = "BUY_NO"
            confidence = min(0.95, 0.45 + no_score)
            reasons.append(f"-> BUY NO (score {no_score:.2f}, edge {no_edge}c"
                           + (", trend OK" if trend_confirms_no else "") + ")")
        else:
            return self._hold(f"No edge. {'; '.join(reasons)}")

        # Confidence gate
        if confidence < config.RULE_MIN_CONFIDENCE:
            return self._hold(f"Low confidence {confidence:.0%}. {'; '.join(reasons)}")

        reasoning = "; ".join(reasons)
        self.last_decision = {
            "decision": decision,
            "confidence": confidence,
            "reasoning": reasoning,
        }

        record_decision(
            market_id=market_data.get("ticker"),
            decision=decision,
            confidence=confidence,
            reasoning=reasoning,
        )
        log_event("RULES", f"{decision} ({confidence:.0%}) — {reasoning[:200]}")
        return self.last_decision

    def _hold(self, reasoning: str) -> dict:
        """Return a HOLD decision."""
        result = {"decision": "HOLD", "confidence": 0.0, "reasoning": reasoning}
        self.last_decision = result
        log_event("RULES", f"HOLD — {reasoning[:200]}")
        return result

    # ------------------------------------------------------------------
    # Chat (still uses Anthropic API)
    # ------------------------------------------------------------------

    async def chat(self, user_message: str, bot_status: dict | None = None) -> str:
        """Free-form chat with the agent about markets / strategy."""
        if not self.client:
            return "Chat requires ANTHROPIC_API_KEY to be set."

        context = ""
        if bot_status:
            context = f"Current bot status: {json.dumps(bot_status, default=str)}\n\n"
        if self.last_decision:
            context += f"Last trading decision: {json.dumps(self.last_decision)}\n\n"

        try:
            response = await self.client.messages.create(
                model="claude-3-5-haiku-latest",
                max_tokens=600,
                system=(
                    "You are the AI agent powering a Kalshi BTC 15-min binary options auto-trader. "
                    "Answer the user's questions about the current market, your recent decisions, "
                    "trading strategy, or anything related. Be concise and direct."
                ),
                messages=[{"role": "user", "content": context + user_message}],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            return f"Error: {exc}"
