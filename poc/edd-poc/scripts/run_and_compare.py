"""
Model Comparison Script for the Multiplier EDD POC.

Runs 5 hardcoded prompts against each selected model, captures full agent traces
using InMemorySpanExporter, generates tool use graphs, runs ALL 8 SDK-based evaluators
(Helpfulness, Faithfulness, Coherence, Correctness, Harmfulness, ResponseRelevance,
ToolSelectionAccuracy, ToolParameterAccuracy) and custom evaluators via agentcore CLI,
and outputs a comparison table.

Usage:
    python scripts/run_and_compare.py --models sonnet,haiku,nova_pro
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Add parent directory to sys.path so we can import from agents/
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "agents"))

from agent import create_agent, SUPPORTED_MODELS  # noqa: E402

# ---------------------------------------------------------------------------
# Trace capture imports
# ---------------------------------------------------------------------------
from strands_evals import StrandsEvalsTelemetry
from strands_evals.mappers import StrandsInMemorySessionMapper

# ---------------------------------------------------------------------------
# SDK Evaluator imports
# ---------------------------------------------------------------------------
from strands_evals.evaluators import (
    HelpfulnessEvaluator,
    FaithfulnessEvaluator,
    CoherenceEvaluator,
    CorrectnessEvaluator,
    HarmfulnessEvaluator,
    ResponseRelevanceEvaluator,
    ToolSelectionAccuracyEvaluator,
    ToolParameterAccuracyEvaluator,
)
from strands_evals.types.evaluation import EvaluationData

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 5 hardcoded prompts covering different tools
EVALUATION_PROMPTS = [
    # 1. Employee lookup question
    "What is the employment status and department of employee EMP-12345 in Singapore?",
    # 2. Compliance question
    "What are the notice periods and regulatory requirements for employees in Thailand?",
    # 3. Payroll calculation question
    "Calculate the monthly payroll breakdown for an employee earning 90000 SGD annually in Singapore.",
    # 4. Leave management question
    "Check the leave balance for employee EMP-67890. How many days do they have remaining?",
    # 5. Multi-tool question (requires multiple tools)
    "For employee EMP-11111 in India, look up their details, check compliance rules, and calculate their payroll breakdown in INR.",
]

# All SDK evaluators (run locally via strands-agents-evals)
SDK_EVALUATORS = {
    "helpfulness": HelpfulnessEvaluator(),
    "faithfulness": FaithfulnessEvaluator(),
    "coherence": CoherenceEvaluator(),
    "correctness": CorrectnessEvaluator(),
    "harmfulness": HarmfulnessEvaluator(),
    "answer_relevancy": ResponseRelevanceEvaluator(),
    "tool_selection_accuracy": ToolSelectionAccuracyEvaluator(),
    "tool_parameter_accuracy": ToolParameterAccuracyEvaluator(),
}

SDK_EVALUATOR_NAMES = [
    "helpfulness",
    "faithfulness",
    "coherence",
    "correctness",
    "harmfulness",
    "answer_relevancy",
    "tool_selection_accuracy",
    "tool_parameter_accuracy",
]

# Custom evaluators (run via agentcore CLI if available)
CUSTOM_EVALUATORS = [
    "multiplier_domain_accuracy",
    "multiplier_deterministic",
]

ALL_EVALUATORS = SDK_EVALUATOR_NAMES + CUSTOM_EVALUATORS

# Estimated cost per 1K input/output tokens (USD) for cost-per-interaction
# These are approximate Bedrock on-demand pricing estimates
MODEL_PRICING = {
    "sonnet": {"input_per_1k": 0.003, "output_per_1k": 0.015, "avg_input_tokens": 800, "avg_output_tokens": 400},
    "nova_2_pro": {"input_per_1k": 0.0008, "output_per_1k": 0.0032, "avg_input_tokens": 800, "avg_output_tokens": 400},
    "glm_5": {"input_per_1k": 0.00057, "output_per_1k": 0.0021, "avg_input_tokens": 800, "avg_output_tokens": 400},
}


# ---------------------------------------------------------------------------
# Telemetry setup
# ---------------------------------------------------------------------------

def setup_telemetry() -> StrandsEvalsTelemetry:
    """Initialize StrandsEvalsTelemetry with in-memory exporter.

    Must be called BEFORE any agent invocations so the global tracer provider
    is configured to capture spans.

    Returns:
        Configured StrandsEvalsTelemetry instance with in-memory exporter.
    """
    telemetry = StrandsEvalsTelemetry()
    telemetry.setup_in_memory_exporter()
    return telemetry


# ---------------------------------------------------------------------------
# Trace capture helpers
# ---------------------------------------------------------------------------

def capture_trace(
    telemetry: StrandsEvalsTelemetry,
    mapper: StrandsInMemorySessionMapper,
    session_id: str,
) -> tuple[dict | None, object | None]:
    """Capture finished spans and map to a structured session.

    Args:
        telemetry: The telemetry instance with in-memory exporter.
        mapper: The session mapper instance.
        session_id: Unique session identifier for this invocation.

    Returns:
        Tuple of (serialized session dict, raw Session object) or (None, None).
    """
    try:
        exporter = telemetry.in_memory_exporter
        spans = list(exporter.get_finished_spans())

        if not spans:
            print(f"    [WARN] No spans captured for session {session_id}")
            return None, None

        session = mapper.map_to_session(spans, session_id=session_id)

        # Serialize session to dict using Pydantic model_dump
        session_dict = session.model_dump(mode="json")

        # Clear spans for next invocation
        exporter.clear()

        return session_dict, session

    except Exception as e:
        print(f"    [WARN] Trace capture failed for {session_id}: {e}")
        # Clear exporter even on failure to avoid stale spans
        try:
            telemetry.in_memory_exporter.clear()
        except Exception:
            pass
        return None, None


def build_trace_output(
    session_dict: dict | None,
    model_key: str,
    prompt: str,
    response: str,
    duration_ms: float,
) -> dict:
    """Build the trace output JSON structure.

    Args:
        session_dict: Serialized session from mapper, or None.
        model_key: The model key (sonnet, haiku, nova_pro).
        prompt: The input prompt.
        response: The agent response text.
        duration_ms: Total invocation duration in milliseconds.

    Returns:
        Dict with complete trace data for saving to JSON file.
    """
    # Extract tool calls from session spans
    tool_calls = []
    spans_data = []

    if session_dict and session_dict.get("traces"):
        for trace in session_dict["traces"]:
            for span in trace.get("spans", []):
                span_type = span.get("span_type", "")
                if span_type == "execute_tool":
                    tool_call_info = span.get("tool_call", {})
                    tool_result_info = span.get("tool_result", {})
                    tool_calls.append({
                        "tool": tool_call_info.get("name", "unknown"),
                        "params": tool_call_info.get("arguments", {}),
                        "result": tool_result_info.get("content", ""),
                    })
                spans_data.append(span)

    return {
        "session_id": f"{model_key}-prompt-{EVALUATION_PROMPTS.index(prompt) + 1}",
        "model": SUPPORTED_MODELS.get(model_key, "unknown"),
        "prompt": prompt,
        "response": response,
        "spans": spans_data,
        "total_duration_ms": round(duration_ms, 1),
        "tool_calls": tool_calls,
    }


def save_trace_file(trace_data: dict, model_key: str, prompt_idx: int):
    """Save trace data to a JSON file.

    Args:
        trace_data: The trace output dict.
        model_key: The model key.
        prompt_idx: 1-based prompt index.
    """
    traces_dir = PROJECT_ROOT / "results" / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{model_key}_prompt_{prompt_idx}.json"
    filepath = traces_dir / filename

    with open(filepath, "w") as f:
        json.dump(trace_data, f, indent=2, default=str)

    print(f"    Trace saved: {filepath.relative_to(PROJECT_ROOT)}")


# ---------------------------------------------------------------------------
# Agent invocation with trace capture
# ---------------------------------------------------------------------------

def run_agent_with_traces(
    model_key: str,
    prompts: list[str],
    telemetry: StrandsEvalsTelemetry,
    mapper: StrandsInMemorySessionMapper,
) -> list[dict]:
    """Run the agent against all prompts with trace capture.

    Args:
        model_key: The model key (sonnet, haiku, nova_pro).
        prompts: List of prompts to run.
        telemetry: Configured telemetry instance.
        mapper: Session mapper instance.

    Returns:
        List of dicts with prompt, response, duration_ms, and trace_data for each invocation.
    """
    agent = create_agent(model_key)
    results = []

    for i, prompt in enumerate(prompts, 1):
        print(f"  Prompt {i}/{len(prompts)}: {prompt[:60]}...")

        # Clear any leftover spans before invocation
        try:
            telemetry.in_memory_exporter.clear()
        except Exception:
            pass

        start_time = time.time()

        try:
            response = agent(prompt)
            duration_ms = (time.time() - start_time) * 1000
            response_text = str(response)

            # Capture trace
            session_id = f"{model_key}-prompt-{i}"
            session_dict, session_obj = capture_trace(telemetry, mapper, session_id)

            # Build trace output
            trace_data = build_trace_output(
                session_dict, model_key, prompt, response_text, duration_ms
            )

            # Save trace file
            save_trace_file(trace_data, model_key, i)

            results.append({
                "prompt": prompt,
                "response": response_text,
                "duration_ms": round(duration_ms, 1),
                "success": True,
                "trace_data": trace_data,
                "session_obj": session_obj,
            })
            print(f"    ✓ Completed in {duration_ms:.0f}ms")

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000

            # Still try to capture any partial trace
            session_id = f"{model_key}-prompt-{i}"
            session_dict, session_obj = capture_trace(telemetry, mapper, session_id)
            trace_data = build_trace_output(
                session_dict, model_key, prompt, f"ERROR: {str(e)}", duration_ms
            )
            save_trace_file(trace_data, model_key, i)

            results.append({
                "prompt": prompt,
                "response": f"ERROR: {str(e)}",
                "duration_ms": round(duration_ms, 1),
                "success": False,
                "trace_data": trace_data,
                "session_obj": session_obj,
            })
            print(f"    ✗ Failed: {e}")

    return results


# ---------------------------------------------------------------------------
# Tool use graph generation
# ---------------------------------------------------------------------------

def extract_tool_sequences(run_results: dict[str, list]) -> dict[str, dict[int, list]]:
    """Extract tool call sequences from trace data per model per prompt.

    Args:
        run_results: Dict of model_key -> list of invocation results with trace_data.

    Returns:
        Dict of model_key -> {prompt_idx: [tool_call_dicts]}.
    """
    sequences = {}
    for model_key, results in run_results.items():
        sequences[model_key] = {}
        for i, result in enumerate(results, 1):
            trace_data = result.get("trace_data", {})
            tool_calls = trace_data.get("tool_calls", [])
            sequences[model_key][i] = tool_calls
    return sequences


def generate_tool_use_graph(run_results: dict[str, list]):
    """Generate results/traces/tool_use_graph.md with tool call sequences.

    Args:
        run_results: Dict of model_key -> list of invocation results.
    """
    traces_dir = PROJECT_ROOT / "results" / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    output_path = traces_dir / "tool_use_graph.md"

    models = list(run_results.keys())
    sequences = extract_tool_sequences(run_results)

    lines = [
        "# Tool Use Graph",
        "",
        "Visualization of tool call sequences per model per prompt.",
        "",
    ]

    for i, prompt in enumerate(EVALUATION_PROMPTS, 1):
        lines.append(f"## Prompt {i}: \"{prompt}\"")
        lines.append("")
        lines.append("| Model | Tool Sequence | Parameters |")
        lines.append("| --- | --- | --- |")

        for model_key in models:
            tool_calls = sequences.get(model_key, {}).get(i, [])

            if tool_calls:
                # Build sequence: model → tool1 → tool2 → ... → model
                tool_names = [tc["tool"] for tc in tool_calls]
                sequence_str = "model → " + " → ".join(tool_names) + " → model"

                # Build parameters string
                params_parts = []
                for tc in tool_calls:
                    params = tc.get("params", {})
                    if params:
                        param_str = ", ".join(f"{k}={v}" for k, v in params.items())
                        params_parts.append(f"{tc['tool']}({param_str})")
                    else:
                        params_parts.append(f"{tc['tool']}()")
                params_str = "; ".join(params_parts)
            else:
                sequence_str = "model → model (no tools)"
                params_str = "—"

            lines.append(f"| {model_key} | {sequence_str} | {params_str} |")

        lines.append("")

    output_path.write_text("\n".join(lines))
    print(f"Tool use graph saved to: {output_path.relative_to(PROJECT_ROOT)}")


def generate_detailed_traces_section(run_results: dict[str, list]) -> str:
    """Generate detailed agentic traces section showing reasoning loops per model per prompt.

    Parses inference spans to extract:
    - Reasoning steps (text content before/between tool calls)
    - Tool decisions (which tools, parameters, parallel vs sequential)
    - Tool results (data returned)
    - Final synthesis (last assistant message)
    - Timing (duration of each inference step and tool execution)

    Args:
        run_results: Dict of model_key -> list of invocation results with trace_data.

    Returns:
        Markdown string with detailed agentic traces section.
    """
    models = list(run_results.keys())

    lines = [
        "",
        "## Detailed Agentic Traces",
        "",
        "Full reasoning loops showing model behavior, tool use decisions, and synthesis patterns.",
        "",
    ]

    for prompt_idx, prompt in enumerate(EVALUATION_PROMPTS, 1):
        lines.append(f"### Prompt {prompt_idx}: \"{prompt}\"")
        lines.append("")

        for model_key in models:
            results = run_results.get(model_key, [])
            if prompt_idx - 1 >= len(results):
                continue

            result = results[prompt_idx - 1]
            trace_data = result.get("trace_data", {})
            spans = trace_data.get("spans", [])

            lines.append(f"#### {model_key}")
            lines.append("**Reasoning Loop:**")

            # Extract the reasoning loop from the LAST inference span
            # The last inference span has the complete message chain for this prompt
            reasoning_steps = _extract_reasoning_loop(spans, prompt)

            if reasoning_steps:
                for step_num, step in enumerate(reasoning_steps, 1):
                    lines.append(f"{step_num}. {step}")
            else:
                lines.append("_(No trace data available)_")

            lines.append("")

            # Summary stats
            inference_count, tool_count, parallel_info, duration = _extract_trace_stats(
                spans, trace_data
            )
            parallel_str = ""
            if parallel_info:
                parallel_str = f" ({parallel_info})"

            lines.append(
                f"**Inference spans:** {inference_count} | "
                f"**Tool calls:** {tool_count}{parallel_str} | "
                f"**Duration:** {duration}"
            )
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _extract_reasoning_loop(spans: list[dict], prompt: str) -> list[str]:
    """Extract the reasoning loop steps from trace spans for a specific prompt.

    Finds the relevant inference spans and parses the message chain to identify:
    - Model reasoning text
    - Tool use decisions (with parameters)
    - Tool results
    - Final synthesis

    Args:
        spans: List of span dicts from trace_data.
        prompt: The prompt text to find the relevant conversation for.

    Returns:
        List of formatted step strings with emoji prefixes.
    """
    # Strategy: Find inference spans that contain this prompt as the LAST user message
    # The trace structure has multiple inference spans per multi-tool interaction.
    # We need to reconstruct the agentic loop for THIS specific prompt.

    # First, find all inference spans
    inference_spans = [s for s in spans if s.get("span_type") == "inference"]
    execute_tool_spans = [s for s in spans if s.get("span_type") == "execute_tool"]

    if not inference_spans:
        return []

    # For multi-turn traces (like prompt 5 which has history), we need to find
    # the messages relevant to the CURRENT prompt. The last inference span
    # contains the full conversation including the final synthesis.
    # We look at the last inference span's messages to find where our prompt starts.

    last_inference = inference_spans[-1]
    messages = last_inference.get("messages", [])

    if not messages:
        return []

    # Find the index where our prompt appears as a user message
    prompt_start_idx = None
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            content = msg.get("content", [])
            for item in content:
                if item.get("content_type") == "text" and item.get("text") == prompt:
                    prompt_start_idx = i
                    # Don't break - we want the LAST occurrence (most recent)

    if prompt_start_idx is None:
        # Fallback: try to find it in the first inference span
        if len(inference_spans) > 0:
            first_inference = inference_spans[0]
            messages = first_inference.get("messages", [])
            for i, msg in enumerate(messages):
                if msg.get("role") == "user":
                    content = msg.get("content", [])
                    for item in content:
                        if item.get("content_type") == "text" and item.get("text") == prompt:
                            prompt_start_idx = i
        if prompt_start_idx is None:
            return []

    # Now extract the reasoning loop from prompt_start_idx onwards
    steps = []
    relevant_messages = messages[prompt_start_idx:]

    # Track which tool results we've seen (to pair with tool calls)
    pending_tool_calls = []

    for msg in relevant_messages:
        role = msg.get("role")
        content = msg.get("content", [])

        if role == "user":
            # Check if this is a tool_result or the initial prompt
            for item in content:
                if item.get("content_type") == "tool_result":
                    # This is a tool result coming back
                    result_text = item.get("content", "")
                    # Truncate long results for readability
                    if len(result_text) > 200:
                        result_text = result_text[:200] + "..."
                    # Format as JSON-like display
                    try:
                        result_obj = json.loads(result_text.rstrip("..."))
                        result_display = json.dumps(result_obj, indent=None)
                        if len(result_display) > 200:
                            result_display = result_display[:200] + "..."
                    except (json.JSONDecodeError, ValueError):
                        result_display = result_text
                    steps.append(f"📥 Tool result: `{result_display}`")
                # Skip the initial user prompt text (we already show it in the header)

        elif role == "assistant":
            text_parts = []
            tool_use_parts = []

            for item in content:
                if item.get("content_type") == "text":
                    text = item.get("text", "").strip()
                    if text:
                        text_parts.append(text)
                elif item.get("content_type") == "tool_use":
                    tool_name = item.get("name", "unknown")
                    args = item.get("arguments", {})
                    args_str = ", ".join(f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}" for k, v in args.items())
                    tool_use_parts.append(f"`{tool_name}({args_str})`")

            # Determine if this is reasoning, tool call, or final synthesis
            if text_parts and tool_use_parts:
                # Model reasoned AND made tool calls in same message
                # Check if this is the last assistant message (final synthesis)
                # It's intermediate reasoning + tool call
                reasoning_text = " ".join(text_parts)
                if len(reasoning_text) > 150:
                    reasoning_text = reasoning_text[:150] + "..."
                steps.append(f'🧠 Model reasoning: "{reasoning_text}"')

                if len(tool_use_parts) > 1:
                    # Parallel tool calls
                    tools_str = " + ".join(tool_use_parts)
                    steps.append(f"🔧 Tool calls (parallel): {tools_str}")
                else:
                    steps.append(f"🔧 Tool call: {tool_use_parts[0]}")

            elif text_parts and not tool_use_parts:
                # Pure text response - this is either intermediate reasoning or final synthesis
                full_text = " ".join(text_parts)
                # If it's the last message, it's the final synthesis
                if msg == relevant_messages[-1] or (
                    relevant_messages.index(msg) == len(relevant_messages) - 1
                ):
                    # Final synthesis - show truncated
                    if len(full_text) > 200:
                        synthesis_preview = full_text[:200] + "..."
                    else:
                        synthesis_preview = full_text
                    steps.append(f'🧠 Final synthesis: "{synthesis_preview}"')
                else:
                    if len(full_text) > 150:
                        full_text = full_text[:150] + "..."
                    steps.append(f'🧠 Model reasoning: "{full_text}"')

            elif tool_use_parts and not text_parts:
                # Tool calls without preceding text
                if len(tool_use_parts) > 1:
                    tools_str = " + ".join(tool_use_parts)
                    steps.append(f"🔧 Tool calls (parallel): {tools_str}")
                else:
                    steps.append(f"🔧 Tool call: {tool_use_parts[0]}")

    # If the last step isn't a final synthesis but we have a response, add it
    if steps and not any("Final synthesis" in s for s in steps):
        # Check if there's a response in the trace_data
        pass  # The last assistant message should already be captured

    return steps


def _extract_trace_stats(
    spans: list[dict], trace_data: dict
) -> tuple[int, int, str, str]:
    """Extract summary statistics from trace spans.

    Counts only tool calls relevant to the current prompt (from execute_tool spans),
    and detects parallel vs sequential patterns from the inference span messages.

    Args:
        spans: List of span dicts.
        trace_data: Full trace data dict.

    Returns:
        Tuple of (inference_count, tool_count, parallel_info, duration_str).
    """
    inference_spans = [s for s in spans if s.get("span_type") == "inference"]
    execute_tool_spans = [s for s in spans if s.get("span_type") == "execute_tool"]

    inference_count = len(inference_spans)
    tool_count = len(execute_tool_spans)

    # Detect parallel vs sequential tool calls for the CURRENT prompt only.
    # We use the execute_tool spans count as the actual tool call count,
    # and detect parallelism from the message structure in inference spans.
    parallel_calls = 0
    sequential_calls = 0

    # Look at the prompt from trace_data to find relevant messages
    prompt = trace_data.get("prompt", "")

    if inference_spans:
        # Use the last inference span which has the full conversation
        last_inf = inference_spans[-1]
        messages = last_inf.get("messages", [])

        # Find where our prompt starts and only count tool calls after that
        prompt_found = False
        for msg in messages:
            if msg.get("role") == "user" and not prompt_found:
                content = msg.get("content", [])
                for item in content:
                    if item.get("content_type") == "text" and item.get("text") == prompt:
                        prompt_found = True
                        break
                continue

            if prompt_found and msg.get("role") == "assistant":
                tool_uses = [
                    item for item in msg.get("content", [])
                    if item.get("content_type") == "tool_use"
                ]
                if len(tool_uses) > 1:
                    parallel_calls += len(tool_uses)
                elif len(tool_uses) == 1:
                    sequential_calls += 1

    parallel_info = ""
    if parallel_calls > 0 and sequential_calls > 0:
        parallel_info = f"{parallel_calls} parallel + {sequential_calls} sequential"
    elif parallel_calls > 0:
        parallel_info = f"{parallel_calls} parallel"

    # Duration
    total_ms = trace_data.get("total_duration_ms", 0)
    if total_ms > 0:
        duration_str = f"{total_ms / 1000:.1f}s"
    else:
        duration_str = "N/A"

    return inference_count, tool_count, parallel_info, duration_str


def generate_tool_use_summary(run_results: dict[str, list]) -> str:
    """Generate tool use summary section for comparison.md.

    Args:
        run_results: Dict of model_key -> list of invocation results.

    Returns:
        Markdown string with tool use summary.
    """
    models = list(run_results.keys())
    sequences = extract_tool_sequences(run_results)

    lines = [
        "",
        "## Tool Use Summary",
        "",
        "Tool orchestration patterns across models and prompts.",
        "",
    ]

    # Summary table: total tool calls per model
    lines.append("### Tool Call Counts")
    lines.append("")
    lines.append("| Model | Total Tool Calls | Unique Tools Used | Avg Tools/Prompt |")
    lines.append("| --- | --- | --- | --- |")

    for model_key in models:
        all_calls = []
        for prompt_idx, calls in sequences.get(model_key, {}).items():
            all_calls.extend(calls)

        total_calls = len(all_calls)
        unique_tools = len(set(tc["tool"] for tc in all_calls)) if all_calls else 0
        num_prompts = len(sequences.get(model_key, {}))
        avg_per_prompt = total_calls / num_prompts if num_prompts > 0 else 0

        lines.append(
            f"| {model_key} | {total_calls} | {unique_tools} | {avg_per_prompt:.1f} |"
        )

    lines.append("")

    # Per-prompt tool sequences
    lines.append("### Per-Prompt Tool Sequences")
    lines.append("")

    for i, prompt in enumerate(EVALUATION_PROMPTS, 1):
        lines.append(f"**Prompt {i}:** {prompt[:80]}{'...' if len(prompt) > 80 else ''}")
        lines.append("")

        for model_key in models:
            tool_calls = sequences.get(model_key, {}).get(i, [])
            if tool_calls:
                tool_names = [tc["tool"] for tc in tool_calls]
                lines.append(f"- **{model_key}**: {' → '.join(tool_names)}")
            else:
                lines.append(f"- **{model_key}**: (no tools called)")

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SDK-based evaluation (ALL 8 evaluators)
# ---------------------------------------------------------------------------

def run_sdk_evaluations(run_results: dict[str, list]) -> dict[str, dict[str, float | None]]:
    """Run ALL SDK-based evaluators on captured sessions.

    For each model × prompt × evaluator, passes the captured Session object to
    the evaluator and collects scores.

    Args:
        run_results: Dict of model_key -> list of invocation results with session_obj.

    Returns:
        Dict of model_key -> {evaluator_name: average_score}.
    """
    print("\n[SDK Eval] Running all 8 SDK evaluators on captured sessions...")

    sdk_scores = {}

    for model_key, results in run_results.items():
        print(f"  Evaluating {model_key}...")
        model_scores = {}

        for eval_name in SDK_EVALUATOR_NAMES:
            evaluator = SDK_EVALUATORS[eval_name]
            prompt_scores = []

            for i, result in enumerate(results, 1):
                session_obj = result.get("session_obj")
                prompt = result.get("prompt", "")

                if session_obj is None:
                    prompt_scores.append(None)
                    continue

                try:
                    eval_data = EvaluationData(
                        input=prompt,
                        actual_trajectory=session_obj,
                    )
                    eval_results = evaluator.evaluate(eval_data)

                    if eval_results:
                        score = eval_results[0].score
                        prompt_scores.append(score)
                    else:
                        prompt_scores.append(None)

                except Exception as e:
                    print(f"    [{eval_name}] Prompt {i}: Failed - {e}")
                    prompt_scores.append(None)

            # Calculate average (excluding None values)
            valid_scores = [s for s in prompt_scores if s is not None]
            avg_score = sum(valid_scores) / len(valid_scores) if valid_scores else None

            model_scores[eval_name] = avg_score
            model_scores[f"{eval_name}_per_prompt"] = prompt_scores

            if avg_score is not None:
                print(f"    {eval_name}: {avg_score:.3f}")
            else:
                print(f"    {eval_name}: N/A")

        sdk_scores[model_key] = model_scores

    return sdk_scores


# ---------------------------------------------------------------------------
# Mermaid trace diagram generation
# ---------------------------------------------------------------------------

def _truncate(text: str, max_len: int = 50) -> str:
    """Truncate text to max_len characters for diagram readability."""
    if not text:
        return ""
    # Remove newlines for diagram compatibility
    text = text.replace("\n", " ").replace("\r", "")
    if len(text) > max_len:
        return text[:max_len - 3] + "..."
    return text


def _sanitize_mermaid(text: str) -> str:
    """Sanitize text for use in Mermaid diagrams (escape special chars)."""
    # Remove or escape characters that break Mermaid syntax
    text = text.replace('"', "'")
    text = text.replace("#", "")
    text = text.replace(";", ",")
    text = text.replace(":", " -")
    text = text.replace("{", "(")
    text = text.replace("}", ")")
    text = text.replace("<", "")
    text = text.replace(">", "")
    text = text.replace("&", "and")
    return text


def generate_mermaid_diagrams(run_results: dict[str, list]):
    """Generate Mermaid sequence diagrams for each prompt × model.

    Creates results/traces/trace_diagrams.md with visual trace diagrams.

    Args:
        run_results: Dict of model_key -> list of invocation results with trace_data.
    """
    traces_dir = PROJECT_ROOT / "results" / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    output_path = traces_dir / "trace_diagrams.md"

    models = list(run_results.keys())

    lines = [
        "# Trace Diagrams",
        "",
        "Visual Mermaid sequence diagrams showing agent reasoning flows per prompt per model.",
        "",
    ]

    for prompt_idx, prompt in enumerate(EVALUATION_PROMPTS, 1):
        lines.append(f"## Prompt {prompt_idx}: \"{_truncate(prompt, 80)}\"")
        lines.append("")

        for model_key in models:
            results = run_results.get(model_key, [])
            if prompt_idx - 1 >= len(results):
                continue

            result = results[prompt_idx - 1]
            trace_data = result.get("trace_data", {})
            duration_ms = trace_data.get("total_duration_ms", 0)

            lines.append(f"### {model_key}")
            lines.append("")
            lines.append("```mermaid")

            # Build the sequence diagram
            diagram_lines = _build_sequence_diagram(trace_data, prompt, duration_ms)
            lines.extend(diagram_lines)

            lines.append("```")
            lines.append("")

    output_path.write_text("\n".join(lines))
    print(f"Trace diagrams saved to: {output_path.relative_to(PROJECT_ROOT)}")


def _build_sequence_diagram(trace_data: dict, prompt: str, duration_ms: float) -> list[str]:
    """Build a Mermaid sequenceDiagram from trace data.

    Args:
        trace_data: The trace output dict with spans and tool_calls.
        prompt: The input prompt text.
        duration_ms: Total duration in milliseconds.

    Returns:
        List of Mermaid diagram lines.
    """
    lines = ["    sequenceDiagram"]
    lines.append("    participant User")
    lines.append("    participant Agent")

    spans = trace_data.get("spans", [])
    tool_calls = trace_data.get("tool_calls", [])

    # Declare tool participants
    tool_names_seen = set()
    for tc in tool_calls:
        tool_name = tc.get("tool", "unknown")
        if tool_name not in tool_names_seen:
            tool_names_seen.add(tool_name)
            lines.append(f"    participant {tool_name}")

    # User sends prompt
    prompt_display = _sanitize_mermaid(_truncate(prompt, 50))
    lines.append(f"    User->>Agent: {prompt_display}")

    # Parse the reasoning loop from spans to build the diagram
    reasoning_steps = _extract_diagram_steps(spans, prompt)

    if reasoning_steps:
        # Check if there are parallel tool calls
        parallel_groups = []
        current_group = []
        i = 0
        while i < len(reasoning_steps):
            step = reasoning_steps[i]
            if step["type"] == "parallel_tools":
                # Group of parallel tool calls
                parallel_groups.append(("parallel", step["tools"]))
            elif step["type"] == "tool_call":
                parallel_groups.append(("sequential", [step]))
            elif step["type"] == "reasoning":
                parallel_groups.append(("reasoning", step))
            elif step["type"] == "synthesis":
                parallel_groups.append(("synthesis", step))
            i += 1

        for group_type, group_data in parallel_groups:
            if group_type == "reasoning":
                text = _sanitize_mermaid(_truncate(group_data["text"], 50))
                lines.append(f"    Note over Agent: {text}")
            elif group_type == "synthesis":
                text = _sanitize_mermaid(_truncate(group_data["text"], 50))
                lines.append(f"    Agent->>User: {text}")
            elif group_type == "sequential":
                tc = group_data[0]
                tool_name = tc.get("tool", "unknown")
                params = tc.get("params_str", "")
                result_str = tc.get("result_str", "done")
                params_display = _sanitize_mermaid(_truncate(params, 40))
                result_display = _sanitize_mermaid(_truncate(result_str, 40))
                lines.append(f"    Agent->>{tool_name}: {params_display}")
                lines.append(f"    {tool_name}-->>Agent: {result_display}")
            elif group_type == "parallel":
                tools = group_data
                if len(tools) > 1:
                    lines.append("    par Parallel tool calls")
                    for j, tc in enumerate(tools):
                        tool_name = tc.get("tool", "unknown")
                        params = tc.get("params_str", "")
                        result_str = tc.get("result_str", "done")
                        params_display = _sanitize_mermaid(_truncate(params, 40))
                        result_display = _sanitize_mermaid(_truncate(result_str, 40))
                        if j == 0:
                            lines.append(f"        Agent->>{tool_name}: {params_display}")
                            lines.append(f"        {tool_name}-->>Agent: {result_display}")
                        else:
                            lines.append("    and")
                            lines.append(f"        Agent->>{tool_name}: {params_display}")
                            lines.append(f"        {tool_name}-->>Agent: {result_display}")
                    lines.append("    end")
                else:
                    tc = tools[0]
                    tool_name = tc.get("tool", "unknown")
                    params = tc.get("params_str", "")
                    result_str = tc.get("result_str", "done")
                    params_display = _sanitize_mermaid(_truncate(params, 40))
                    result_display = _sanitize_mermaid(_truncate(result_str, 40))
                    lines.append(f"    Agent->>{tool_name}: {params_display}")
                    lines.append(f"    {tool_name}-->>Agent: {result_display}")
    else:
        # Fallback: use tool_calls list directly
        if tool_calls:
            for tc in tool_calls:
                tool_name = tc.get("tool", "unknown")
                params = tc.get("params", {})
                params_str = ", ".join(f"{k}={v}" for k, v in params.items()) if params else ""
                result = tc.get("result", "")
                params_display = _sanitize_mermaid(_truncate(params_str, 40))
                result_display = _sanitize_mermaid(_truncate(str(result), 40))
                lines.append(f"    Agent->>{tool_name}: {params_display}")
                lines.append(f"    {tool_name}-->>Agent: {result_display}")

        # Final response
        response = trace_data.get("response", "")
        if response:
            resp_display = _sanitize_mermaid(_truncate(response, 50))
            lines.append(f"    Agent->>User: {resp_display}")

    # Add timing note
    if duration_ms > 0:
        duration_s = duration_ms / 1000
        lines.append(f"    Note right of Agent: {duration_s:.1f}s total")

    return lines


def _extract_diagram_steps(spans: list[dict], prompt: str) -> list[dict]:
    """Extract structured steps from trace spans for Mermaid diagram generation.

    Parses inference spans to identify reasoning, tool calls (parallel/sequential),
    and final synthesis.

    Args:
        spans: List of span dicts from trace_data.
        prompt: The prompt text.

    Returns:
        List of step dicts with type and relevant data.
    """
    inference_spans = [s for s in spans if s.get("span_type") == "inference"]
    execute_tool_spans = [s for s in spans if s.get("span_type") == "execute_tool"]

    if not inference_spans:
        return []

    # Use the last inference span which has the full conversation
    last_inference = inference_spans[-1]
    messages = last_inference.get("messages", [])

    if not messages:
        return []

    # Find where our prompt starts
    prompt_start_idx = None
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            content = msg.get("content", [])
            for item in content:
                if item.get("content_type") == "text" and item.get("text") == prompt:
                    prompt_start_idx = i

    if prompt_start_idx is None:
        return []

    steps = []
    relevant_messages = messages[prompt_start_idx:]

    # Build a map of tool results from execute_tool spans
    tool_results_map = {}
    for span in execute_tool_spans:
        tool_call_info = span.get("tool_call", {})
        tool_result_info = span.get("tool_result", {})
        tool_name = tool_call_info.get("name", "unknown")
        tool_results_map[tool_name] = tool_result_info.get("content", "")

    for msg_idx, msg in enumerate(relevant_messages):
        role = msg.get("role")
        content = msg.get("content", [])

        if role == "assistant":
            text_parts = []
            tool_use_parts = []

            for item in content:
                if item.get("content_type") == "text":
                    text = item.get("text", "").strip()
                    if text:
                        text_parts.append(text)
                elif item.get("content_type") == "tool_use":
                    tool_name = item.get("name", "unknown")
                    args = item.get("arguments", {})
                    args_str = ", ".join(
                        f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}"
                        for k, v in args.items()
                    )
                    result_str = tool_results_map.get(tool_name, "done")
                    if isinstance(result_str, str) and len(result_str) > 50:
                        result_str = result_str[:47] + "..."
                    tool_use_parts.append({
                        "tool": tool_name,
                        "params_str": args_str,
                        "result_str": str(result_str) if result_str else "done",
                    })

            # Add reasoning step
            if text_parts:
                full_text = " ".join(text_parts)
                is_last = msg_idx == len(relevant_messages) - 1
                if is_last and not tool_use_parts:
                    steps.append({"type": "synthesis", "text": full_text})
                else:
                    steps.append({"type": "reasoning", "text": full_text})

            # Add tool call steps
            if tool_use_parts:
                if len(tool_use_parts) > 1:
                    steps.append({"type": "parallel_tools", "tools": tool_use_parts})
                else:
                    steps.append({"type": "tool_call", **tool_use_parts[0]})

    # If no synthesis step was added but we have a response, add one
    if steps and not any(s["type"] == "synthesis" for s in steps):
        # The last text step might actually be the synthesis
        pass

    return steps


# ---------------------------------------------------------------------------
# Evaluation via agentcore CLI (custom evaluators only)
# ---------------------------------------------------------------------------

def run_custom_evaluations(model_key: str) -> dict[str, float]:
    """Run custom evaluators against traces for a given model using agentcore CLI.

    Only runs the 2 custom evaluators (multiplier_domain_accuracy, multiplier_deterministic).
    Standard evaluators are handled by the SDK.

    Args:
        model_key: The model key whose traces to evaluate.

    Returns:
        Dict mapping evaluator name to average score (0.0-1.0).
    """
    service_name = f"multiplier-local-{model_key}"
    scores = {}

    for evaluator in CUSTOM_EVALUATORS:
        print(f"  Running custom evaluator: {evaluator}...")
        try:
            cmd = [
                "agentcore", "evals", "run",
                "--evaluator-name", evaluator,
                "--service-name", service_name,
                "--output-format", "json",
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode == 0:
                try:
                    eval_output = json.loads(result.stdout)
                    score = _extract_score(eval_output)
                    scores[evaluator] = score
                    print(f"    Score: {score:.2f}")
                except json.JSONDecodeError:
                    score = _parse_score_from_text(result.stdout)
                    scores[evaluator] = score
                    print(f"    Score (parsed): {score:.2f}")
            else:
                print(f"    [WARN] Evaluator returned non-zero: {result.stderr[:100]}")
                scores[evaluator] = None

        except FileNotFoundError:
            print(f"    [WARN] agentcore CLI not found. Skipping custom evaluator.")
            scores[evaluator] = None
        except subprocess.TimeoutExpired:
            print(f"    [WARN] Evaluator timed out")
            scores[evaluator] = None

    return scores


def _extract_score(eval_output: dict) -> float:
    """Extract average score from agentcore eval JSON output."""
    # Handle various output formats from agentcore eval
    if isinstance(eval_output, dict):
        if "score" in eval_output:
            return float(eval_output["score"])
        if "average_score" in eval_output:
            return float(eval_output["average_score"])
        if "results" in eval_output:
            results = eval_output["results"]
            if isinstance(results, list) and results:
                total = sum(r.get("score", 0) for r in results)
                return total / len(results)
    if isinstance(eval_output, list) and eval_output:
        total = sum(r.get("score", 0) for r in eval_output)
        return total / len(eval_output)
    return 0.0


def _parse_score_from_text(text: str) -> float:
    """Attempt to parse a score from non-JSON text output."""
    import re
    # Look for patterns like "score: 0.85" or "Score: 0.85"
    match = re.search(r"[Ss]core[:\s]+([0-9.]+)", text)
    if match:
        return float(match.group(1))
    return 0.0


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

def estimate_cost_per_interaction(model_key: str) -> float:
    """Estimate cost per interaction based on model pricing.

    Args:
        model_key: The model key.

    Returns:
        Estimated cost in USD per interaction.
    """
    pricing = MODEL_PRICING.get(model_key)
    if not pricing:
        return 0.0

    input_cost = (pricing["avg_input_tokens"] / 1000) * pricing["input_per_1k"]
    output_cost = (pricing["avg_output_tokens"] / 1000) * pricing["output_per_1k"]
    return round(input_cost + output_cost, 6)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_comparison_table(
    all_scores: dict[str, dict],
    costs: dict[str, float],
    sdk_scores: dict[str, dict] | None = None,
):
    """Print a formatted comparison table to stdout.

    Args:
        all_scores: Dict of model_key -> {evaluator_name: score} (custom evaluators).
        costs: Dict of model_key -> cost_per_interaction.
        sdk_scores: Dict of model_key -> {eval_name: score} for all 8 SDK evaluators.
    """
    models = list(all_scores.keys())

    # Header
    col_width = 14
    header = f"{'Evaluator':<28}" + "".join(f"{m:>{col_width}}" for m in models)
    separator = "-" * len(header)

    print("\n" + separator)
    print("MODEL COMPARISON RESULTS")
    print(separator)
    print(header)
    print(separator)

    # SDK evaluator rows (all 8)
    if sdk_scores:
        for eval_name in SDK_EVALUATOR_NAMES:
            row = f"{eval_name:<28}"
            for model in models:
                score = sdk_scores.get(model, {}).get(eval_name)
                if score is not None:
                    row += f"{score:>{col_width}.3f}"
                else:
                    row += f"{'N/A':>{col_width}}"
            print(row)

    print(separator)

    # Custom evaluator rows (via CLI)
    for evaluator in CUSTOM_EVALUATORS:
        row = f"{evaluator:<28}"
        for model in models:
            score = all_scores[model].get(evaluator)
            if score is not None:
                row += f"{score:>{col_width}.3f}"
            else:
                row += f"{'N/A':>{col_width}}"
        print(row)

    print(separator)

    # Cost row
    cost_row = f"{'cost/interaction (USD)':<28}"
    for model in models:
        cost = costs.get(model, 0.0)
        cost_row += f"${cost:>{col_width - 1}.5f}"
    print(cost_row)
    print(separator + "\n")


def save_comparison_markdown(
    all_scores: dict[str, dict],
    costs: dict[str, float],
    run_results: dict[str, list],
    sdk_scores: dict[str, dict] | None = None,
):
    """Save comparison results as results/comparison.md.

    Args:
        all_scores: Dict of model_key -> {evaluator_name: score} (custom evaluators).
        costs: Dict of model_key -> cost_per_interaction.
        run_results: Dict of model_key -> list of invocation results.
        sdk_scores: Dict of model_key -> {eval_name: score} for all 8 SDK evaluators.
    """
    results_dir = PROJECT_ROOT / "results"
    results_dir.mkdir(exist_ok=True)
    output_path = results_dir / "comparison.md"

    models = list(all_scores.keys())
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    total_evaluators = len(SDK_EVALUATOR_NAMES) + len(CUSTOM_EVALUATORS)

    lines = [
        "# Model Comparison Results",
        "",
        f"Generated: {timestamp}",
        "",
        f"Models evaluated: {', '.join(models)}",
        f"Prompts per model: {len(EVALUATION_PROMPTS)}",
        f"Evaluators: {total_evaluators} ({len(SDK_EVALUATOR_NAMES)} SDK + {len(CUSTOM_EVALUATORS)} custom)",
        "",
        "## Evaluation Scores (SDK Evaluators)",
        "",
        "| Evaluator | " + " | ".join(models) + " |",
        "| --- | " + " | ".join(["---"] * len(models)) + " |",
    ]

    # SDK evaluator rows (all 8)
    if sdk_scores:
        for eval_name in SDK_EVALUATOR_NAMES:
            row_cells = []
            for model in models:
                score = sdk_scores.get(model, {}).get(eval_name)
                if score is not None:
                    row_cells.append(f"{score:.3f}")
                else:
                    row_cells.append("N/A")
            lines.append(f"| {eval_name} | " + " | ".join(row_cells) + " |")

    lines.extend([
        "",
        "## Custom Evaluators (agentcore CLI)",
        "",
        "| Evaluator | " + " | ".join(models) + " |",
        "| --- | " + " | ".join(["---"] * len(models)) + " |",
    ])

    # Custom evaluator rows
    for evaluator in CUSTOM_EVALUATORS:
        row_cells = []
        for model in models:
            score = all_scores[model].get(evaluator)
            if score is not None:
                row_cells.append(f"{score:.3f}")
            else:
                row_cells.append("N/A")
        lines.append(f"| {evaluator} | " + " | ".join(row_cells) + " |")

    lines.extend([
        "",
        "## Cost per Interaction",
        "",
        "| Model | Cost (USD) |",
        "| --- | --- |",
    ])

    for model in models:
        cost = costs.get(model, 0.0)
        lines.append(f"| {model} | ${cost:.5f} |")

    lines.extend([
        "",
        "## Prompts Used",
        "",
    ])

    for i, prompt in enumerate(EVALUATION_PROMPTS, 1):
        lines.append(f"{i}. {prompt}")

    lines.extend([
        "",
        "## Run Summary",
        "",
    ])

    for model in models:
        results = run_results.get(model, [])
        success_count = sum(1 for r in results if r.get("success"))
        avg_duration = (
            sum(r["duration_ms"] for r in results) / len(results)
            if results
            else 0
        )
        lines.append(f"### {model} ({SUPPORTED_MODELS.get(model, 'unknown')})")
        lines.append(f"- Successful invocations: {success_count}/{len(results)}")
        lines.append(f"- Average latency: {avg_duration:.0f}ms")
        lines.append("")

    # Append tool use summary
    tool_use_summary = generate_tool_use_summary(run_results)
    lines.append(tool_use_summary)

    # Append detailed agentic traces
    detailed_traces = generate_detailed_traces_section(run_results)
    lines.append(detailed_traces)

    output_path.write_text("\n".join(lines))
    print(f"Results saved to: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run model comparison: invoke agents, capture traces, evaluate, output comparison table."
    )
    parser.add_argument(
        "--models",
        type=str,
        default="sonnet,nova_2_pro,glm_5",
        help="Comma-separated model keys to compare (default: sonnet,nova_2_pro,glm_5)",
    )
    args = parser.parse_args()

    # Parse and validate model keys
    model_keys = [m.strip() for m in args.models.split(",")]
    for key in model_keys:
        if key not in SUPPORTED_MODELS:
            print(f"ERROR: Unknown model key '{key}'. Supported: {list(SUPPORTED_MODELS.keys())}")
            sys.exit(1)

    print("=" * 60)
    print("MULTIPLIER EDD POC — Model Comparison")
    print("=" * 60)
    print(f"Models: {model_keys}")
    print(f"Prompts: {len(EVALUATION_PROMPTS)}")
    print(f"Evaluators: {len(SDK_EVALUATOR_NAMES)} SDK + {len(CUSTOM_EVALUATORS)} custom = {len(ALL_EVALUATORS)} total")
    print("=" * 60)

    # -----------------------------------------------------------------------
    # Setup: Initialize telemetry with in-memory exporter BEFORE agent runs
    # -----------------------------------------------------------------------
    print("\n[Setup] Initializing telemetry with InMemorySpanExporter...")
    telemetry = setup_telemetry()
    mapper = StrandsInMemorySessionMapper()
    print("  ✓ Telemetry configured")

    # -----------------------------------------------------------------------
    # Phase 1: Run agents with trace capture
    # -----------------------------------------------------------------------
    run_results = {}
    for model_key in model_keys:
        print(f"\n[Phase 1] Running {model_key} ({SUPPORTED_MODELS[model_key]})...")
        results = run_agent_with_traces(model_key, EVALUATION_PROMPTS, telemetry, mapper)
        run_results[model_key] = results

    # -----------------------------------------------------------------------
    # Phase 2: Generate tool use graph
    # -----------------------------------------------------------------------
    print("\n[Phase 2] Generating tool use graph...")
    generate_tool_use_graph(run_results)

    # -----------------------------------------------------------------------
    # Phase 3: Run SDK-based evaluations (ALL 8 evaluators)
    # -----------------------------------------------------------------------
    sdk_scores = run_sdk_evaluations(run_results)

    # -----------------------------------------------------------------------
    # Phase 4: Run agentcore CLI evaluations (custom evaluators only)
    # -----------------------------------------------------------------------
    all_scores = {}
    for model_key in model_keys:
        print(f"\n[Phase 4] Evaluating {model_key} with custom evaluators via agentcore CLI...")
        scores = run_custom_evaluations(model_key)
        all_scores[model_key] = scores

    # -----------------------------------------------------------------------
    # Phase 5: Generate Mermaid trace diagrams
    # -----------------------------------------------------------------------
    print("\n[Phase 5] Generating Mermaid trace diagrams...")
    generate_mermaid_diagrams(run_results)

    # -----------------------------------------------------------------------
    # Phase 6: Calculate costs and output results
    # -----------------------------------------------------------------------
    costs = {model_key: estimate_cost_per_interaction(model_key) for model_key in model_keys}

    # Print comparison table to stdout
    print_comparison_table(all_scores, costs, sdk_scores)

    # Save results/comparison.md (includes tool use summary)
    save_comparison_markdown(all_scores, costs, run_results, sdk_scores)

    print("Done! Comparison complete.")
    print(f"  - Trace files: results/traces/")
    print(f"  - Tool use graph: results/traces/tool_use_graph.md")
    print(f"  - Trace diagrams: results/traces/trace_diagrams.md")
    print(f"  - Comparison: results/comparison.md")


if __name__ == "__main__":
    main()
