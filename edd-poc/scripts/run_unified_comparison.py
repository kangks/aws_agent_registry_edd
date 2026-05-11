"""
Unified Evaluation Comparison for All 6 Agents.

Replaces the dual-evaluation architecture with a single pipeline where both
managed and BYO agents are scored by the AgentCore Evaluate API using the
same evaluators, producing directly comparable results.

Four phases:
  1. Invocation — invoke 3 managed + 3 BYO agents (5 prompts each)
  2. Wait — configurable trace propagation delay (default 120s)
  3. Evaluation — score all agents via AgentCore Evaluate API
  4. Report — generate results/unified_comparison.md + registry updates

Usage:
    AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \\
        .venv/bin/python scripts/run_unified_comparison.py

    # Override wait time and evaluators:
    .venv/bin/python scripts/run_unified_comparison.py \\
        --wait-seconds 180 --evaluators multiplier_domain_accuracy,faithfulness

    # Skip invocation phase (reuse previous intermediate data):
    .venv/bin/python scripts/run_unified_comparison.py --skip-invocation
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from unified_models import (
    ADOT_ENV,
    AGENTCORE,
    BYO_AGENTS,
    BYO_RUNNER,
    MANAGED_AGENTS,
    OTEL_INSTRUMENT,
    PROMPTS,
    PYTHON,
    AgentEvalResults,
    AgentInvocationData,
    EvaluationScore,
    IntermediateState,
    InvocationResult,
    PromptEvaluation,
    ReportMetadata,
)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class UnifiedConfig:
    """Configuration for the unified comparison script."""

    aws_profile: str = "ml-sandbox"
    aws_region: str = "us-east-1"
    wait_seconds: int = 120
    evaluators: list[str] = field(default_factory=lambda: ["multiplier_domain_accuracy"])
    skip_invocation: bool = False
    intermediate_path: str = "results/unified_intermediate.json"
    output_path: str = "results/unified_comparison.md"
    max_workers: int = 6
    managed_timeout: int = 120
    byo_timeout: int = 300


def parse_evaluators(evaluators_str: str) -> list[str]:
    """Parse a comma-separated evaluator string into a list of trimmed, non-empty names.

    Args:
        evaluators_str: Comma-separated evaluator names (e.g., "multiplier_domain_accuracy,faithfulness")

    Returns:
        List of trimmed, non-empty evaluator name strings.
    """
    return [name.strip() for name in evaluators_str.split(",") if name.strip()]


def build_config() -> UnifiedConfig:
    """Build UnifiedConfig from CLI arguments and environment variables.

    CLI arguments:
        --wait-seconds N       Trace propagation delay in seconds (default: 120)
        --evaluators LIST      Comma-separated evaluator names (default: multiplier_domain_accuracy)
        --skip-invocation      Skip invocation phase, use intermediate data from previous run

    Environment variables:
        AWS_PROFILE            AWS profile name (default: ml-sandbox)
        AWS_REGION             AWS region (default: us-east-1)

    Returns:
        Fully populated UnifiedConfig instance.
    """
    parser = argparse.ArgumentParser(
        description="Unified evaluation comparison for all 6 agents.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=120,
        metavar="N",
        help="Trace propagation delay in seconds (default: 120)",
    )
    parser.add_argument(
        "--evaluators",
        type=str,
        default="multiplier_domain_accuracy",
        metavar="LIST",
        help="Comma-separated evaluator names (default: multiplier_domain_accuracy)",
    )
    parser.add_argument(
        "--skip-invocation",
        action="store_true",
        default=False,
        help="Skip invocation phase, use intermediate data from previous run",
    )

    args = parser.parse_args()

    return UnifiedConfig(
        aws_profile=os.environ.get("AWS_PROFILE", "ml-sandbox"),
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        wait_seconds=args.wait_seconds,
        evaluators=parse_evaluators(args.evaluators),
        skip_invocation=args.skip_invocation,
    )


# ---------------------------------------------------------------------------
# Phase progress printing
# ---------------------------------------------------------------------------


def print_phase_start(phase_name: str) -> None:
    """Print a phase start banner."""
    print(f"\n{'=' * 70}")
    print(f"  Phase: {phase_name} — STARTED")
    print(f"{'=' * 70}")


def print_phase_complete(phase_name: str) -> None:
    """Print a phase completion banner."""
    print(f"\n  Phase: {phase_name} — COMPLETE ✅")
    print(f"{'─' * 70}")


# ---------------------------------------------------------------------------
# Managed Agent Invocation
# ---------------------------------------------------------------------------

SESSION_ID_PATTERN = re.compile(r"Session:\s*(\S+)")


def parse_session_id(cli_output: str) -> str | None:
    """Extract session ID from agentcore CLI output.

    Searches for 'Session: <id>' pattern in the combined stdout+stderr output.
    Returns None if no session ID is found.

    Args:
        cli_output: Combined stdout+stderr from the agentcore invoke command.

    Returns:
        The session ID string, or None if not found.
    """
    match = SESSION_ID_PATTERN.search(cli_output)
    return match.group(1) if match else None


def invoke_managed(runtime_name: str, prompt: str, timeout: int = 120) -> InvocationResult:
    """Invoke a managed agent and capture session ID from CLI output.

    Runs `agentcore invoke --runtime <runtime_name> <prompt>` as a subprocess,
    captures stdout/stderr, extracts the session ID, and measures elapsed time.

    If the subprocess times out, the invocation is recorded as failed with an
    error message and the function returns normally (does not raise).

    Args:
        runtime_name: AgentCore runtime name (e.g., "multiplier_hr_sonnet")
        prompt: The prompt to send to the agent
        timeout: Maximum seconds to wait for response (default: 120)

    Returns:
        InvocationResult with response, session_id, duration_ms, and success flag.
    """
    cmd = [AGENTCORE, "invoke", "--runtime", runtime_name, prompt]

    start_time = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed_ms = (time.time() - start_time) * 1000

        combined_output = result.stdout + result.stderr
        session_id = parse_session_id(combined_output)

        return InvocationResult(
            response=result.stdout.strip(),
            session_id=session_id,
            duration_ms=elapsed_ms,
            success=result.returncode == 0,
        )
    except subprocess.TimeoutExpired:
        elapsed_ms = (time.time() - start_time) * 1000
        return InvocationResult(
            response="",
            session_id=None,
            duration_ms=elapsed_ms,
            success=False,
            error=f"Timeout after {timeout}s",
        )


# ---------------------------------------------------------------------------
# BYO Agent Invocation
# ---------------------------------------------------------------------------


def invoke_byo(model_key: str, service_name: str, prompt: str, timeout: int = 300) -> InvocationResult:
    """Invoke a BYO agent with ADOT instrumentation.

    Runs the BYO runner script wrapped with `opentelemetry-instrument` to export
    traces to CloudWatch via ADOT. Sets ADOT environment variables and
    OTEL_RESOURCE_ATTRIBUTES with the service name for span identification.

    Records service_name and invocation_timestamp for later span retrieval
    from the CloudWatch aws/spans log group.

    If the subprocess times out, the invocation is recorded as failed with an
    error message and the function returns normally (does not raise).

    Args:
        model_key: Model identifier (e.g., "sonnet", "nova_2_pro", "glm_5")
        service_name: OTEL service name for span filtering (e.g., "multiplier-byo-sonnet")
        prompt: The prompt to send to the agent
        timeout: Maximum seconds to wait for response (default: 300)

    Returns:
        InvocationResult with response, service_name, invocation_timestamp,
        duration_ms, and success flag.
    """
    # Configure the BYO agent log group (required for evaluation)
    # This tells ADOT where to export GenAI log events so the evaluator can find them
    byo_log_group = f"/aws/bedrock-agentcore/runtimes/{service_name}"

    env = {
        **os.environ,
        **ADOT_ENV,
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"service.name={service_name},"
            f"aws.log.group.names={byo_log_group},"
            f"aws.service.type=gen_ai_agent"
        ),
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS": (
            f"x-aws-log-group={byo_log_group},"
            f"x-aws-log-stream=runtime-logs,"
            f"x-aws-metric-namespace=bedrock-agentcore"
        ),
    }
    cmd = [OTEL_INSTRUMENT, PYTHON, BYO_RUNNER, "--model", model_key, "--prompt", prompt]

    invocation_start = datetime.now(timezone.utc)
    # Generate a unique session ID for this invocation
    import uuid as _uuid
    session_id = f"byo-{service_name}-{_uuid.uuid4().hex[:8]}"
    env["BYO_SESSION_ID"] = session_id

    start_time = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        elapsed_ms = (time.time() - start_time) * 1000

        return InvocationResult(
            response=result.stdout.strip(),
            session_id=None,
            duration_ms=elapsed_ms,
            success=result.returncode == 0,
            service_name=service_name,
            invocation_timestamp=invocation_start,
        )
    except subprocess.TimeoutExpired:
        elapsed_ms = (time.time() - start_time) * 1000
        return InvocationResult(
            response="",
            session_id=None,
            duration_ms=elapsed_ms,
            success=False,
            service_name=service_name,
            invocation_timestamp=invocation_start,
            error=f"Timeout after {timeout}s",
        )


# ---------------------------------------------------------------------------
# Intermediate State Persistence
# ---------------------------------------------------------------------------


def save_intermediate_state(state: IntermediateState, path: str) -> None:
    """Save intermediate state to a JSON file for resumption on failure.

    Creates parent directories if they don't exist. Serializes the
    IntermediateState dataclass to JSON with datetime objects converted
    to ISO format strings.

    Args:
        state: The IntermediateState object to persist.
        path: File path to write the JSON state (e.g., "results/unified_intermediate.json").
    """
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(asdict(state), f, indent=2, default=str)


def load_intermediate_state(path: str) -> list[AgentInvocationData]:
    """Load intermediate state from a JSON file and reconstruct AgentInvocationData objects.

    Reads the JSON intermediate state file saved by a previous run and
    reconstructs the list of AgentInvocationData objects from the saved
    dictionaries. Handles datetime deserialization for invocation_timestamp
    fields (ISO format strings back to datetime objects).

    This is used when `--skip-invocation` is set to resume from a previous
    invocation phase without re-running agent invocations.

    Args:
        path: File path to the intermediate JSON state file.

    Returns:
        List of AgentInvocationData objects reconstructed from the saved state.

    Raises:
        FileNotFoundError: If the intermediate state file does not exist.
        json.JSONDecodeError: If the file contains invalid JSON.
        KeyError: If the file does not contain invocation_data.
    """
    with open(path) as f:
        state_dict = json.load(f)

    raw_invocation_data = state_dict.get("invocation_data")
    if not raw_invocation_data:
        raise KeyError(
            f"Intermediate state file '{path}' does not contain 'invocation_data'. "
            f"Phase completed: {state_dict.get('phase_completed', 'unknown')}"
        )

    invocation_data: list[AgentInvocationData] = []
    for agent_dict in raw_invocation_data:
        # Reconstruct InvocationResult objects from dicts
        results: list[InvocationResult] = []
        for result_dict in agent_dict.get("results", []):
            # Parse invocation_timestamp from ISO string back to datetime
            inv_ts = result_dict.get("invocation_timestamp")
            if inv_ts and isinstance(inv_ts, str):
                try:
                    inv_ts = datetime.fromisoformat(inv_ts)
                except (ValueError, TypeError):
                    inv_ts = None

            results.append(
                InvocationResult(
                    response=result_dict.get("response", ""),
                    session_id=result_dict.get("session_id"),
                    duration_ms=result_dict.get("duration_ms", 0.0),
                    success=result_dict.get("success", False),
                    service_name=result_dict.get("service_name"),
                    invocation_timestamp=inv_ts,
                    error=result_dict.get("error"),
                )
            )

        invocation_data.append(
            AgentInvocationData(
                name=agent_dict["name"],
                agent_type=agent_dict["agent_type"],
                model_key=agent_dict["model_key"],
                runtime_arn=agent_dict.get("runtime_arn"),
                service_name=agent_dict.get("service_name"),
                results=results,
            )
        )

    return invocation_data


# ---------------------------------------------------------------------------
# Phase 1: Concurrent Invocation Orchestrator
# ---------------------------------------------------------------------------


def run_managed_agent(agent_info: dict, config: UnifiedConfig) -> AgentInvocationData:
    """Run all prompts sequentially for a single managed agent.

    Iterates over PROMPTS, calling invoke_managed() for each prompt with a
    5-second delay between prompts to avoid per-model throttling.

    Args:
        agent_info: Dict with keys 'name', 'runtime', 'model_key'.
        config: UnifiedConfig instance with managed_timeout.

    Returns:
        AgentInvocationData containing all invocation results for this agent.
    """
    data = AgentInvocationData(
        name=agent_info["name"],
        agent_type="managed",
        model_key=agent_info["model_key"],
        runtime_arn=agent_info["runtime"],
    )

    for i, prompt in enumerate(PROMPTS):
        print(f"    [{agent_info['name']}] Prompt {i + 1}/{len(PROMPTS)}")
        result = invoke_managed(
            runtime_name=agent_info["runtime"],
            prompt=prompt,
            timeout=config.managed_timeout,
        )
        data.results.append(result)

        # Sleep 5s between prompts (not after the last one)
        if i < len(PROMPTS) - 1:
            time.sleep(5)

    successful = sum(1 for r in data.results if r.success)
    print(f"    [{agent_info['name']}] Complete — {successful}/{len(PROMPTS)} successful")
    return data


def run_byo_agent(agent_info: dict, config: UnifiedConfig) -> AgentInvocationData:
    """Run all prompts sequentially for a single BYO agent.

    Iterates over PROMPTS, calling invoke_byo() for each prompt with a
    5-second delay between prompts to avoid per-model throttling.

    Args:
        agent_info: Dict with keys 'name', 'model_key', 'service_name'.
        config: UnifiedConfig instance with byo_timeout.

    Returns:
        AgentInvocationData containing all invocation results for this agent.
    """
    data = AgentInvocationData(
        name=agent_info["name"],
        agent_type="byo",
        model_key=agent_info["model_key"],
        service_name=agent_info["service_name"],
    )

    for i, prompt in enumerate(PROMPTS):
        print(f"    [{agent_info['name']}] Prompt {i + 1}/{len(PROMPTS)}")
        result = invoke_byo(
            model_key=agent_info["model_key"],
            service_name=agent_info["service_name"],
            prompt=prompt,
            timeout=config.byo_timeout,
        )
        data.results.append(result)

        # Sleep 5s between prompts (not after the last one)
        if i < len(PROMPTS) - 1:
            time.sleep(5)

    successful = sum(1 for r in data.results if r.success)
    print(f"    [{agent_info['name']}] Complete — {successful}/{len(PROMPTS)} successful")
    return data


def run_invocation_phase(config: UnifiedConfig) -> list[AgentInvocationData]:
    """Run invocation phase: invoke all 6 agents concurrently.

    Creates a ThreadPoolExecutor with max_workers=6 and submits all 3 managed
    and 3 BYO agents. Each agent runs its 5 prompts sequentially. After all
    agents complete, saves intermediate state to the configured path.

    Args:
        config: UnifiedConfig instance with max_workers, timeouts, and intermediate_path.

    Returns:
        List of AgentInvocationData objects (one per agent, 6 total).
    """
    all_results: list[AgentInvocationData] = []

    print(f"  Invoking {len(MANAGED_AGENTS)} managed + {len(BYO_AGENTS)} BYO agents "
          f"({len(PROMPTS)} prompts each, max_workers={config.max_workers})")

    with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        futures = {}

        # Submit managed agents
        for agent_info in MANAGED_AGENTS:
            future = executor.submit(run_managed_agent, agent_info, config)
            futures[future] = agent_info["name"]

        # Submit BYO agents
        for agent_info in BYO_AGENTS:
            future = executor.submit(run_byo_agent, agent_info, config)
            futures[future] = agent_info["name"]

        # Collect results as they complete
        for future in as_completed(futures):
            agent_name = futures[future]
            try:
                result = future.result()
                all_results.append(result)
            except Exception as e:
                print(f"  [ERROR] Agent {agent_name} failed: {e}")

    # Save intermediate state after invocation phase completes
    state = IntermediateState(
        phase_completed="invocation",
        invocation_data=[asdict(d) for d in all_results],
    )
    save_intermediate_state(state, config.intermediate_path)
    print(f"  Intermediate state saved to {config.intermediate_path}")

    return all_results


# ---------------------------------------------------------------------------
# Phase 2: Trace Propagation Wait
# ---------------------------------------------------------------------------


def run_wait_phase(config: UnifiedConfig) -> None:
    """Wait for trace propagation with countdown messages.

    Sleeps for `config.wait_seconds` in 30-second increments, printing
    a countdown message every 30 seconds indicating remaining wait time.
    After the wait completes, saves intermediate state.

    Args:
        config: UnifiedConfig instance with wait_seconds and intermediate_path.
    """
    remaining = config.wait_seconds

    while remaining > 0:
        sleep_interval = min(30, remaining)
        print(f"  Waiting for trace propagation... {remaining}s remaining")
        time.sleep(sleep_interval)
        remaining -= sleep_interval

    # Save intermediate state after wait completes
    state = IntermediateState(phase_completed="wait")
    save_intermediate_state(state, config.intermediate_path)


# ---------------------------------------------------------------------------
# Phase 3: BYO Span Retrieval from CloudWatch Logs Insights
# ---------------------------------------------------------------------------


def build_spans_query(service_name: str) -> str:
    """Build CloudWatch Logs Insights query for BYO agent spans.

    Constructs a query that filters log events by the service.name resource
    attribute, returning timestamps and messages sorted chronologically.

    Args:
        service_name: The OTEL service name to filter by (e.g., "multiplier-byo-sonnet").

    Returns:
        A CloudWatch Logs Insights query string.
    """
    return f"""fields @timestamp, @message
| filter @message like /"{service_name}"/
| sort @timestamp asc
| limit 1000"""


def poll_query_results(cw_logs_client, query_id: str, max_wait: int = 120) -> list[dict]:
    """Poll CloudWatch Logs Insights query until complete or failed.

    Polls `get_query_results` every 2 seconds until the query status is
    "Complete" or "Failed", or until the maximum wait time is exceeded.

    Args:
        cw_logs_client: boto3 CloudWatch Logs client.
        query_id: The query ID returned by `start_query`.
        max_wait: Maximum seconds to wait for query completion (default: 120).

    Returns:
        List of result dicts from the query. Each dict has a list of
        field/value pairs. Returns empty list on failure or timeout.
    """
    elapsed = 0
    poll_interval = 2

    while elapsed < max_wait:
        response = cw_logs_client.get_query_results(queryId=query_id)
        status = response.get("status", "")

        if status == "Complete":
            # Convert results from [{field, value}] format to flat dicts
            results = []
            for row in response.get("results", []):
                entry = {}
                for field_pair in row:
                    entry[field_pair["field"]] = field_pair["value"]
                results.append(entry)
            return results
        elif status == "Failed":
            print(f"    [WARN] CW Logs Insights query {query_id} failed")
            return []

        time.sleep(poll_interval)
        elapsed += poll_interval

    print(f"    [WARN] CW Logs Insights query {query_id} timed out after {max_wait}s")
    return []


def parse_spans_from_log_events(log_events: list[dict]) -> list[dict]:
    """Parse CloudWatch log events into Session_Spans format.

    Each log event contains a JSON-encoded OTEL span in the `@message` field.
    This function extracts and parses them into the format expected by the
    AgentCore Evaluate API sessionSpans parameter.

    Invalid JSON messages are skipped without raising errors.

    Args:
        log_events: List of dicts from CloudWatch Logs Insights results,
                    each containing an `@message` field with JSON span data.

    Returns:
        List of parsed span dicts suitable for the Evaluate API sessionSpans parameter.
    """
    spans = []
    for event in log_events:
        message = event.get("@message", "")
        try:
            span_data = json.loads(message)
            spans.append(span_data)
        except json.JSONDecodeError:
            continue
    return spans


def retrieve_byo_spans(
    cw_logs_client,
    service_name: str,
    invocation_start: datetime,
    invocation_end: datetime,
) -> list[dict]:
    """Retrieve spans from CloudWatch aws/spans log group for a BYO agent session.

    Queries CloudWatch Logs Insights for spans matching the given service name
    within the time range. Applies a 60-second buffer before invocation_start
    to account for clock skew, and a 60-second buffer after invocation_end.

    Implements retry logic: if no spans are found on the first attempt, waits
    60 seconds and retries once. If still empty after retry, returns an empty
    list (caller will mark the session as unevaluated).

    Args:
        cw_logs_client: boto3 CloudWatch Logs client.
        service_name: BYO agent service name (e.g., "multiplier-byo-sonnet").
        invocation_start: When the invocation started.
        invocation_end: When the invocation completed.

    Returns:
        List of span dicts in Session_Spans format for the Evaluate API.
        Returns empty list if no spans found after retry.
    """
    query_start = int((invocation_start - timedelta(seconds=60)).timestamp())
    query_end = int((invocation_end + timedelta(seconds=60)).timestamp())

    query_string = build_spans_query(service_name)

    def _execute_query() -> list[dict]:
        query_id = cw_logs_client.start_query(
            logGroupName="aws/spans",
            startTime=query_start,
            endTime=query_end,
            queryString=query_string,
        )["queryId"]
        results = poll_query_results(cw_logs_client, query_id)
        return parse_spans_from_log_events(results)

    # First attempt
    spans = _execute_query()
    if spans:
        return spans

    # Retry once after 60s if no spans found
    print(f"    [{service_name}] No spans found, retrying after 60s...")
    time.sleep(60)
    spans = _execute_query()

    if not spans:
        print(f"    [{service_name}] No spans found after retry — marking as unevaluated")

    return spans


# ---------------------------------------------------------------------------
# Phase 3: Unified Evaluation via AgentCore Evaluate API
# ---------------------------------------------------------------------------

THROTTLING_ERROR_CODES = ("ThrottlingException", "TooManyRequestsException", "Throttling")


def parse_evaluation_response(response: dict) -> list[EvaluationScore]:
    """Parse the AgentCore Evaluate API response into EvaluationScore objects.

    The API returns evaluationResults with fields:
      - evaluatorId / evaluatorName: the evaluator identifier
      - value: numeric score (double)
      - label: human-readable label (e.g., "Good", "Excellent")
      - explanation: justification text

    Args:
        response: Raw response dict from client.evaluate().

    Returns:
        List of EvaluationScore objects, one per evaluator in the response.
    """
    scores: list[EvaluationScore] = []

    evaluation_results = response.get("evaluationResults", response.get("results", []))

    for result in evaluation_results:
        evaluator_name = result.get("evaluatorName", result.get("evaluatorId", "unknown"))
        score = result.get("value", result.get("score", 0.0))
        justification = result.get("explanation", result.get("justification", None))

        # Skip results with error codes
        if result.get("errorCode") or result.get("errorMessage"):
            error_msg = result.get("errorMessage", result.get("errorCode", "Unknown error"))
            print(f"    [EVAL ERROR] {evaluator_name}: {error_msg}")
            continue

        scores.append(
            EvaluationScore(
                evaluator_name=evaluator_name,
                score=float(score),
                justification=justification,
            )
        )

    return scores


def _is_throttling_error(error: ClientError) -> bool:
    """Check if a ClientError is a throttling error.

    Args:
        error: A botocore ClientError exception.

    Returns:
        True if the error code indicates throttling.
    """
    error_code = error.response.get("Error", {}).get("Code", "")
    return error_code in THROTTLING_ERROR_CODES


def _call_evaluate_with_retry(client, params: dict, max_retries: int = 3) -> list[EvaluationScore]:
    """Call evaluate() with retry logic.

    Retry strategy:
    - On throttling (ClientError with throttling error code): exponential
      backoff (10s, 20s, 40s) up to max_retries.
    - On general failure (any other exception): retry once after 10s delay,
      then raise on second failure.

    Args:
        client: boto3 bedrock-agentcore client with evaluate() method.
        params: Parameters dict to pass to client.evaluate().
        max_retries: Maximum number of retries for throttling errors (default: 3).

    Returns:
        List of EvaluationScore objects from the parsed response.

    Raises:
        ClientError: If throttling persists after max_retries.
        Exception: If a non-throttling error occurs after the first retry.
    """
    for attempt in range(max_retries + 1):
        try:
            response = client.evaluate(**params)
            return parse_evaluation_response(response)
        except ClientError as e:
            if _is_throttling_error(e):
                if attempt >= max_retries:
                    raise
                delay = 10 * (2 ** attempt)  # 10s, 20s, 40s
                print(f"    [THROTTLE] Retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                # General ClientError — retry once after 10s
                if attempt == 0:
                    print(f"    [RETRY] Evaluate failed ({e}), retrying in 10s...")
                    time.sleep(10)
                    continue
                raise
        except Exception as e:
            # Non-ClientError exception — retry once after 10s
            if attempt == 0:
                print(f"    [RETRY] Evaluate failed ({e}), retrying in 10s...")
                time.sleep(10)
                continue
            raise


def evaluate_agent(
    agentcore_client,
    agent_type: str,
    evaluator_names: list[str],
    runtime_arn: str | None = None,
    session_id: str | None = None,
    session_spans: list[dict] | None = None,
    trace_id: str | None = None,
) -> list[EvaluationScore]:
    """Evaluate an agent trace using the agentcore run eval CLI.

    Uses the `agentcore run eval` CLI with `--trace-id` to evaluate traces.
    The CLI handles log event lookup internally through the AgentCore service.

    For managed agents: uses --runtime <name> --evaluator <name> --trace-id <id>
    For BYO agents: uses --runtime <name> --evaluator <name> --trace-id <id>
    (BYO traces are evaluated through a managed runtime reference since the
    evaluator is the same and traces are in the shared aws/spans log group)

    Args:
        agentcore_client: boto3 bedrock-agentcore client (unused, kept for interface compat).
        agent_type: Either "managed" or "byo".
        evaluator_names: List of evaluator names (project-level names, e.g., ["multiplier_domain_accuracy"]).
        runtime_arn: Runtime name for the CLI --runtime flag.
        session_id: Session ID (unused in trace-based evaluation).
        session_spans: List of span dicts (used to extract trace IDs).
        trace_id: Trace ID to evaluate directly.

    Returns:
        List of EvaluationScore objects with scores and justifications.
    """
    # Extract trace ID from spans if not provided directly
    if not trace_id and session_spans:
        # Get the trace ID from the first span (all spans in a session share a trace)
        for span in session_spans:
            tid = span.get("traceId")
            if tid:
                trace_id = tid
                break

    if not trace_id:
        return []

    all_scores: list[EvaluationScore] = []

    for evaluator_name in evaluator_names:
        # Build the CLI command based on agent type
        if agent_type == "managed" and runtime_arn:
            # Managed: use --runtime <name> --evaluator <name>
            cmd = [
                AGENTCORE, "run", "eval",
                "--runtime", runtime_arn,
                "--evaluator", evaluator_name,
                "--trace-id", trace_id,
                "--json",
            ]
        else:
            # BYO: use --runtime-arn and --evaluator-arn for standalone mode
            # We need the actual ARNs from the deployed state
            deployed_state = {}
            if DEPLOYED_STATE_PATH.exists():
                with open(DEPLOYED_STATE_PATH) as f:
                    deployed_state = json.load(f)

            # Get evaluator ARN
            evaluators = deployed_state.get("targets", {}).get("default", {}).get("resources", {}).get("evaluators", {})
            evaluator_info = evaluators.get(evaluator_name, {})
            evaluator_arn = evaluator_info.get("evaluatorArn", "")

            # Get any runtime ARN (needed for --runtime-arn even for BYO)
            runtimes = deployed_state.get("targets", {}).get("default", {}).get("resources", {}).get("runtimes", {})
            # Use the first available runtime ARN
            first_runtime = next(iter(runtimes.values()), {})
            runtime_arn_for_cli = first_runtime.get("runtimeArn", "")

            if not evaluator_arn or not runtime_arn_for_cli:
                print(f"    [EVAL WARN] {evaluator_name}: Missing ARNs for BYO evaluation")
                continue

            cmd = [
                AGENTCORE, "run", "eval",
                "--runtime-arn", runtime_arn_for_cli,
                "--evaluator-arn", evaluator_arn,
                "--trace-id", trace_id,
                "--region", "us-east-1",
                "--json",
            ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )

            # Strip ANSI escape codes from output
            import re as _re
            clean_stdout = _re.sub(r'\x1b\[[^a-zA-Z]*[a-zA-Z]', '', result.stdout).strip()

            if result.returncode != 0:
                # Try to parse JSON error
                try:
                    error_data = json.loads(clean_stdout)
                    error_msg = error_data.get("error", result.stderr.strip())
                except (json.JSONDecodeError, ValueError):
                    error_msg = result.stderr.strip() or clean_stdout
                print(f"    [EVAL WARN] {evaluator_name}: {error_msg[:200]}")
                continue

            # Parse JSON output
            output = json.loads(clean_stdout)
            if output.get("success") and output.get("run", {}).get("results"):
                for eval_result in output["run"]["results"]:
                    for session_score in eval_result.get("sessionScores", []):
                        score_value = session_score.get("value", 0.0)
                        explanation = session_score.get("explanation", "")
                        label = session_score.get("label", "")

                        all_scores.append(
                            EvaluationScore(
                                evaluator_name=eval_result.get("evaluator", evaluator_name),
                                score=float(score_value),
                                justification=f"[{label}] {explanation}" if label else explanation,
                            )
                        )
            else:
                error_msg = output.get("error", "Unknown error")
                print(f"    [EVAL WARN] {evaluator_name}: {error_msg[:200]}")

        except subprocess.TimeoutExpired:
            print(f"    [EVAL WARN] {evaluator_name}: Timeout after 120s")
        except Exception as e:
            print(f"    [EVAL WARN] {evaluator_name}: {e}")

    return all_scores


# ---------------------------------------------------------------------------
# Phase 3: Evaluation Phase Orchestrator
# ---------------------------------------------------------------------------

ACCOUNT_ID = "654654616949"

# Load deployed evaluator IDs from the deployed state
DEPLOYED_STATE_PATH = PROJECT_ROOT / "agentcore" / ".cli" / "deployed-state.json"


def _load_deployed_evaluator_ids() -> dict[str, str]:
    """Load deployed evaluator IDs from agentcore/.cli/deployed-state.json.

    Returns:
        Dict mapping evaluator name to deployed evaluator ID.
    """
    if not DEPLOYED_STATE_PATH.exists():
        return {}
    with open(DEPLOYED_STATE_PATH) as f:
        state = json.load(f)
    evaluators = (
        state.get("targets", {})
        .get("default", {})
        .get("resources", {})
        .get("evaluators", {})
    )
    return {name: info["evaluatorId"] for name, info in evaluators.items()}


def _resolve_evaluator_ids(evaluator_names: list[str]) -> list[str]:
    """Resolve evaluator names to deployed evaluator IDs.

    If a name matches a deployed evaluator, use its full ID.
    If a name starts with 'Builtin.', use it as-is.
    Otherwise, use the name as-is (may fail at API call time).

    Args:
        evaluator_names: List of evaluator names from config.

    Returns:
        List of resolved evaluator IDs.
    """
    deployed = _load_deployed_evaluator_ids()
    resolved = []
    for name in evaluator_names:
        if name in deployed:
            resolved.append(deployed[name])
            print(f"  Resolved evaluator '{name}' → '{deployed[name]}'")
        elif name.startswith("Builtin."):
            resolved.append(name)
        else:
            # Try as-is (might be a full ID already)
            resolved.append(name)
    return resolved


def _get_runtime_arns() -> dict[str, str]:
    """Load deployed runtime ARNs from agentcore/.cli/deployed-state.json.

    Returns:
        Dict mapping runtime name to runtime ARN.
    """
    if not DEPLOYED_STATE_PATH.exists():
        return {}
    with open(DEPLOYED_STATE_PATH) as f:
        state = json.load(f)
    runtimes = (
        state.get("targets", {})
        .get("default", {})
        .get("resources", {})
        .get("runtimes", {})
    )
    return {name: info["runtimeArn"] for name, info in runtimes.items()}


def run_evaluation_phase(
    config: UnifiedConfig,
    invocation_data: list[AgentInvocationData],
) -> dict[str, AgentEvalResults]:
    """Run evaluation phase using a unified approach.

    For managed agents: uses `agentcore run eval --runtime --trace-id` CLI
    (reads from runtime-specific log group).

    For BYO agents: reads evaluation results from the Online Eval Config output
    log groups. The online eval configs continuously process BYO sessions from
    their CloudWatch log groups using the same `multiplier_domain_accuracy` evaluator.

    Both paths use the SAME evaluator, producing directly comparable scores.

    Args:
        config: UnifiedConfig instance with aws_profile, aws_region, evaluators, max_workers.
        invocation_data: List of AgentInvocationData from the invocation phase.

    Returns:
        Dict mapping agent name to AgentEvalResults.
    """
    # Create boto3 session and clients
    session = boto3.Session(profile_name=config.aws_profile, region_name=config.aws_region)
    agentcore_client = session.client("bedrock-agentcore")
    cw_logs_client = session.client("logs")

    # Load runtime ARNs for managed agents
    runtime_arns = _get_runtime_arns()

    # Online eval config IDs for BYO agents (map service_name -> config_id)
    BYO_EVAL_CONFIGS = {
        "multiplier-byo-sonnet": "eddpoc_eval_byo_sonnet-AVImd57apu",
        "multiplier-byo-nova-2-pro": "eddpoc_eval_byo_nova_2_pro-UGf4Dw79AU",
        "multiplier-byo-glm-5": "eddpoc_eval_byo_glm_5-ujF6o573Ll",
    }

    # Managed agent online eval config IDs
    MANAGED_EVAL_CONFIGS = {
        "multiplier_hr_sonnet": "eddpoc_eval_sonnet-D6R6FHCa6w",
        "multiplier_hr_nova_2_pro": "eddpoc_eval_nova_2_pro-taaTtC7VN4",
        "multiplier_hr_glm_5": "eddpoc_eval_glm_5-eBX7lw3Kpg",
    }

    # Step 1: Get trace IDs for all agents (needed to match eval results to prompts)
    print("  Retrieving trace IDs from CloudWatch...")
    agent_trace_ids: dict[str, list[str]] = {}

    for agent_data in invocation_data:
        if agent_data.agent_type == "managed":
            runtime_arn_full = runtime_arns.get(agent_data.runtime_arn, "")
            runtime_id = runtime_arn_full.split("/")[-1] if runtime_arn_full else f"eddpoc_{agent_data.runtime_arn}"
            runtime_log_group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"

            query_end = datetime.now(timezone.utc)
            query_start = query_end - timedelta(seconds=3600)  # 1 hour lookback for managed traces

            try:
                query_string = 'fields @message | filter @message like /"traceId"/ | sort @timestamp asc | limit 500'
                query_id_resp = cw_logs_client.start_query(
                    logGroupName=runtime_log_group,
                    startTime=int(query_start.timestamp()),
                    endTime=int(query_end.timestamp()),
                    queryString=query_string,
                )["queryId"]
                log_results = poll_query_results(cw_logs_client, query_id_resp)

                trace_ids_found: list[str] = []
                for event in log_results:
                    message = event.get("@message", "")
                    try:
                        span_data = json.loads(message)
                        tid = span_data.get("traceId")
                        if tid and tid not in trace_ids_found:
                            trace_ids_found.append(tid)
                    except json.JSONDecodeError:
                        continue

                agent_trace_ids[agent_data.name] = trace_ids_found
                print(f"    [{agent_data.name}] Found {len(trace_ids_found)} unique trace IDs")
            except Exception as e:
                print(f"    [{agent_data.name}] Error: {e}")
                agent_trace_ids[agent_data.name] = []
        else:
            # BYO: get trace IDs from aws/spans
            first_result = next((r for r in agent_data.results if r.success), None)
            if not first_result:
                agent_trace_ids[agent_data.name] = []
                continue

            query_start = first_result.invocation_timestamp - timedelta(seconds=60)
            query_end = datetime.now(timezone.utc)

            try:
                query_string = f'fields @message | filter @message like /"{agent_data.service_name}"/ and @message like /"invoke_agent"/ | sort @timestamp asc | limit 100'
                query_id_resp = cw_logs_client.start_query(
                    logGroupName="aws/spans",
                    startTime=int(query_start.timestamp()),
                    endTime=int(query_end.timestamp()),
                    queryString=query_string,
                )["queryId"]
                log_results = poll_query_results(cw_logs_client, query_id_resp)

                trace_ids_found = []
                for event in log_results:
                    message = event.get("@message", "")
                    try:
                        span_data = json.loads(message)
                        tid = span_data.get("traceId")
                        if tid and tid not in trace_ids_found:
                            trace_ids_found.append(tid)
                    except json.JSONDecodeError:
                        continue

                agent_trace_ids[agent_data.name] = trace_ids_found
                print(f"    [{agent_data.name}] Found {len(trace_ids_found)} unique trace IDs")
            except Exception as e:
                print(f"    [{agent_data.name}] Error: {e}")
                agent_trace_ids[agent_data.name] = []

    # Step 2: Evaluate managed agents via CLI, BYO via online eval results
    print("  Running evaluations...")

    # Step 2a: Managed agents — use agentcore run eval CLI
    eval_results: dict[str, AgentEvalResults] = {}

    for agent_data in invocation_data:
        eval_results[agent_data.name] = AgentEvalResults(
            agent_name=agent_data.name,
            agent_type=agent_data.agent_type,
            model_key=agent_data.model_key,
            prompt_evaluations=[],
        )

        trace_ids = agent_trace_ids.get(agent_data.name, [])

        if agent_data.agent_type == "managed":
            # Managed: evaluate via CLI per trace
            for i, result in enumerate(agent_data.results):
                prompt_text = PROMPTS[i]
                if not result.success:
                    eval_results[agent_data.name].prompt_evaluations.append(
                        PromptEvaluation(prompt_index=i, prompt_text=prompt_text, scores=[], success=False, error="Invocation failed")
                    )
                    continue

                if i >= len(trace_ids):
                    eval_results[agent_data.name].prompt_evaluations.append(
                        PromptEvaluation(prompt_index=i, prompt_text=prompt_text, scores=[], success=False, error=f"No trace ID (have {len(trace_ids)})")
                    )
                    continue

                scores = evaluate_agent(
                    agentcore_client=agentcore_client,
                    agent_type="managed",
                    evaluator_names=config.evaluators,
                    runtime_arn=agent_data.runtime_arn,
                    trace_id=trace_ids[i],
                )

                if scores:
                    eval_results[agent_data.name].prompt_evaluations.append(
                        PromptEvaluation(prompt_index=i, prompt_text=prompt_text, scores=scores, success=True)
                    )
                else:
                    eval_results[agent_data.name].prompt_evaluations.append(
                        PromptEvaluation(prompt_index=i, prompt_text=prompt_text, scores=[], success=False, error="CLI returned no scores")
                    )

            managed_success = sum(1 for pe in eval_results[agent_data.name].prompt_evaluations if pe.success)
            print(f"    [{agent_data.name}] {managed_success}/{len(agent_data.results)} evaluated via CLI")

        else:
            # BYO: read results from online eval output log group
            config_id = BYO_EVAL_CONFIGS.get(agent_data.service_name)
            if not config_id:
                for i in range(len(agent_data.results)):
                    eval_results[agent_data.name].prompt_evaluations.append(
                        PromptEvaluation(prompt_index=i, prompt_text=PROMPTS[i], scores=[], success=False, error="No online eval config")
                    )
                continue

            eval_log_group = f"/aws/bedrock-agentcore/evaluations/results/{config_id}"

            # Query the online eval output for results matching our trace IDs
            trace_id_set = set(trace_ids)
            query_end = datetime.now(timezone.utc)
            query_start = query_end - timedelta(seconds=3600)  # 1 hour lookback for eval results

            try:
                query_string = 'fields @message | filter @message like /"gen_ai.evaluation.result"/ | sort @timestamp desc | limit 50'
                query_id_resp = cw_logs_client.start_query(
                    logGroupName=eval_log_group,
                    startTime=int(query_start.timestamp()),
                    endTime=int(query_end.timestamp()),
                    queryString=query_string,
                )["queryId"]
                log_results = poll_query_results(cw_logs_client, query_id_resp)

                # Parse results and match to trace IDs
                eval_by_trace: dict[str, dict] = {}
                all_eval_results_list: list[dict] = []
                for event in log_results:
                    message = event.get("@message", "")
                    try:
                        result_data = json.loads(message)
                        tid = result_data.get("traceId", "")
                        attrs = result_data.get("attributes", {})

                        # Skip results with errors
                        if attrs.get("error") or attrs.get("error.message"):
                            error_msg = attrs.get("error.message", "Unknown evaluation error")
                            eval_entry = {
                                "score": None,
                                "explanation": "",
                                "evaluator": attrs.get("gen_ai.evaluation.name", "unknown"),
                                "session_id": attrs.get("session.id", ""),
                                "trace_id": tid,
                                "error": error_msg,
                            }
                        else:
                            eval_entry = {
                                "score": attrs.get("gen_ai.evaluation.score.value", None),
                                "explanation": attrs.get("gen_ai.evaluation.explanation", ""),
                                "evaluator": attrs.get("gen_ai.evaluation.name", "unknown"),
                                "session_id": attrs.get("session.id", ""),
                                "trace_id": tid,
                                "error": None,
                            }

                        if tid in trace_id_set:
                            eval_by_trace[tid] = eval_entry
                        all_eval_results_list.append(eval_entry)
                    except json.JSONDecodeError:
                        continue

                # If we couldn't match by trace ID, use the most recent results
                # (online eval may have processed sessions from a previous run)
                if not eval_by_trace and all_eval_results_list:
                    # Use the most recent N results (where N = number of prompts)
                    recent_results = all_eval_results_list[:len(trace_ids)]
                    for i, er in enumerate(recent_results):
                        if i < len(trace_ids):
                            eval_by_trace[trace_ids[i]] = er
                    print(f"    [{agent_data.name}] Using {len(eval_by_trace)} most recent online eval results (trace IDs didn't match)")

                print(f"    [{agent_data.name}] Found {len(eval_by_trace)}/{len(trace_ids)} online eval results")

                # Map results to prompts
                for i, result in enumerate(agent_data.results):
                    prompt_text = PROMPTS[i]
                    if not result.success:
                        eval_results[agent_data.name].prompt_evaluations.append(
                            PromptEvaluation(prompt_index=i, prompt_text=prompt_text, scores=[], success=False, error="Invocation failed")
                        )
                        continue

                    if i >= len(trace_ids):
                        eval_results[agent_data.name].prompt_evaluations.append(
                            PromptEvaluation(prompt_index=i, prompt_text=prompt_text, scores=[], success=False, error="No trace ID")
                        )
                        continue

                    tid = trace_ids[i]
                    if tid in eval_by_trace:
                        er = eval_by_trace[tid]
                        if er.get("error") or er.get("score") is None:
                            eval_results[agent_data.name].prompt_evaluations.append(
                                PromptEvaluation(prompt_index=i, prompt_text=prompt_text, scores=[], success=False, error=er.get("error", "Evaluation returned no score"))
                            )
                        else:
                            scores = [EvaluationScore(
                                evaluator_name=er["evaluator"],
                                score=float(er["score"]),
                                justification=er["explanation"],
                            )]
                            eval_results[agent_data.name].prompt_evaluations.append(
                                PromptEvaluation(prompt_index=i, prompt_text=prompt_text, scores=scores, success=True)
                            )
                    else:
                        eval_results[agent_data.name].prompt_evaluations.append(
                            PromptEvaluation(prompt_index=i, prompt_text=prompt_text, scores=[], success=False, error="Online eval result not yet available (may need more wait time)")
                        )

            except Exception as e:
                print(f"    [{agent_data.name}] Error reading online eval results: {e}")
                for i in range(len(agent_data.results)):
                    eval_results[agent_data.name].prompt_evaluations.append(
                        PromptEvaluation(prompt_index=i, prompt_text=PROMPTS[i], scores=[], success=False, error=str(e))
                    )

    # Sort prompt_evaluations by prompt_index
    for agent_eval in eval_results.values():
        agent_eval.prompt_evaluations.sort(key=lambda pe: pe.prompt_index)

    # Save intermediate state
    state = IntermediateState(
        phase_completed="evaluation",
        evaluation_results={name: asdict(results) for name, results in eval_results.items()},
    )
    save_intermediate_state(state, config.intermediate_path)
    print(f"  Intermediate state saved to {config.intermediate_path}")

    # Print summary
    total_evals = sum(1 for r in eval_results.values() for pe in r.prompt_evaluations if pe.success)
    total_prompts = sum(len(r.prompt_evaluations) for r in eval_results.values())
    print(f"  Evaluation complete: {total_evals}/{total_prompts} successful evaluations")

    return eval_results


# ---------------------------------------------------------------------------
# Phase 4: Report Generation
# ---------------------------------------------------------------------------


def compute_average_score(prompt_evaluations: list[PromptEvaluation]) -> float | None:
    """Compute the average score across all successful prompt evaluations.

    Iterates over prompt evaluations, collecting scores from successful
    evaluations (where success=True and scores are present). Computes the
    arithmetic mean of all individual evaluator scores across all prompts.

    Skips failed evaluations (success=False) and evaluations with no scores.

    Args:
        prompt_evaluations: List of PromptEvaluation objects for a single agent.

    Returns:
        The arithmetic mean of all non-None scores, or None if no valid scores exist.
    """
    all_scores: list[float] = []
    for pe in prompt_evaluations:
        if not pe.success:
            continue
        for score in pe.scores:
            all_scores.append(score.score)

    if not all_scores:
        return None

    return sum(all_scores) / len(all_scores)


def generate_unified_report(
    evaluation_results: dict[str, AgentEvalResults],
    metadata: ReportMetadata,
) -> str:
    """Generate the unified comparison report in Markdown format.

    Produces a comprehensive Markdown report with the following sections:
    1. Metadata (timestamp, evaluators, counts, execution time)
    2. Summary table (Agent, Deployment Type, Model, Avg Score, Per-Prompt)
    3. Per-model comparison (managed vs BYO for same model)
    4. Per-prompt detail (score + justification per agent-prompt)
    5. Failures section (if any evaluations failed)

    Writes the report to `results/unified_comparison.md` and returns the
    Markdown string.

    Args:
        evaluation_results: Dict mapping agent name to AgentEvalResults.
        metadata: ReportMetadata with timestamp, evaluator names, counts, and timing.

    Returns:
        The generated Markdown report as a string.
    """
    lines: list[str] = []

    # -----------------------------------------------------------------------
    # Section 1: Header and Metadata
    # -----------------------------------------------------------------------
    lines.append("# Unified Evaluation Comparison Report")
    lines.append("")
    lines.append("## Metadata")
    lines.append("")
    lines.append(f"- **Generated:** {metadata.timestamp}")
    lines.append(f"- **Evaluators:** {', '.join(metadata.evaluator_names)}")
    lines.append(f"- **Successful Evaluations:** {metadata.successful_evaluations} / {metadata.total_evaluations}")
    lines.append(f"- **Total Execution Time:** {metadata.total_execution_time_seconds:.1f}s")
    lines.append("")

    # -----------------------------------------------------------------------
    # Section 2: Summary Table
    # -----------------------------------------------------------------------
    lines.append("## Summary")
    lines.append("")
    lines.append("| Agent Name | Deployment Type | Model | Average Score | Per-Prompt Scores |")
    lines.append("|---|---|---|---|---|")

    for agent_name, results in sorted(evaluation_results.items()):
        avg_score = compute_average_score(results.prompt_evaluations)
        avg_str = f"{avg_score:.2f}" if avg_score is not None else "N/A"

        # Build per-prompt scores string
        per_prompt_parts: list[str] = []
        sorted_evals = sorted(results.prompt_evaluations, key=lambda pe: pe.prompt_index)
        for pe in sorted_evals:
            if pe.success and pe.scores:
                # Average across evaluators for this prompt
                prompt_avg = sum(s.score for s in pe.scores) / len(pe.scores)
                per_prompt_parts.append(f"P{pe.prompt_index + 1}:{prompt_avg:.1f}")
            else:
                per_prompt_parts.append(f"P{pe.prompt_index + 1}:FAIL")

        per_prompt_str = " ".join(per_prompt_parts)
        deployment_type = results.agent_type
        model = results.model_key

        lines.append(f"| {agent_name} | {deployment_type} | {model} | {avg_str} | {per_prompt_str} |")

    lines.append("")

    # -----------------------------------------------------------------------
    # Section 3: Per-Model Comparison (managed vs BYO)
    # -----------------------------------------------------------------------
    lines.append("## Per-Model Comparison (Managed vs BYO)")
    lines.append("")

    # Group results by model_key
    model_groups: dict[str, list[AgentEvalResults]] = {}
    for results in evaluation_results.values():
        model_groups.setdefault(results.model_key, []).append(results)

    for model_key in sorted(model_groups.keys()):
        agents_for_model = model_groups[model_key]
        lines.append(f"### Model: {model_key}")
        lines.append("")
        lines.append("| Agent Name | Deployment Type | Average Score |")
        lines.append("|---|---|---|")

        for agent_results in sorted(agents_for_model, key=lambda r: r.agent_type):
            avg_score = compute_average_score(agent_results.prompt_evaluations)
            avg_str = f"{avg_score:.2f}" if avg_score is not None else "N/A"
            lines.append(f"| {agent_results.agent_name} | {agent_results.agent_type} | {avg_str} |")

        lines.append("")

    # -----------------------------------------------------------------------
    # Section 4: Per-Prompt Detail
    # -----------------------------------------------------------------------
    lines.append("## Per-Prompt Detail")
    lines.append("")

    for agent_name, results in sorted(evaluation_results.items()):
        lines.append(f"### {agent_name} ({results.agent_type}, {results.model_key})")
        lines.append("")

        sorted_evals = sorted(results.prompt_evaluations, key=lambda pe: pe.prompt_index)
        for pe in sorted_evals:
            lines.append(f"**Prompt {pe.prompt_index + 1}:** {pe.prompt_text}")
            lines.append("")

            if not pe.success:
                lines.append(f"- ❌ **Failed:** {pe.error or 'Unknown error'}")
                lines.append("")
                continue

            if not pe.scores:
                lines.append("- No scores available")
                lines.append("")
                continue

            for score in pe.scores:
                justification_str = score.justification or "No justification provided"
                lines.append(f"- **{score.evaluator_name}:** {score.score:.2f}")
                lines.append(f"  - Justification: {justification_str}")

            lines.append("")

    # -----------------------------------------------------------------------
    # Section 5: Failures (only if any evaluations failed)
    # -----------------------------------------------------------------------
    failures: list[tuple[str, int, str]] = []
    for agent_name, results in evaluation_results.items():
        for pe in results.prompt_evaluations:
            if not pe.success:
                failures.append((agent_name, pe.prompt_index, pe.error or "Unknown error"))

    if failures:
        lines.append("## Failures")
        lines.append("")
        lines.append("| Agent | Prompt Index | Failure Reason |")
        lines.append("|---|---|---|")
        for agent_name, prompt_idx, reason in sorted(failures):
            lines.append(f"| {agent_name} | {prompt_idx + 1} | {reason} |")
        lines.append("")

    # -----------------------------------------------------------------------
    # Write report to file
    # -----------------------------------------------------------------------
    report_content = "\n".join(lines)

    output_path = Path(PROJECT_ROOT / "results" / "unified_comparison.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(report_content)

    print(f"  Report written to {output_path}")
    return report_content


# ---------------------------------------------------------------------------
# Phase 4: Registry Update (local + AWS)
# ---------------------------------------------------------------------------

REGISTRY_INFO_PATH = PROJECT_ROOT / "registry" / "registry_info.json"
AWS_REGISTRY_ID = "Rqbs73eeqpMEEwf9"


def _load_registry_info() -> dict:
    """Load registry_info.json with AWS record IDs."""
    if REGISTRY_INFO_PATH.exists():
        with open(REGISTRY_INFO_PATH) as f:
            return json.load(f)
    return {"records": []}


def _get_record_id_for_agent(agent_name: str, registry_info: dict) -> str | None:
    """Map local agent name to AWS registry record ID.

    Translates underscore-based local agent names to the hyphenated AWS
    registry record names, then looks up the record ID.

    Args:
        agent_name: Local agent name (e.g., "multiplier_hr_sonnet").
        registry_info: Dict loaded from registry_info.json.

    Returns:
        The AWS registry record ID, or None if not found.
    """
    name_map = {
        "multiplier_hr_sonnet": "multiplier-hr-sonnet-managed",
        "multiplier_hr_nova_2_pro": "multiplier-hr-nova-2-pro-managed",
        "multiplier_hr_glm_5": "multiplier-hr-glm-5-managed",
        "multiplier_byo_sonnet": "multiplier-hr-sonnet-byo",
        "multiplier_byo_nova_2_pro": "multiplier-hr-nova-2-pro-byo",
        "multiplier_byo_glm_5": "multiplier-hr-glm-5-byo",
    }
    aws_name = name_map.get(agent_name)
    if not aws_name:
        return None
    for rec in registry_info.get("records", []):
        if rec.get("name") == aws_name:
            return rec.get("record_id")
    return None


def update_registries(
    evaluation_results: dict[str, AgentEvalResults],
    config: UnifiedConfig,
) -> None:
    """Update local and AWS registries with evaluation scores.

    Computes average score per agent across all prompts, updates the local
    registry/registry.json via agent_registry.update_eval_results(), and
    updates AWS Agent Registry records via bedrock-agentcore-control client.

    Records evaluator as "unified_comparison" to distinguish from legacy
    evaluation methods. Continues on partial AWS failures (logs warning
    per failed record).

    Args:
        evaluation_results: Dict mapping agent name to AgentEvalResults.
        config: UnifiedConfig instance with aws_profile and aws_region.
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    # Import local registry module
    sys.path.insert(0, str(PROJECT_ROOT / "registry"))
    try:
        from agent_registry import update_eval_results
    except ImportError:
        update_eval_results = None
        print("  [WARN] Could not import agent_registry module, skipping local registry update")

    # Compute average scores and update local registry
    agent_scores: dict[str, float] = {}
    for agent_name, results in evaluation_results.items():
        avg_score = compute_average_score(results.prompt_evaluations)
        if avg_score is None:
            print(f"  [SKIP] {agent_name}: no valid scores to record")
            continue

        agent_scores[agent_name] = avg_score

        # Update local registry
        if update_eval_results is not None:
            scores_dict = {"unified_comparison_avg": round(avg_score, 3)}
            updated = update_eval_results(
                agent_name,
                scores_dict,
                timestamp,
                "unified_comparison",
            )
            if updated:
                print(f"  [LOCAL] Updated {agent_name}: avg_score={avg_score:.3f}")
            else:
                print(f"  [WARN] Agent {agent_name} not found in local registry")

    # Update AWS Agent Registry (best-effort)
    registry_info = _load_registry_info()
    if not registry_info.get("records"):
        print("  [WARN] No registry_info.json records found, skipping AWS registry update")
        return

    try:
        session = boto3.Session(
            profile_name=config.aws_profile,
            region_name=config.aws_region,
        )
        client = session.client("bedrock-agentcore-control")
    except Exception as e:
        print(f"  [WARN] Could not create bedrock-agentcore-control client: {e}")
        return

    updated_count = 0
    for agent_name, avg_score in agent_scores.items():
        record_id = _get_record_id_for_agent(agent_name, registry_info)
        if not record_id:
            print(f"  [WARN] No AWS registry record found for {agent_name}")
            continue

        try:
            # Read existing record metadata
            rec = client.get_registry_record(
                registryId=AWS_REGISTRY_ID,
                recordId=record_id,
            )
            current_descriptors = rec.get("descriptors", {})
            current_custom = current_descriptors.get("custom", {}).get("inlineContent", "{}")
            metadata = json.loads(current_custom)
        except Exception as e:
            print(f"  [WARN] Could not read AWS record for {agent_name}: {e}")
            metadata = {}

        # Update metadata with unified comparison scores
        metadata["last_eval_score"] = {"unified_comparison_avg": round(avg_score, 3)}
        metadata["last_eval_date"] = timestamp
        metadata["last_eval_details"] = {
            "evaluator": "unified_comparison",
            "prompts_count": len(PROMPTS),
            "timestamp": timestamp,
        }

        try:
            client.update_registry_record(
                registryId=AWS_REGISTRY_ID,
                recordId=record_id,
                descriptorType="CUSTOM",
                descriptors={
                    "optionalValue": {
                        "custom": {
                            "optionalValue": {
                                "inlineContent": json.dumps(metadata),
                            }
                        }
                    }
                },
            )
            updated_count += 1
            print(f"  [AWS] Updated {agent_name}: avg_score={avg_score:.3f}")
        except Exception as e:
            print(f"  [WARN] AWS registry update failed for {agent_name}: {e}")

    print(f"  AWS Registry: {updated_count}/{len(agent_scores)} records updated")


# ---------------------------------------------------------------------------
# Main entry point (placeholder — wired in task 7.1)
# ---------------------------------------------------------------------------


def main() -> None:
    """Main orchestrator for the unified comparison pipeline.

    Orchestrates all 4 phases sequentially:
      1. Invocation — invoke all 6 agents (or load from intermediate state)
      2. Wait — configurable trace propagation delay
      3. Evaluation — score all agents via AgentCore Evaluate API
      4. Report — generate comparison report + update registries

    Respects the --skip-invocation flag to resume from a previous run's
    intermediate state, skipping phases 1 and 2.

    Prints a final summary with: total agents evaluated, total prompts scored,
    average score per deployment type (managed vs BYO), and total wall-clock time.
    """
    start_time = time.time()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    config = build_config()

    print("=" * 70)
    print("  Unified Evaluation Comparison")
    print(f"  Started: {timestamp}")
    print(f"  Profile: {config.aws_profile} | Region: {config.aws_region}")
    print(f"  Evaluators: {', '.join(config.evaluators)}")
    print(f"  Wait: {config.wait_seconds}s | Skip invocation: {config.skip_invocation}")
    print("=" * 70)

    # Phase 1 & 2: Invocation + Wait (or skip via --skip-invocation)
    if config.skip_invocation:
        print("\n  [SKIP] Invocation and wait phases skipped (--skip-invocation)")
        print(f"  Loading intermediate state from {config.intermediate_path}...")
        invocation_data = load_intermediate_state(config.intermediate_path)
        print(f"  Loaded {len(invocation_data)} agents from intermediate state")
    else:
        # Phase 1: Invocation
        print_phase_start("Invocation")
        invocation_data = run_invocation_phase(config)
        print_phase_complete("Invocation")

        # Phase 2: Wait
        print_phase_start("Trace Propagation Wait")
        run_wait_phase(config)
        print_phase_complete("Trace Propagation Wait")

    # Phase 3: Evaluation
    print_phase_start("Evaluation")
    evaluation_results = run_evaluation_phase(config, invocation_data)
    print_phase_complete("Evaluation")

    # Phase 4: Report Generation + Registry Update
    print_phase_start("Report Generation")

    # Build ReportMetadata
    elapsed_so_far = time.time() - start_time
    successful_evals = sum(
        1
        for results in evaluation_results.values()
        for pe in results.prompt_evaluations
        if pe.success
    )
    total_evals = sum(
        len(results.prompt_evaluations)
        for results in evaluation_results.values()
    )

    metadata = ReportMetadata(
        timestamp=timestamp,
        evaluator_names=config.evaluators,
        successful_evaluations=successful_evals,
        total_evaluations=total_evals,
        total_execution_time_seconds=elapsed_so_far,
    )

    generate_unified_report(evaluation_results, metadata)
    update_registries(evaluation_results, config)
    print_phase_complete("Report Generation")

    # -----------------------------------------------------------------------
    # Final Summary
    # -----------------------------------------------------------------------
    elapsed = time.time() - start_time

    # Compute average score per deployment type
    managed_scores: list[float] = []
    byo_scores: list[float] = []
    for results in evaluation_results.values():
        avg = compute_average_score(results.prompt_evaluations)
        if avg is not None:
            if results.agent_type == "managed":
                managed_scores.append(avg)
            else:
                byo_scores.append(avg)

    managed_avg = sum(managed_scores) / len(managed_scores) if managed_scores else None
    byo_avg = sum(byo_scores) / len(byo_scores) if byo_scores else None

    total_prompts_scored = successful_evals

    print(f"\n{'=' * 70}")
    print("  COMPLETE — Unified Evaluation Comparison")
    print(f"{'─' * 70}")
    print(f"  Total agents evaluated:    {len(evaluation_results)}")
    print(f"  Total prompts scored:      {total_prompts_scored}")
    print(f"  Avg score (managed):       {managed_avg:.3f}" if managed_avg is not None else "  Avg score (managed):       N/A")
    print(f"  Avg score (BYO):           {byo_avg:.3f}" if byo_avg is not None else "  Avg score (BYO):           N/A")
    print(f"  Total wall-clock time:     {elapsed:.1f}s")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
