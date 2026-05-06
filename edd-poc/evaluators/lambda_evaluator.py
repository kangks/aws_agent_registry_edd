"""
Custom Lambda Evaluator: multiplier_deterministic

Performs 4 deterministic checks on agent traces:
1. tool_usage    - At least one tool was called
2. empty_params  - No tool was called with empty/null parameters
3. response_content - Agent produced a non-empty response
4. latency       - Total trace duration is below threshold (30s)

Evaluator level: TRACE
Runtime: python3.11
"""

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Latency threshold in seconds
LATENCY_THRESHOLD_SECONDS = 30.0

EVALUATOR_NAME = "multiplier_deterministic"
TARGET_TYPE = "TRACE"


def _extract_spans(trace: dict) -> list:
    """Extract spans from a trace, handling various trace formats."""
    if "spans" in trace:
        return trace["spans"]
    if "resourceSpans" in trace:
        spans = []
        for resource_span in trace["resourceSpans"]:
            for scope_span in resource_span.get("scopeSpans", []):
                spans.extend(scope_span.get("spans", []))
        return spans
    return []


def _check_tool_usage(spans: list) -> bool:
    """Check that at least one tool was called in the trace."""
    for span in spans:
        name = span.get("name", "").lower()
        kind = span.get("kind", "")
        attributes = span.get("attributes", {})

        # Check span name for tool invocation patterns
        if "tool" in name:
            return True

        # Check attributes for tool-related markers
        if isinstance(attributes, dict):
            for key, value in attributes.items():
                if "tool" in key.lower():
                    return True

        # Check for attributes in list format (OTEL convention)
        if isinstance(attributes, list):
            for attr in attributes:
                key = attr.get("key", "")
                if "tool" in key.lower():
                    return True

    return False


def _check_empty_params(spans: list) -> bool:
    """Check that no tool was called with empty/null parameters.

    Returns True if all tool calls have non-empty parameters (pass).
    Returns True if no tool calls exist (nothing to fail on).
    Returns False if any tool call has empty/null parameters.
    """
    for span in spans:
        name = span.get("name", "").lower()
        attributes = span.get("attributes", {})

        # Only check spans that are tool invocations
        if "tool" not in name:
            continue

        # Check for parameters in attributes
        params = None

        if isinstance(attributes, dict):
            params = attributes.get("tool.parameters") or attributes.get(
                "gen_ai.tool.parameters"
            )
        elif isinstance(attributes, list):
            for attr in attributes:
                key = attr.get("key", "")
                if key in ("tool.parameters", "gen_ai.tool.parameters"):
                    params = attr.get("value", {}).get("stringValue", "")
                    break

        # If we found a tool span, check its parameters
        if params is not None:
            # Handle string params (JSON encoded)
            if isinstance(params, str):
                params = params.strip()
                if not params or params in ("null", "{}", "[]", ""):
                    return False
                try:
                    parsed = json.loads(params)
                    if not parsed:
                        return False
                except (json.JSONDecodeError, TypeError):
                    # Non-JSON string that's non-empty is acceptable
                    pass
            elif isinstance(params, dict):
                if not params:
                    return False
            elif params is None:
                return False

    return True


def _check_response_content(trace: dict, spans: list) -> bool:
    """Check that the agent produced a non-empty response."""
    # Check trace-level response field
    if "response" in trace:
        response = trace["response"]
        if isinstance(response, str) and response.strip():
            return True
        if isinstance(response, dict) and response:
            return True

    # Check output field
    if "output" in trace:
        output = trace["output"]
        if isinstance(output, str) and output.strip():
            return True
        if isinstance(output, dict) and output:
            return True

    # Check spans for response content
    for span in spans:
        attributes = span.get("attributes", {})

        if isinstance(attributes, dict):
            for key, value in attributes.items():
                if "response" in key.lower() or "output" in key.lower():
                    if isinstance(value, str) and value.strip():
                        return True
                    if value:
                        return True
        elif isinstance(attributes, list):
            for attr in attributes:
                key = attr.get("key", "")
                if "response" in key.lower() or "output" in key.lower():
                    val = attr.get("value", {})
                    str_val = val.get("stringValue", "")
                    if str_val and str_val.strip():
                        return True

    return False


def _check_latency(trace: dict, spans: list) -> bool:
    """Check that total trace duration is below the threshold.

    Returns True if latency is within threshold (pass).
    Returns False if latency exceeds threshold (fail).
    """
    # Check trace-level duration
    if "duration_ms" in trace:
        duration_s = trace["duration_ms"] / 1000.0
        return duration_s < LATENCY_THRESHOLD_SECONDS

    if "duration" in trace:
        duration = trace["duration"]
        # Assume seconds if it's a number
        if isinstance(duration, (int, float)):
            return duration < LATENCY_THRESHOLD_SECONDS

    # Calculate from spans using start/end times
    if not spans:
        # No spans to measure — pass by default
        return True

    min_start = None
    max_end = None

    for span in spans:
        start = span.get("startTimeUnixNano") or span.get("start_time")
        end = span.get("endTimeUnixNano") or span.get("end_time")

        if start is not None:
            start_val = int(start) if isinstance(start, (int, str)) else None
            if start_val is not None:
                if min_start is None or start_val < min_start:
                    min_start = start_val

        if end is not None:
            end_val = int(end) if isinstance(end, (int, str)) else None
            if end_val is not None:
                if max_end is None or end_val > max_end:
                    max_end = end_val

    if min_start is not None and max_end is not None:
        # OTEL uses nanoseconds
        duration_s = (max_end - min_start) / 1_000_000_000.0
        return duration_s < LATENCY_THRESHOLD_SECONDS

    # Cannot determine latency — pass by default
    return True


def lambda_handler(event, context):
    """Lambda entry point for the multiplier_deterministic evaluator.

    Args:
        event: Dict with "trace" and "evaluator_config" keys from AgentCore.
        context: Lambda context (unused).

    Returns:
        Dict with "score" (0.0-1.0) and "details" mapping check names to booleans.
    """
    try:
        # Validate input
        if not isinstance(event, dict):
            logger.error("Event is not a dict: %s", type(event))
            return {"score": 0.0, "details": {"error": "malformed_trace"}}

        trace = event.get("trace")

        if not isinstance(trace, dict):
            logger.error("Trace is missing or not a dict")
            return {"score": 0.0, "details": {"error": "malformed_trace"}}

        # Extract spans from trace
        spans = _extract_spans(trace)

        # Run 4 deterministic checks
        tool_usage = _check_tool_usage(spans)
        empty_params = _check_empty_params(spans)
        response_content = _check_response_content(trace, spans)
        latency = _check_latency(trace, spans)

        details = {
            "tool_usage": tool_usage,
            "empty_params": empty_params,
            "response_content": response_content,
            "latency": latency,
        }

        passing_checks = sum(1 for v in details.values() if v is True)
        score = passing_checks / 4

        logger.info(
            "Evaluation complete: score=%.2f, details=%s",
            score,
            json.dumps(details),
        )

        return {"score": score, "details": details}

    except Exception as e:
        logger.error("Unexpected error during evaluation: %s", str(e))
        return {"score": 0.0, "details": {"error": "malformed_trace"}}
