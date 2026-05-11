"""
Unified Trajectory Evaluation Comparison (POC)

Uses a SINGLE AgentCore code-based evaluator (`multiplier_trajectory_eval`) to
score both managed agents and BYO agents on the same rubric. Proves that the
BYO-outside-AgentCore-Runtime path CAN be unified with the managed path when
the evaluator is code-based (metadata-driven) rather than LLM-as-a-judge.

Pipeline:
  1. Invoke all 6 agents with 5 prompts each
  2. Wait for traces to propagate
  3. For each (agent, prompt) session, fetch sessionSpans from CloudWatch
  4. Call evaluate() API with the trajectory evaluator
  5. Collect scores, generate comparison report at results/trajectory_comparison.md
"""

import json
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REGION = os.environ.get("AWS_REGION", "us-east-1")
PROFILE = os.environ.get("AWS_PROFILE", "ml-sandbox")
EVALUATOR_ID = os.environ.get("EVALUATOR_ID", "multiplier_trajectory_eval-Evy2MEDqBq")
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "120"))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)
REPORT_PATH = RESULTS_DIR / "trajectory_comparison.md"

PROMPTS = [
    "What is the employment status of employee EMP-12345 in Singapore?",
    "What are the notice periods and regulatory requirements for employees in Thailand?",
    "Calculate the monthly payroll breakdown for an employee earning 90000 SGD annually in Singapore.",
    "Check the leave balance for employee EMP-67890. How many days do they have remaining?",
    "For employee EMP-11111 in India, look up their details, check compliance rules, and calculate their payroll breakdown in INR.",
]

# Agent configurations
MANAGED_AGENTS = [
    {
        "name": "multiplier_hr_sonnet",
        "runtime_name": "multiplier_hr_sonnet",
        "model_key": "sonnet",
        "service_name": "eddpoc_multiplier_hr_sonnet.DEFAULT",
        "log_group": "/aws/bedrock-agentcore/runtimes/eddpoc_multiplier_hr_sonnet-5YhsT625tI-DEFAULT",
    },
    {
        "name": "multiplier_hr_nova_2_pro",
        "runtime_name": "multiplier_hr_nova_2_pro",
        "model_key": "nova_2_pro",
        "service_name": "eddpoc_multiplier_hr_nova_2_pro.DEFAULT",
        "log_group": "/aws/bedrock-agentcore/runtimes/eddpoc_multiplier_hr_nova_2_pro-w5d1Pu9Uv4-DEFAULT",
    },
    {
        "name": "multiplier_hr_glm_5",
        "runtime_name": "multiplier_hr_glm_5",
        "model_key": "glm_5",
        "service_name": "eddpoc_multiplier_hr_glm_5.DEFAULT",
        "log_group": "/aws/bedrock-agentcore/runtimes/eddpoc_multiplier_hr_glm_5-SbMYgjFojx-DEFAULT",
    },
]

BYO_AGENTS = [
    {
        "name": "multiplier_byo_sonnet",
        "model_key": "sonnet",
        "service_name": "multiplier-byo-sonnet",
        "log_group": "/aws/bedrock-agentcore/runtimes/multiplier-byo-sonnet",
    },
    {
        "name": "multiplier_byo_nova_2_pro",
        "model_key": "nova_2_pro",
        "service_name": "multiplier-byo-nova-2-pro",
        "log_group": "/aws/bedrock-agentcore/runtimes/multiplier-byo-nova-2-pro",
    },
    {
        "name": "multiplier_byo_glm_5",
        "model_key": "glm_5",
        "service_name": "multiplier-byo-glm-5",
        "log_group": "/aws/bedrock-agentcore/runtimes/multiplier-byo-glm-5",
    },
]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Invocation:
    agent_name: str
    agent_type: str
    model_key: str
    prompt_index: int
    prompt_text: str
    trace_id: str | None = None
    session_id: str | None = None
    stdout_snippet: str = ""
    success: bool = False
    duration_s: float = 0.0
    error: str = ""


@dataclass
class Evaluation:
    agent_name: str
    agent_type: str
    model_key: str
    prompt_index: int
    prompt_text: str
    trace_id: str
    value: float | None = None
    label: str = ""
    explanation: str = ""
    success: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------

_boto_session = boto3.Session(profile_name=PROFILE, region_name=REGION)
_logs = _boto_session.client("logs")
_agentcore = _boto_session.client("bedrock-agentcore")


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------

def invoke_managed(agent: dict, prompt_index: int, prompt: str) -> Invocation:
    """Invoke a managed agent via agentcore invoke CLI."""
    inv = Invocation(
        agent_name=agent["name"],
        agent_type="managed",
        model_key=agent["model_key"],
        prompt_index=prompt_index,
        prompt_text=prompt,
    )
    cmd = ["agentcore", "invoke", "--runtime", agent["runtime_name"], prompt]
    start = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        inv.duration_s = time.time() - start
        inv.success = r.returncode == 0
        inv.stdout_snippet = r.stdout[:300]
        if not inv.success:
            inv.error = r.stderr[:300]
    except subprocess.TimeoutExpired:
        inv.duration_s = time.time() - start
        inv.error = "timeout"
    except Exception as e:
        inv.duration_s = time.time() - start
        inv.error = f"{type(e).__name__}: {e}"[:300]
    return inv


def invoke_byo(agent: dict, prompt_index: int, prompt: str) -> Invocation:
    """Invoke a BYO agent via opentelemetry-instrument wrapper."""
    inv = Invocation(
        agent_name=agent["name"],
        agent_type="byo",
        model_key=agent["model_key"],
        prompt_index=prompt_index,
        prompt_text=prompt,
    )
    session_id = f"byo-{agent['name']}-p{prompt_index}-{uuid.uuid4().hex[:6]}"
    inv.session_id = session_id

    env = {
        **os.environ,
        "AGENT_OBSERVABILITY_ENABLED": "true",
        "OTEL_PYTHON_DISTRO": "aws_distro",
        "OTEL_PYTHON_CONFIGURATOR": "aws_configurator",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_TRACES_EXPORTER": "otlp",
        "BYPASS_TOOL_CONSENT": "true",
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"service.name={agent['service_name']},"
            f"aws.log.group.names={agent['log_group']},"
            f"aws.service.type=gen_ai_agent"
        ),
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS": (
            f"x-aws-log-group={agent['log_group']},"
            f"x-aws-log-stream=runtime-logs,"
            f"x-aws-metric-namespace=bedrock-agentcore"
        ),
        "BYO_SESSION_ID": session_id,
    }

    cmd = [
        ".venv/bin/opentelemetry-instrument",
        ".venv/bin/python",
        "agents/byo_runner.py",
        "--model", agent["model_key"],
        "--prompt", prompt,
    ]

    start = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env, cwd=str(PROJECT_ROOT))
        inv.duration_s = time.time() - start
        inv.success = r.returncode == 0
        inv.stdout_snippet = r.stdout[:300]
        if not inv.success:
            inv.error = r.stderr[:300]
    except subprocess.TimeoutExpired:
        inv.duration_s = time.time() - start
        inv.error = "timeout"
    except Exception as e:
        inv.duration_s = time.time() - start
        inv.error = f"{type(e).__name__}: {e}"[:300]
    return inv


def run_invocations_for_agent(agent: dict, agent_type: str) -> list[Invocation]:
    """Invoke all 5 prompts sequentially for one agent (avoids per-model throttling)."""
    invs = []
    for i, prompt in enumerate(PROMPTS):
        print(f"  [{agent['name']}] prompt {i+1}/5...")
        if agent_type == "managed":
            inv = invoke_managed(agent, i, prompt)
        else:
            inv = invoke_byo(agent, i, prompt)
        invs.append(inv)
        time.sleep(3)  # small pause between prompts
    return invs


# ---------------------------------------------------------------------------
# Trace ID Discovery
# ---------------------------------------------------------------------------

def query_logs(log_group: str, start: int, end: int, query: str, wait_s: int = 10) -> list[dict]:
    qid = _logs.start_query(
        logGroupName=log_group, startTime=start, endTime=end, queryString=query,
    )["queryId"]
    # Poll
    for _ in range(wait_s):
        time.sleep(1)
        r = _logs.get_query_results(queryId=qid)
        if r.get("status") == "Complete":
            break
    return r.get("results", [])


def discover_trace_ids(agent: dict, agent_type: str, invocations: list[Invocation]) -> list[str]:
    """Find the trace IDs for each of the 5 prompt invocations of this agent."""
    end = int(time.time())
    start = end - 1800  # 30 min lookback

    # Find traces via aws/spans (works for both managed and BYO)
    service = agent["service_name"]
    query = f'fields @timestamp, @message | filter @message like /"{service}"/ and @message like /"invoke_agent"/ | sort @timestamp asc | limit 50'

    results = query_logs("aws/spans", start, end, query)
    trace_ids = []
    for row in results:
        for f in row:
            if f["field"] == "@message":
                try:
                    msg = json.loads(f["value"])
                    tid = msg.get("traceId")
                    if tid and tid not in trace_ids:
                        trace_ids.append(tid)
                except (json.JSONDecodeError, ValueError):
                    pass

    # Return the LAST 5 trace IDs (most recent invocations are ours)
    # They're already in chronological order (sort asc) so take last 5
    recent = trace_ids[-len(PROMPTS):] if len(trace_ids) >= len(PROMPTS) else trace_ids
    return recent


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def fetch_session_spans(log_group: str, trace_id: str) -> list[dict]:
    """Fetch spans from aws/spans + events from the agent's log group."""
    end = int(time.time())
    start = end - 3600

    # aws/spans (spans only)
    spans_results = query_logs(
        "aws/spans", start, end,
        f'fields @message | filter @message like /"{trace_id}"/ | sort @timestamp asc | limit 500',
    )
    spans = []
    for row in spans_results:
        for f in row:
            if f["field"] == "@message":
                try:
                    spans.append(json.loads(f["value"]))
                except (json.JSONDecodeError, ValueError):
                    pass

    # Agent log group (events — service will strip these but we include for completeness)
    ev_results = query_logs(
        log_group, start, end,
        f'fields @message | filter @message like /"{trace_id}"/ | sort @timestamp asc | limit 500',
    )
    events = []
    for row in ev_results:
        for f in row:
            if f["field"] == "@message":
                try:
                    events.append(json.loads(f["value"]))
                except (json.JSONDecodeError, ValueError):
                    pass

    return spans + events


def evaluate_one(agent: dict, agent_type: str, prompt_index: int, trace_id: str) -> Evaluation:
    """Call AgentCore evaluate() for one trace."""
    ev = Evaluation(
        agent_name=agent["name"],
        agent_type=agent_type,
        model_key=agent["model_key"],
        prompt_index=prompt_index,
        prompt_text=PROMPTS[prompt_index],
        trace_id=trace_id,
    )

    try:
        session_spans = fetch_session_spans(agent["log_group"], trace_id)
        if not session_spans:
            ev.error = "no spans found"
            return ev

        resp = _agentcore.evaluate(
            evaluatorId=EVALUATOR_ID,
            evaluationInput={"sessionSpans": session_spans},
            evaluationTarget={"traceIds": [trace_id]},
        )

        for r in resp.get("evaluationResults", []):
            if "errorCode" in r:
                ev.error = f"{r['errorCode']}: {r.get('errorMessage', '')[:200]}"
                return ev
            ev.value = float(r.get("value", 0))
            ev.label = str(r.get("label", ""))
            ev.explanation = str(r.get("explanation", ""))[:2000]
            ev.success = True
            return ev

        ev.error = "evaluate returned no results"
    except Exception as e:
        ev.error = f"{type(e).__name__}: {e}"[:300]
    return ev


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def compute_avg(evals: list[Evaluation]) -> float | None:
    values = [e.value for e in evals if e.success and e.value is not None]
    return sum(values) / len(values) if values else None


def generate_report(all_evals: dict[str, list[Evaluation]], metadata: dict) -> str:
    lines = []
    lines.append("# Unified Trajectory Evaluation Comparison")
    lines.append("")
    lines.append("## Metadata")
    lines.append("")
    lines.append(f"- **Generated:** {metadata['timestamp']}")
    lines.append(f"- **Evaluator:** `multiplier_trajectory_eval` (AgentCore code-based Lambda)")
    lines.append(f"- **Evaluator ID:** `{EVALUATOR_ID}`")
    lines.append(f"- **Evaluator type:** Deterministic metadata scoring (same Lambda for managed + BYO)")
    lines.append(f"- **Successful evaluations:** {metadata['success_count']} / {metadata['total_count']}")
    lines.append(f"- **Total wall-clock:** {metadata['wall_clock_s']:.1f}s")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Agent | Deployment | Model | Avg Score | P1 | P2 | P3 | P4 | P5 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")

    for name in sorted(all_evals.keys()):
        evals = sorted(all_evals[name], key=lambda e: e.prompt_index)
        if not evals:
            continue
        agent_type = evals[0].agent_type
        model = evals[0].model_key
        avg = compute_avg(evals)
        avg_str = f"{avg:.3f}" if avg is not None else "N/A"
        per_prompt = []
        for pi in range(5):
            match = next((e for e in evals if e.prompt_index == pi), None)
            if match and match.success and match.value is not None:
                per_prompt.append(f"{match.value:.2f}")
            else:
                per_prompt.append("FAIL")
        lines.append(f"| {name} | {agent_type} | {model} | {avg_str} | " + " | ".join(per_prompt) + " |")

    lines.append("")
    lines.append("## Per-Model Parity (Managed vs BYO)")
    lines.append("")

    by_model: dict[str, list[tuple[str, list[Evaluation]]]] = {}
    for name, evals in all_evals.items():
        if not evals:
            continue
        model = evals[0].model_key
        by_model.setdefault(model, []).append((name, evals))

    for model in sorted(by_model.keys()):
        lines.append(f"### {model}")
        lines.append("")
        lines.append("| Agent | Deployment | Avg Score | Label distribution |")
        lines.append("|---|---|---|---|")
        for name, evals in sorted(by_model[model], key=lambda p: p[1][0].agent_type):
            agent_type = evals[0].agent_type
            avg = compute_avg(evals)
            avg_str = f"{avg:.3f}" if avg is not None else "N/A"
            labels = [e.label for e in evals if e.success and e.label]
            label_counts: dict[str, int] = {}
            for l in labels:
                label_counts[l] = label_counts.get(l, 0) + 1
            label_str = ", ".join(f"{k}={v}" for k, v in sorted(label_counts.items(), key=lambda kv: -kv[1]))
            lines.append(f"| {name} | {agent_type} | {avg_str} | {label_str} |")
        lines.append("")

    lines.append("## Per-Prompt Detail")
    lines.append("")
    for name in sorted(all_evals.keys()):
        evals = sorted(all_evals[name], key=lambda e: e.prompt_index)
        if not evals:
            continue
        lines.append(f"### {name} ({evals[0].agent_type}, {evals[0].model_key})")
        lines.append("")
        for ev in evals:
            lines.append(f"**Prompt {ev.prompt_index + 1}:** {ev.prompt_text}")
            lines.append("")
            if ev.success and ev.value is not None:
                lines.append(f"- Trace: `{ev.trace_id}`")
                lines.append(f"- Score: **{ev.value:.3f}** ({ev.label})")
                lines.append(f"- {ev.explanation}")
            else:
                lines.append(f"- ❌ Failed: {ev.error}")
            lines.append("")

    lines.append("## Failures")
    lines.append("")
    failures = [
        (name, ev) for name, evals in all_evals.items() for ev in evals if not ev.success
    ]
    if not failures:
        lines.append("_No failures — all agent-prompt combinations evaluated successfully._")
    else:
        lines.append("| Agent | Prompt | Reason |")
        lines.append("|---|---|---|")
        for name, ev in failures:
            lines.append(f"| {name} | {ev.prompt_index + 1} | {ev.error[:200]} |")
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append("This report is produced by a SINGLE AgentCore code-based evaluator running as a Lambda.")
    lines.append("Both **managed** (AgentCore Runtime-hosted) and **BYO** (Strands + ADOT outside AgentCore) agents")
    lines.append("are scored via the same `evaluate()` API call — proving that the BYO path CAN be unified with")
    lines.append("the managed path when the evaluator is metadata-driven rather than content-driven.")
    lines.append("")
    lines.append("The evaluator scores on six deterministic criteria from span metadata alone:")
    lines.append("agent presence, tool success rate, tool variety, error-free execution, latency, and efficiency.")
    lines.append("Content-level evaluation (factual accuracy, completeness) requires the LLM-as-a-judge")
    lines.append("evaluator path — which still has the `invoke_agent` log event gap for BYO agents documented in")
    lines.append("`BYO_AgentCore_Observability_issue.md`.")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    wall_start = time.time()
    print(f"{'=' * 60}")
    print(f"Unified Trajectory Evaluation Comparison")
    print(f"{'=' * 60}")
    print(f"Evaluator: {EVALUATOR_ID}")
    print(f"Wait between invocation and eval: {WAIT_SECONDS}s")
    print()

    # ---- Phase 1: Invocation ----
    print("[Phase 1] Invoking all 6 agents (5 prompts each) in parallel per-agent, sequential within-agent...")

    all_invocations: dict[str, list[Invocation]] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {}
        for agent in MANAGED_AGENTS:
            futures[pool.submit(run_invocations_for_agent, agent, "managed")] = agent["name"]
        for agent in BYO_AGENTS:
            futures[pool.submit(run_invocations_for_agent, agent, "byo")] = agent["name"]
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                all_invocations[name] = fut.result()
                ok = sum(1 for inv in all_invocations[name] if inv.success)
                print(f"  ✓ {name}: {ok}/5 successful")
            except Exception as e:
                print(f"  ✗ {name}: {e}")
                all_invocations[name] = []

    # Save intermediate
    with open(RESULTS_DIR / "trajectory_invocations.json", "w") as f:
        json.dump({k: [asdict(i) for i in v] for k, v in all_invocations.items()}, f, indent=2, default=str)

    # ---- Phase 2: Wait for trace propagation ----
    print(f"\n[Phase 2] Waiting {WAIT_SECONDS}s for trace propagation...")
    remaining = WAIT_SECONDS
    while remaining > 0:
        step = min(30, remaining)
        print(f"  {remaining}s remaining...")
        time.sleep(step)
        remaining -= step

    # ---- Phase 3: Trace ID discovery + evaluation ----
    print(f"\n[Phase 3] Discovering trace IDs and evaluating...")

    all_evaluations: dict[str, list[Evaluation]] = {}

    all_agents_combined = [(a, "managed") for a in MANAGED_AGENTS] + [(a, "byo") for a in BYO_AGENTS]
    for agent, agent_type in all_agents_combined:
        name = agent["name"]
        invs = all_invocations.get(name, [])
        print(f"\n  {name}:")
        trace_ids = discover_trace_ids(agent, agent_type, invs)
        print(f"    Discovered {len(trace_ids)} trace IDs")

        evals: list[Evaluation] = []
        for i in range(len(PROMPTS)):
            if i >= len(trace_ids):
                evals.append(Evaluation(
                    agent_name=name, agent_type=agent_type, model_key=agent["model_key"],
                    prompt_index=i, prompt_text=PROMPTS[i], trace_id="",
                    error="no trace_id found",
                ))
                continue
            tid = trace_ids[i]
            ev = evaluate_one(agent, agent_type, i, tid)
            status = "✓" if ev.success else "✗"
            score = f"{ev.value:.2f}" if ev.success and ev.value is not None else ev.error[:60]
            print(f"    P{i+1}: {status} {score}")
            evals.append(ev)

        all_evaluations[name] = evals

    # ---- Phase 4: Report ----
    print(f"\n[Phase 4] Generating report...")
    wall_end = time.time()

    total_count = sum(len(v) for v in all_evaluations.values())
    success_count = sum(1 for v in all_evaluations.values() for ev in v if ev.success)

    metadata = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "wall_clock_s": wall_end - wall_start,
        "success_count": success_count,
        "total_count": total_count,
    }

    report = generate_report(all_evaluations, metadata)
    REPORT_PATH.write_text(report)

    # Save raw eval results
    with open(RESULTS_DIR / "trajectory_evaluations.json", "w") as f:
        json.dump({k: [asdict(e) for e in v] for k, v in all_evaluations.items()}, f, indent=2, default=str)

    # Final summary
    print(f"\n{'=' * 60}")
    print(f"COMPLETE")
    print(f"{'=' * 60}")
    print(f"Successful: {success_count}/{total_count}")
    print(f"Wall-clock: {metadata['wall_clock_s']:.1f}s")
    print(f"Report:     {REPORT_PATH}")

    # Per-deployment average
    managed_scores = [
        ev.value for evals in all_evaluations.values()
        for ev in evals if ev.success and ev.value is not None and ev.agent_type == "managed"
    ]
    byo_scores = [
        ev.value for evals in all_evaluations.values()
        for ev in evals if ev.success and ev.value is not None and ev.agent_type == "byo"
    ]
    if managed_scores:
        print(f"Avg managed score: {sum(managed_scores)/len(managed_scores):.3f} (n={len(managed_scores)})")
    if byo_scores:
        print(f"Avg BYO score:     {sum(byo_scores)/len(byo_scores):.3f} (n={len(byo_scores)})")


if __name__ == "__main__":
    main()
