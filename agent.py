import json
import config
from database import log_event, record_decision

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


TRADING_SYSTEM_PROMPT = """You are a disciplined BTC derivatives trader on Kalshi 15-minute binary options.

Key rules:
- Contracts pay $1 if BTC is above the strike at settlement (YES wins), $0 otherwise (NO wins)
- You choose BUY_YES, BUY_NO, or HOLD with a confidence from 0.0 to 1.0
- Only trade when mispricing exists: fair_value differs meaningfully from the market price
- We trade trends, not reversals. Follow momentum when volatility is high.
- Avoid contracts priced below 5c or above 85c (bad risk/reward on both sides)
- Higher volatility = more uncertainty = fair value pulled toward 50c
- Near expiry with BTC far from strike, the outcome becomes more certain — be aggressive on the winning side
- Edge = fair_value - market_price. Positive edge on a side = buy opportunity on that side.
- If both sides have edge, pick the one with more edge and trend confirmation
- HOLD when there is no clear mispricing or when you are uncertain

Always respond with valid JSON only — no markdown, no extra text:
{"decision": "BUY_YES"|"BUY_NO"|"HOLD", "confidence": 0.0-1.0, "reasoning": "..."}"""


def _build_trading_prompt(market_data: dict, current_position: dict | None,
                          alpha_monitor=None) -> str:
    """Build a rich user prompt with all available market data."""
    parts = [f"Market data: {json.dumps(market_data, default=str)}"]

    if current_position:
        parts.append(f"Current position: {json.dumps(current_position, default=str)}")
    else:
        parts.append("Current position: None")

    # Enrich with alpha engine data
    if alpha_monitor:
        strike = market_data.get("strike_price", 0)
        secs_left = market_data.get("seconds_to_close", 0)

        if strike and strike > 0:
            fv = alpha_monitor.get_fair_value(strike, secs_left)
            parts.append(f"Fair value analysis: {json.dumps(fv, default=str)}")

            best_ask = market_data.get("best_ask", 100)
            best_bid = market_data.get("best_bid", 0)
            yes_edge = fv["fair_yes_cents"] - best_ask
            no_edge = (100 - fv["fair_yes_cents"]) - (100 - best_bid)
            parts.append(f"YES edge: {yes_edge:+d}c (fair {fv['fair_yes_cents']}c vs ask {best_ask}c)")
            parts.append(f"NO edge: {no_edge:+d}c (fair {100 - fv['fair_yes_cents']}c vs cost {100 - best_bid}c)")

        vol = alpha_monitor.get_volatility()
        parts.append(f"Volatility: {json.dumps(vol, default=str)}")

        vel = alpha_monitor.get_price_velocity()
        parts.append(f"Price velocity: {json.dumps(vel, default=str)}")

        # Latency delta
        delta = getattr(alpha_monitor, 'latency_delta', None)
        if delta is not None:
            parts.append(f"Binance-Coinbase delta: ${delta:+.1f}")

    parts.append(
        "\nReturn valid JSON with exactly these keys:\n"
        '{"decision": "BUY_YES"|"BUY_NO"|"HOLD", '
        '"confidence": 0.0-1.0, '
        '"reasoning": "..."}'
    )
    return "\n".join(parts)


class MarketAgent:
    def __init__(self):
        self.client = None
        if HAS_ANTHROPIC and config.ANTHROPIC_API_KEY:
            self.client = anthropic.AsyncAnthropic(
                api_key=config.ANTHROPIC_API_KEY,
                timeout=60.0,
            )
        self.last_decision: dict | None = None

    # ------------------------------------------------------------------
    # Claude AI trading decision
    # ------------------------------------------------------------------

    async def analyze_market(
        self, market_data: dict, current_position: dict | None = None,
        alpha_monitor=None,
    ) -> dict:
        """Call Claude Haiku to get a trading decision.

        Sends enriched market data (fair value, volatility, velocity, edge)
        and returns dict with keys: decision, confidence, reasoning.
        Falls back to HOLD on any error.
        """
        if not self.client:
            return self._hold("No Anthropic client — ANTHROPIC_API_KEY not set")

        user_msg = _build_trading_prompt(market_data, current_position, alpha_monitor)

        try:
            response = await self.client.messages.create(
                model="claude-3-5-haiku-latest",
                max_tokens=300,
                system=TRADING_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
                timeout=5.0,
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
            log_event("AGENT", f"{decision} ({confidence:.0%}) — {reasoning[:200]}")
            return self.last_decision

        except json.JSONDecodeError as exc:
            log_event("ERROR", f"Agent returned invalid JSON: {exc}")
        except Exception as exc:
            log_event("ERROR", f"Agent error: {exc}")

        return self._hold("Agent error — defaulting to HOLD.")

    def _hold(self, reasoning: str, confidence: float = 0.0) -> dict:
        """Return a HOLD decision with optional confidence score."""
        result = {"decision": "HOLD", "confidence": confidence, "reasoning": reasoning}
        self.last_decision = result
        log_event("AGENT", f"HOLD — {reasoning[:200]}")
        return result

    # ------------------------------------------------------------------
    # Chat (still uses Anthropic API)
    # ------------------------------------------------------------------

    async def chat(self, user_message: str, bot_status: dict | None = None,
                   trades_summary: dict | None = None, config: dict | None = None,
                   history: list[dict] | None = None,
                   config_updater: callable = None) -> str:
        """Free-form chat with the agent about markets / strategy."""
        if not self.client:
            return "Chat requires ANTHROPIC_API_KEY to be set."

        # Build rich context from live data
        context_parts = []

        if bot_status:
            # Extract key metrics for context
            dashboard = bot_status.get("dashboard", {})
            ctx = {
                "running": bot_status.get("running"),
                "balance": bot_status.get("balance"),
                "position": bot_status.get("position"),
                "market": bot_status.get("market"),
                "last_action": bot_status.get("last_action"),
                "decision": bot_status.get("decision"),
                "confidence": bot_status.get("confidence"),
                "reasoning": bot_status.get("reasoning"),
            }
            # Dashboard alpha signals
            if dashboard:
                ctx["btc_price"] = dashboard.get("btc_price")
                ctx["strike"] = dashboard.get("strike")
                ctx["volatility"] = dashboard.get("volatility")
                ctx["momentum"] = dashboard.get("momentum")
                ctx["secs_left"] = dashboard.get("secs_left")
                ctx["yes_edge"] = dashboard.get("yes_edge")
                ctx["no_edge"] = dashboard.get("no_edge")
                ctx["fair_value"] = dashboard.get("fair_value")
                ctx["rolling_avg_confidence"] = dashboard.get("rolling_avg_confidence")
                ctx["rolling_avg_max_confidence"] = dashboard.get("rolling_avg_max_confidence")
            context_parts.append(f"LIVE STATUS:\n{json.dumps(ctx, default=str, indent=2)}")

        if trades_summary:
            context_parts.append(f"TRADING PERFORMANCE:\n{json.dumps(trades_summary, indent=2)}")

        if config:
            # Only include key config values
            key_config = {k: v.get("value") if isinstance(v, dict) else v
                         for k, v in config.items()
                         if k in ["MIN_EDGE_CENTS", "MIN_AGENT_CONFIDENCE", "VOL_HIGH_THRESHOLD",
                                  "VOL_LOW_THRESHOLD", "DELTA_THRESHOLD"]}
            context_parts.append(f"KEY CONFIG:\n{json.dumps(key_config, indent=2)}")

        if self.last_decision:
            context_parts.append(f"LAST DECISION:\n{json.dumps(self.last_decision, indent=2)}")

        context = "\n\n".join(context_parts) + "\n\n" if context_parts else ""

        system_prompt = """You are the AI advisor for a Kalshi BTC 15-minute binary options trading bot. You have access to live data about the bot's performance, current market conditions, and configuration.

Your role is to:
1. Answer questions about current market conditions and the bot's decisions
2. Analyze trading performance and suggest improvements
3. Recommend config adjustments based on observed patterns
4. Explain why the bot is making certain decisions
5. Help interpret alpha signals (momentum, volatility, fair value)
6. USE THE update_config TOOL when the user asks you to change settings

Key concepts:
- YES/NO are binary outcomes based on whether BTC price is above/below the strike at settlement
- Edge = fair_value - market_price (positive edge means opportunity)
- Volatility affects fair value calculation (higher vol = more uncertainty = prices closer to 50c)
- Confidence threshold determines whether to trade
- The trading agent (Claude Haiku) makes BUY_YES/BUY_NO/HOLD decisions each cycle

Available config parameters you can change:
- MIN_AGENT_CONFIDENCE: Minimum confidence to trade (0.0-1.0, default 0.75)
- MIN_EDGE_CENTS: Minimum edge in cents to trade (1-20, default 5)
- VOL_HIGH_THRESHOLD: High volatility threshold $/min (50-2000, default 400)
- VOL_LOW_THRESHOLD: Low volatility threshold $/min (20-1000, default 200)
- DELTA_THRESHOLD: Momentum threshold for front-run (5-100, default 20)

When asked to change settings, USE THE TOOL - don't just suggest changes. After changing, confirm what you changed."""

        # Define tool for updating config
        tools = [
            {
                "name": "update_config",
                "description": "Update a bot configuration setting. Use this when the user asks to change a setting.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "setting": {
                            "type": "string",
                            "description": "The config key to update (e.g., MIN_AGENT_CONFIDENCE, MIN_EDGE_CENTS)"
                        },
                        "value": {
                            "type": "number",
                            "description": "The new value for the setting"
                        }
                    },
                    "required": ["setting", "value"]
                }
            }
        ]

        # Build messages list with history
        messages = []

        # First message includes live context
        if history:
            # Add context to first user message, then include history
            first_msg = history[0] if history else None
            if first_msg and first_msg.get("role") == "user":
                messages.append({"role": "user", "content": context + first_msg["content"]})
                messages.extend(history[1:])
            else:
                messages.extend(history)
            # Add current message
            messages.append({"role": "user", "content": user_message})
        else:
            # No history - single message with context
            messages.append({"role": "user", "content": context + "USER QUESTION: " + user_message})

        try:
            response = await self.client.messages.create(
                model="claude-3-5-haiku-latest",
                max_tokens=800,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )

            # Check for tool use
            tool_results = []
            final_text = ""

            for block in response.content:
                if block.type == "text":
                    final_text += block.text
                elif block.type == "tool_use" and block.name == "update_config":
                    # Execute config update
                    setting = block.input.get("setting")
                    value = block.input.get("value")
                    result = {"success": False, "message": "No config updater available"}

                    if config_updater and setting and value is not None:
                        try:
                            applied = config_updater({setting: value})
                            if applied:
                                result = {"success": True, "message": f"Updated {setting} to {value}"}
                            else:
                                result = {"success": False, "message": f"Failed to update {setting}"}
                        except Exception as e:
                            result = {"success": False, "message": str(e)}

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result)
                    })

            # If there were tool calls, get final response
            if tool_results:
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

                final_response = await self.client.messages.create(
                    model="claude-3-5-haiku-latest",
                    max_tokens=400,
                    system=system_prompt,
                    messages=messages,
                )
                return final_response.content[0].text.strip()

            return final_text.strip() if final_text else "I couldn't generate a response."
        except Exception as exc:
            return f"Error: {exc}"
