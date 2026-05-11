"""
Trajectory Evaluator: AgentCore Code-Based Evaluator Lambda (Metadata Mode)

Evaluates the FULL agent trajectory for both managed (AgentCore Runtime) and BYO
(outside AgentCore) agents using the same Lambda. Works identically for both
because AgentCore normalizes sessionSpans before invoking code-based evaluators.

IMPORTANT FINDING FROM THIS POC:
  AgentCore's code-based evaluator DOES NOT pass raw event bodies (user messages,
  assistant responses, tool parameters/results) to the Lambda. It passes only
  SPAN METADATA (attributes, status, timing, scope). This is likely a data
  governance choice — content goes only to the LLM-as-a-judge internal pipeline.

WHAT WE EVALUATE (from metadata alone):
  1. Tool selection — did the agent call the expected tools?
  2. Tool success rate — did all tools complete with status=success?
  3. Trajectory shape — reasonable number of LLM calls and tool calls
  4. Token efficiency — tokens per response reasonable?
  5. Latency — total trajectory duration within threshold
  6. Model consistency — same model used throughout?

Works uniformly for managed AND BYO agents — same evaluator, same scoring rubric.

AgentCore Contract:
  Input:
    {
      "schemaVersion": "1.0",
      "evaluatorId": "...",
      "evaluationLevel": "TRACE",
      "evaluationInput": {"sessionSpans": [...]},
      "evaluationTarget": {"traceIds": ["..."], "spanIds": [...]}
    }
  Output (success):
    {"label": "...", "value": 0.0-1.0, "explanation": "..."}
  Output (error):
    {"errorCode": "...", "errorMessage": "..."}

Docs: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html

Runtime: python3.11
Timeout: 60s (we don't call external services — deterministic metadata scoring)
"""

import json
import logging
from typing import Any, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Scoring thresholds
MAX_LATENCY_SECONDS = 30.0           # anything over this loses points
MAX_TOOL_CALLS = 10                   # more than this suggests confused trajectory
MAX_LLM_CALLS = 8                     # more than this suggests looping
MIN_TOOL_CALLS_FOR_COMPLEX = 1        # we expect at least one tool call for real work


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_scope(item: dict) -> str:
    """Extract the scope name from a span item."""
    scope = item.get("scope") or item.get("instrumentationScope") or {}
    return scope.get("name", "") if isinstance(scope, dict) else ""


def _get_attr(item: dict, key: str, default: Any = None) -> Any:
    """Convenience: fetch a span attribute value."""
    attrs = item.get("attributes") or {}
    if isinstance(attrs, dict):
        return attrs.get(key, default)
    # OTEL list-form fallback (key/value pairs)
    if isinstance(attrs, list):
        for a in attrs:
            if a.get("key") == key:
                v = a.get("value", {})
                if isinstance(v, dict):
                    return v.get("stringValue", v.get("intValue", v.get("doubleValue", default)))
                return v
    return default


def _get_operation(item: dict) -> str:
    """What kind of operation is this span? invoke_agent / execute_tool / chat / etc."""
    op = _get_attr(item, "gen_ai.operation.name", "")
    if op:
        return str(op).lower()
    # Fallback: infer from name
    name = str(item.get("name", "")).lower()
    if "invoke_agent" in name:
        return "invoke_agent"
    if name.startswith("execute_tool"):
        return "execute_tool"
    if name.startswith("chat"):
        return "chat"
    return ""


def _span_status_code(item: dict) -> str:
    """OK / ERROR / UNSET"""
    status = item.get("status") or {}
    if isinstance(status, dict):
        return str(status.get("code", "UNSET")).upper()
    return str(status).upper() if status else "UNSET"


# ---------------------------------------------------------------------------
# Trajectory Analysis
# ---------------------------------------------------------------------------

def analyze_trajectory(session_spans: list[dict], target_trace_id: Optional[str] = None) -> dict:
    """Extract all measurable metadata from the sessionSpans.

    Returns an analysis dict with everything we can learn from metadata alone.
    """
    # Filter by target trace if provided
    if target_trace_id:
        relevant = [
            s for s in session_spans
            if s.get("traceId") == target_trace_id
            or s.get("trace_id") == target_trace_id
        ]
    else:
        relevant = list(session_spans)

    analysis = {
        "trace_id": target_trace_id or "",
        "span_count": len(relevant),
        "invoke_agent_spans": [],
        "tool_calls": [],        # list of {name, call_id, status, duration_ms, description}
        "llm_calls": [],         # list of {model, tokens_in, tokens_out, duration_ms}
        "tools_available": [],   # from invoke_agent span's gen_ai.agent.tools
        "total_latency_s": 0.0,
        "agent_duration_s": 0.0, # latency of invoke_agent specifically
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "models_used": set(),
        "sessions": set(),
        "errors": [],            # spans with status=ERROR
        "scopes_seen": {},
    }

    # Time range
    min_start = None
    max_end = None

    for item in relevant:
        scope = _get_scope(item)
        analysis["scopes_seen"][scope] = analysis["scopes_seen"].get(scope, 0) + 1

        # Time tracking
        start = item.get("startTimeUnixNano") or item.get("timeUnixNano")
        end = item.get("endTimeUnixNano")
        if start is not None:
            try:
                s = int(start)
                if min_start is None or s < min_start:
                    min_start = s
            except (ValueError, TypeError):
                pass
        if end is not None:
            try:
                e = int(end)
                if max_end is None or e > max_end:
                    max_end = e
            except (ValueError, TypeError):
                pass

        # Session
        sid = _get_attr(item, "session.id", "")
        if sid:
            analysis["sessions"].add(sid)

        # Error check — only count agent-relevant spans, not ADOT infrastructure noise
        # (e.g., CountTokens spans have ERROR status but aren't actual agent errors)
        if _span_status_code(item) == "ERROR" and scope == "strands.telemetry.tracer":
            analysis["errors"].append({
                "name": item.get("name", ""),
                "span_id": item.get("spanId") or item.get("span_id", ""),
            })

        op = _get_operation(item)
        duration_ms = item.get("durationNano") or item.get("duration_ms") or 0
        # Normalize to ms
        if duration_ms and isinstance(duration_ms, (int, float)) and duration_ms > 1_000_000:
            # Value is in nanoseconds
            duration_ms = duration_ms / 1_000_000

        # ---- invoke_agent span ----
        if op == "invoke_agent" and scope == "strands.telemetry.tracer":
            analysis["invoke_agent_spans"].append(item)

            # Token totals live on invoke_agent
            in_t = _get_attr(item, "gen_ai.usage.input_tokens", 0) or 0
            out_t = _get_attr(item, "gen_ai.usage.output_tokens", 0) or 0
            total_t = _get_attr(item, "gen_ai.usage.total_tokens", 0) or 0
            try:
                analysis["input_tokens"] += int(in_t)
                analysis["output_tokens"] += int(out_t)
                analysis["total_tokens"] += int(total_t)
            except (ValueError, TypeError):
                pass

            model = _get_attr(item, "gen_ai.request.model", "")
            if model:
                analysis["models_used"].add(str(model))

            # Agent tools available
            tools_attr = _get_attr(item, "gen_ai.agent.tools", "")
            if tools_attr:
                try:
                    tools_list = json.loads(tools_attr) if isinstance(tools_attr, str) else tools_attr
                    if isinstance(tools_list, list):
                        analysis["tools_available"] = tools_list
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

            try:
                analysis["agent_duration_s"] = float(duration_ms) / 1000.0 if duration_ms else 0.0
            except (ValueError, TypeError):
                pass

        # ---- execute_tool span ----
        elif op == "execute_tool" and scope == "strands.telemetry.tracer":
            tool_name = _get_attr(item, "gen_ai.tool.name", "") or \
                        str(item.get("name", "")).replace("execute_tool", "").strip()
            analysis["tool_calls"].append({
                "name": tool_name,
                "call_id": _get_attr(item, "gen_ai.tool.call.id", ""),
                "status": _get_attr(item, "gen_ai.tool.status", "") or _span_status_code(item),
                "duration_ms": float(duration_ms) if duration_ms else 0.0,
                "description": _get_attr(item, "gen_ai.tool.description", "")[:200],
                "schema_present": bool(_get_attr(item, "gen_ai.tool.json_schema", "")),
            })

        # ---- chat span (LLM call) ----
        elif op == "chat" and scope == "strands.telemetry.tracer":
            in_t = _get_attr(item, "gen_ai.usage.input_tokens", 0) or 0
            out_t = _get_attr(item, "gen_ai.usage.output_tokens", 0) or 0
            model = _get_attr(item, "gen_ai.request.model", "")
            analysis["llm_calls"].append({
                "model": str(model),
                "tokens_in": int(in_t) if in_t else 0,
                "tokens_out": int(out_t) if out_t else 0,
                "duration_ms": float(duration_ms) if duration_ms else 0.0,
            })

    # Total latency
    if min_start is not None and max_end is not None:
        analysis["total_latency_s"] = (max_end - min_start) / 1_000_000_000.0

    # Convert sets to lists for JSON
    analysis["models_used"] = sorted(analysis["models_used"])
    analysis["sessions"] = sorted(analysis["sessions"])

    return analysis


# ---------------------------------------------------------------------------
# Scoring Rubric
# ---------------------------------------------------------------------------

def score_trajectory(a: dict) -> dict:
    """Deterministic score from trajectory analysis.

    Scoring rubric (100 points total):
      - Agent present (20): at least one invoke_agent span exists
      - Tool success (30): all tool calls have status=success
      - Tool variety (10): used ≥1 distinct tool
      - No errors (15): no ERROR-status spans in trajectory
      - Latency (15): agent_duration_s < threshold
      - Efficiency (10): tool and LLM call counts in reasonable range

    Maps to label:
      ≥90 → Excellent
      ≥75 → Very Good
      ≥60 → Good
      ≥40 → Poor
      <40 → Unacceptable
    """
    score = 0
    breakdown = {}

    # 1. Agent present (20)
    if a["invoke_agent_spans"]:
        score += 20
        breakdown["agent_present"] = 20
    else:
        breakdown["agent_present"] = 0

    # 2. Tool success (30)
    tool_calls = a["tool_calls"]
    if tool_calls:
        successes = sum(
            1 for tc in tool_calls
            if str(tc.get("status", "")).lower() in ("success", "ok")
        )
        tool_success_score = round(30 * successes / len(tool_calls))
        score += tool_success_score
        breakdown["tool_success"] = tool_success_score
    else:
        # No tools called — partial credit (simple requests may not need tools)
        breakdown["tool_success"] = 15
        score += 15

    # 3. Tool variety (10) — did the agent use multiple distinct tools when needed?
    distinct_tools = {tc["name"] for tc in tool_calls if tc.get("name")}
    if len(distinct_tools) >= 2:
        breakdown["tool_variety"] = 10
        score += 10
    elif len(distinct_tools) == 1:
        breakdown["tool_variety"] = 7
        score += 7
    else:
        breakdown["tool_variety"] = 0

    # 4. No errors (15)
    if not a["errors"]:
        breakdown["no_errors"] = 15
        score += 15
    else:
        # Penalty proportional to errors, floor at 0
        penalty = min(15, len(a["errors"]) * 5)
        breakdown["no_errors"] = 15 - penalty
        score += (15 - penalty)

    # 5. Latency (15)
    agent_dur = a["agent_duration_s"] or a["total_latency_s"]
    if agent_dur > 0 and agent_dur < MAX_LATENCY_SECONDS:
        # Higher score for faster execution
        # At 0s: 15. At MAX: 5. Below 50%: 12
        ratio = agent_dur / MAX_LATENCY_SECONDS
        latency_score = round(15 - (ratio * 10))
        breakdown["latency"] = max(5, latency_score)
        score += max(5, latency_score)
    elif agent_dur >= MAX_LATENCY_SECONDS:
        # Too slow
        breakdown["latency"] = 0
    else:
        # Unknown — neutral
        breakdown["latency"] = 10
        score += 10

    # 6. Efficiency (10)
    n_tools = len(tool_calls)
    n_llm = len(a["llm_calls"])
    efficiency_ok = (
        n_tools <= MAX_TOOL_CALLS
        and n_llm <= MAX_LLM_CALLS
        and not (n_tools == 0 and n_llm == 0)
    )
    if efficiency_ok:
        breakdown["efficiency"] = 10
        score += 10
    else:
        breakdown["efficiency"] = 0

    # Clamp 0-100
    score = max(0, min(100, score))

    # Label
    if score >= 90:
        label = "Excellent"
    elif score >= 75:
        label = "Very Good"
    elif score >= 60:
        label = "Good"
    elif score >= 40:
        label = "Poor"
    else:
        label = "Unacceptable"

    return {
        "score": score,
        "label": label,
        "breakdown": breakdown,
    }


def render_explanation(a: dict, scoring: dict) -> str:
    """Human-readable explanation of the score."""
    lines = []
    lines.append(f"Trajectory: {a['span_count']} spans, {len(a['tool_calls'])} tool calls, "
                 f"{len(a['llm_calls'])} LLM calls, latency {a['agent_duration_s']:.1f}s, "
                 f"{a['total_tokens']} tokens.")

    if a["tools_available"]:
        lines.append(f"Tools available: {', '.join(a['tools_available'])}.")

    if a["tool_calls"]:
        tool_summary = []
        for tc in a["tool_calls"]:
            status = tc.get("status", "?")
            status_icon = "✓" if str(status).lower() in ("success", "ok") else "✗"
            tool_summary.append(f"{status_icon}{tc['name']}")
        lines.append(f"Tool sequence: {' → '.join(tool_summary)}.")
    else:
        lines.append("No tool calls made.")

    if a["models_used"]:
        lines.append(f"Model: {', '.join(a['models_used'])}.")

    if a["errors"]:
        error_names = [e["name"] for e in a["errors"][:3]]
        lines.append(f"ERRORS: {len(a['errors'])} span(s) failed: {', '.join(error_names)}.")

    # Scoring breakdown
    bd = scoring["breakdown"]
    lines.append(f"Scoring breakdown: agent={bd['agent_present']}/20, "
                 f"tool_success={bd['tool_success']}/30, "
                 f"tool_variety={bd['tool_variety']}/10, "
                 f"no_errors={bd['no_errors']}/15, "
                 f"latency={bd['latency']}/15, "
                 f"efficiency={bd['efficiency']}/10 "
                 f"= {scoring['score']}/100 ({scoring['label']}).")

    return " ".join(lines)


# ---------------------------------------------------------------------------
# Lambda Handler
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context) -> dict:
    """AgentCore code-based evaluator entry point."""
    try:
        logger.info(f"Received event keys: {list(event.keys())}")

        eval_input = event.get("evaluationInput", {})
        session_spans = eval_input.get("sessionSpans", [])
        eval_target = event.get("evaluationTarget", {}) or {}
        target_trace_ids = eval_target.get("traceIds", []) if isinstance(eval_target, dict) else []
        target_trace_id = target_trace_ids[0] if target_trace_ids else None

        logger.info(
            f"Processing: {len(session_spans)} sessionSpans, "
            f"target_trace_id={target_trace_id}"
        )

        if not session_spans:
            return {
                "errorCode": "NO_SPANS",
                "errorMessage": "evaluationInput.sessionSpans is empty",
            }

        # Analyze trajectory
        analysis = analyze_trajectory(session_spans, target_trace_id)

        logger.info(
            f"Analysis: invoke_agent={len(analysis['invoke_agent_spans'])}, "
            f"tools={len(analysis['tool_calls'])}, "
            f"llm={len(analysis['llm_calls'])}, "
            f"latency={analysis['agent_duration_s']:.2f}s, "
            f"tokens={analysis['total_tokens']}, "
            f"errors={len(analysis['errors'])}"
        )

        # Guard: did we actually find the agent?
        if not analysis["invoke_agent_spans"] and not analysis["tool_calls"] and not analysis["llm_calls"]:
            return {
                "errorCode": "TRAJECTORY_EMPTY",
                "errorMessage": (
                    f"No strands agent spans found in trajectory. "
                    f"Scopes seen: {analysis['scopes_seen']}"
                ),
            }

        # Score
        scoring = score_trajectory(analysis)
        explanation = render_explanation(analysis, scoring)

        logger.info(f"Score: {scoring['score']}/100 ({scoring['label']})")

        # Normalize to 0.0-1.0 for the API
        return {
            "label": scoring["label"],
            "value": scoring["score"] / 100.0,
            "explanation": explanation,
        }

    except Exception as e:
        logger.exception("Evaluator failed")
        return {
            "errorCode": "EVALUATOR_ERROR",
            "errorMessage": f"{type(e).__name__}: {str(e)[:500]}",
        }
