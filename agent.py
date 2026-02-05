import json
import anthropic
import config
from database import log_event, record_decision

SYSTEM_PROMPT = (
    "You are a disciplined crypto derivatives trader. "
    "The market is Kalshi BTC 15-min binaries. "
    "We trade trends, not reversals. "
    "We avoid contracts priced < 55 cents (lottery tickets). "
    "Always respond with valid JSON only — no markdown, no extra text."
)


def _build_user_prompt(market_data: dict, current_position: dict | None) -> str:
    return (
        f"Market data: {json.dumps(market_data)}\n"
        f"Current position: {json.dumps(current_position)}\n\n"
        "Return valid JSON with exactly these keys:\n"
        '{"decision": "BUY_YES"|"BUY_NO"|"HOLD", '
        '"confidence": 0.0-1.0, '
        '"reasoning": "..."}'
    )


class MarketAgent:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self.last_decision: dict | None = None

    def analyze_market(
        self, market_data: dict, current_position: dict | None = None,
        alpha_monitor=None
    ) -> dict:
        """Call Claude to get a trading decision.

        Returns dict with keys: decision, confidence, reasoning.
        Falls back to HOLD on any error.
        """
        user_msg = _build_user_prompt(market_data, current_position)

        try:
            response = self.client.messages.create(
                model="claude-3-5-haiku-latest",
                max_tokens=300,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text.strip()
            # Strip markdown fences if the model wraps them
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1]
                raw = raw.rsplit("```", 1)[0]
            result = json.loads(raw)

            # Validate expected keys
            decision = result.get("decision", "HOLD")
            if decision not in ("BUY_YES", "BUY_NO", "HOLD"):
                decision = "HOLD"

            confidence = float(result.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))

            reasoning = result.get("reasoning", "No reasoning provided.")

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
            log_event("AGENT", f"{decision} ({confidence:.0%}) — {reasoning[:120]}")
            return self.last_decision

        except json.JSONDecodeError as exc:
            log_event("ERROR", f"Agent returned invalid JSON: {exc}")
        except anthropic.APIError as exc:
            log_event("ERROR", f"Anthropic API error: {exc}")
        except Exception as exc:
            log_event("ERROR", f"Agent error: {exc}")

        fallback = {"decision": "HOLD", "confidence": 0.0, "reasoning": "Agent error — defaulting to HOLD."}
        self.last_decision = fallback
        return fallback

    def chat(self, user_message: str, bot_status: dict | None = None) -> str:
        """Free-form chat with the agent about markets / strategy."""
        context = ""
        if bot_status:
            context = f"Current bot status: {json.dumps(bot_status, default=str)}\n\n"
        if self.last_decision:
            context += f"Last trading decision: {json.dumps(self.last_decision)}\n\n"

        try:
            response = self.client.messages.create(
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
