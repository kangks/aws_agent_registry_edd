"""
Registry Comparison: Run all 6 agents (3 managed + 3 BYO) concurrently.

Uses ThreadPoolExecutor to run all 6 agents in parallel.
Each agent runs 5 prompts sequentially (to avoid per-model throttling).
Total: 30 invocations, ~2-3 minutes wall clock time.

After invocations:
- Phase 2: In-memory evaluation (ground-truth vs contender) using StrandsEvalsTelemetry
- Updates local registry.json with latest eval scores and timestamps
- Updates AWS Agent Registry records with eval scores via update_registry_record
- Generates results/registry_comparison.md

Usage:
    AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \\
        .venv/bin/python scripts/run_registry_comparison.py
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import boto3

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "agents"))
sys.path.insert(0, str(PROJECT_ROOT / "registry"))

from agent import create_agent, SUPPORTED_MODELS  # noqa: E402
from agent_registry import (  # noqa: E402
    get_all_agents,
    update_eval_results,
    get_stale_agents,
)

# ---------------------------------------------------------------------------
# AWS Agent Registry integration
# ---------------------------------------------------------------------------

AWS_REGISTRY_ID = "Rqbs73eeqpMEEwf9"
REGISTRY_INFO_PATH = PROJECT_ROOT / "registry" / "registry_info.json"


def _load_registry_info() -> dict:
    """Load registry_info.json with AWS record IDs."""
    if REGISTRY_INFO_PATH.exists():
        with open(REGISTRY_INFO_PATH) as f:
            return json.load(f)
    return {"records": []}


def _get_record_id_for_agent(agent_name: str, registry_info: dict) -> str | None:
    """Map local agent name to AWS registry record ID."""
    # Map from local names to AWS registry names
    name_map = {
        "multiplier_hr_sonnet": "multiplier-hr-sonnet-managed",
        "multiplier_hr_haiku": "multiplier-hr-haiku-managed",
        "multiplier_hr_nova_pro": "multiplier-hr-nova-pro-managed",
        "multiplier_byo_sonnet": "multiplier-hr-sonnet-byo",
        "multiplier_byo_haiku": "multiplier-hr-haiku-byo",
        "multiplier_byo_nova_pro": "multiplier-hr-nova-pro-byo",
    }
    aws_name = name_map.get(agent_name)
    if not aws_name:
        return None
    for rec in registry_info.get("records", []):
        if rec.get("name") == aws_name:
            return rec.get("record_id")
    return None


def update_aws_registry_records(eval_results: dict) -> None:
    """Update AWS Agent Registry records with latest eval scores."""
    registry_info = _load_registry_info()
    if not registry_info.get("records"):
        print("  [WARN] No registry_info.json found, skipping AWS registry update")
        return

    try:
        session = boto3.Session(
            profile_name=os.environ.get("AWS_PROFILE", "ml-sandbox"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        client = session.client("bedrock-agentcore-control")
    except Exception as e:
        print(f"  [WARN] Could not create AWS client: {e}")
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build scores per agent
    agent_scores = {}

    # Baseline
    baseline_h = eval_results.get("_baseline", {}).get("helpfulness", [])
    avg_bh = _avg(baseline_h)
    if avg_bh is not None:
        agent_scores["multiplier_hr_sonnet"] = {"helpfulness": round(avg_bh, 3), "role": "baseline"}
        agent_scores["multiplier_byo_sonnet"] = {"helpfulness": round(avg_bh, 3), "role": "baseline"}

    # Contenders
    for name, scores_data in eval_results.items():
        if name.startswith("_"):
            continue
        avg_c = _avg(scores_data.get("correctness", []))
        avg_h = _avg(scores_data.get("helpfulness", []))
        scores = {}
        if avg_c is not None:
            scores["correctness"] = round(avg_c, 3)
        if avg_h is not None:
            scores["helpfulness"] = round(avg_h, 3)
        if scores:
            agent_scores[name] = scores

    # Update each record in AWS
    updated = 0
    for agent_name, scores in agent_scores.items():
        record_id = _get_record_id_for_agent(agent_name, registry_info)
        if not record_id:
            continue

        # First get current record to read existing metadata
        try:
            rec = client.get_registry_record(registryId=AWS_REGISTRY_ID, recordId=record_id)
            current_descriptors = rec.get("descriptors", {})
            current_custom = current_descriptors.get("custom", {}).get("inlineContent", "{}")
            metadata = json.loads(current_custom)
        except Exception as e:
            print(f"    [WARN] Could not read record {record_id}: {e}")
            metadata = {}

        # Update metadata with eval scores
        metadata["last_eval_score"] = scores
        metadata["last_eval_date"] = timestamp
        metadata["last_eval_details"] = {
            "evaluator": "registry_comparison",
            "prompts_count": len(PROMPTS),
            "baseline_model": "sonnet",
        }

        # Update the record
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
            updated += 1
            print(f"    ✅ Updated AWS record: {agent_name} → {scores}")
        except Exception as e:
            print(f"    ❌ Failed to update {agent_name}: {e}")

    print(f"  AWS Registry: {updated}/{len(agent_scores)} records updated")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROMPTS = [
    "What is the employment status of employee EMP-12345 in Singapore?",
    "What are the notice periods and regulatory requirements for employees in Thailand?",
    "Calculate the monthly payroll breakdown for an employee earning 90000 SGD annually in Singapore.",
    "Check the leave balance for employee EMP-67890. How many days do they have remaining?",
    "For employee EMP-11111 in India, look up their details, check compliance rules, and calculate their payroll breakdown in INR.",
]

MANAGED_AGENTS = [
    {"name": "multiplier_hr_sonnet", "runtime": "multiplier_hr_sonnet", "model_key": "sonnet"},
    {"name": "multiplier_hr_haiku", "runtime": "multiplier_hr_haiku", "model_key": "haiku"},
    {"name": "multiplier_hr_nova_pro", "runtime": "multiplier_hr_nova_pro", "model_key": "nova_pro"},
]

BYO_AGENTS = [
    {"name": "multiplier_byo_sonnet", "model_key": "sonnet", "service_name": "multiplier-byo-sonnet"},
    {"name": "multiplier_byo_haiku", "model_key": "haiku", "service_name": "multiplier-byo-haiku"},
    {"name": "multiplier_byo_nova_pro", "model_key": "nova_pro", "service_name": "multiplier-byo-nova-pro"},
]

ADOT_ENV = {
    "AGENT_OBSERVABILITY_ENABLED": "true",
    "OTEL_PYTHON_DISTRO": "aws_distro",
    "OTEL_PYTHON_CONFIGURATOR": "aws_configurator",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "BYPASS_TOOL_CONSENT": "true",
}

PYTHON = str(PROJECT_ROOT / ".venv" / "bin" / "python")
OTEL_INSTRUMENT = str(PROJECT_ROOT / ".venv" / "bin" / "opentelemetry-instrument")
BYO_RUNNER = str(PROJECT_ROOT / "agents" / "byo_runner.py")
AGENTCORE = "agentcore"


# ---------------------------------------------------------------------------
# Phase 1: Invoke agents
# ---------------------------------------------------------------------------

def invoke_managed(runtime_name: str, prompt: str) -> dict:
    """Invoke a managed agent via agentcore CLI. Returns response + session_id."""
    cmd = [AGENTCORE, "invoke", "--runtime", runtime_name, prompt]
    start = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            env={**os.environ, "BYPASS_TOOL_CONSENT": "true"},
            cwd=str(PROJECT_ROOT),
        )
        duration_ms = (time.time() - start) * 1000
        combined = (result.stdout or "") + (result.stderr or "")

        # Parse session ID from output
        session_id = None
        for line in combined.split("\n"):
            if "Session:" in line:
                session_id = line.split("Session:")[-1].strip()
                break

        response_text = result.stdout.strip() if result.returncode == 0 else f"ERROR: {result.stderr[:200]}"
        return {
            "response": response_text,
            "session_id": session_id,
            "duration_ms": duration_ms,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"response": "ERROR: timeout", "session_id": None, "duration_ms": 120000, "success": False}
    except Exception as e:
        return {"response": f"ERROR: {e}", "session_id": None, "duration_ms": 0, "success": False}


def invoke_byo(model_key: str, service_name: str, prompt: str) -> dict:
    """Invoke a BYO agent via ADOT. Returns response + duration."""
    env = {**os.environ, **ADOT_ENV, "OTEL_RESOURCE_ATTRIBUTES": f"service.name={service_name}"}
    cmd = [OTEL_INSTRUMENT, PYTHON, BYO_RUNNER, "--model", model_key, "--prompt", prompt]
    start = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            env=env, cwd=str(PROJECT_ROOT),
        )
        duration_ms = (time.time() - start) * 1000
        response_text = result.stdout.strip() if result.returncode == 0 else f"ERROR: {result.stderr[:200]}"
        return {
            "response": response_text,
            "duration_ms": duration_ms,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"response": "ERROR: timeout", "duration_ms": 300000, "success": False}
    except Exception as e:
        return {"response": f"ERROR: {e}", "duration_ms": 0, "success": False}


def run_managed_agent(agent_info: dict) -> dict:
    """Run all 5 prompts for a managed agent sequentially."""
    name = agent_info["name"]
    runtime = agent_info["runtime"]
    results = []
    print(f"  [MANAGED] {name}: starting 5 prompts...")
    for i, prompt in enumerate(PROMPTS, 1):
        result = invoke_managed(runtime, prompt)
        results.append(result)
        status = "✓" if result["success"] else "✗"
        print(f"    {name} prompt {i}/5: {status} ({result['duration_ms']:.0f}ms)")
        if i < len(PROMPTS):
            time.sleep(5)  # Avoid throttling
    return {"name": name, "type": "managed", "model_key": agent_info["model_key"], "results": results}


def run_byo_agent(agent_info: dict) -> dict:
    """Run all 5 prompts for a BYO agent sequentially."""
    name = agent_info["name"]
    model_key = agent_info["model_key"]
    service_name = agent_info["service_name"]
    results = []
    print(f"  [BYO] {name}: starting 5 prompts...")
    for i, prompt in enumerate(PROMPTS, 1):
        result = invoke_byo(model_key, service_name, prompt)
        results.append(result)
        status = "✓" if result["success"] else "✗"
        print(f"    {name} prompt {i}/5: {status} ({result['duration_ms']:.0f}ms)")
        if i < len(PROMPTS):
            time.sleep(5)  # Avoid throttling
    return {"name": name, "type": "byo", "model_key": agent_info["model_key"], "results": results}


# ---------------------------------------------------------------------------
# Phase 2: In-memory evaluation
# ---------------------------------------------------------------------------

def run_evaluation(all_agent_results: list[dict]) -> dict:
    """
    Run in-memory evaluation for all agents.

    Uses Sonnet (managed) as baseline for correctness comparison.
    Evaluates all agents with HelpfulnessEvaluator.
    """
    from strands_evals import StrandsEvalsTelemetry  # noqa: E402
    from strands_evals.mappers import StrandsInMemorySessionMapper  # noqa: E402
    from strands_evals.evaluators import CorrectnessEvaluator, HelpfulnessEvaluator  # noqa: E402
    from strands_evals.types.evaluation import EvaluationData  # noqa: E402

    telemetry = StrandsEvalsTelemetry()
    telemetry.setup_in_memory_exporter()
    mapper = StrandsInMemorySessionMapper()
    correctness_eval = CorrectnessEvaluator()
    helpfulness_eval = HelpfulnessEvaluator()

    eval_results = {}

    # First, run baseline (sonnet managed) in-memory to get ground-truth responses
    print("\n  [EVAL] Running baseline (sonnet) in-memory for ground-truth...")
    baseline_agent = create_agent("sonnet")
    baseline_responses = []
    baseline_sessions = []
    baseline_helpfulness = []

    for i, prompt in enumerate(PROMPTS, 1):
        try:
            telemetry.in_memory_exporter.clear()
        except Exception:
            pass
        response = baseline_agent(prompt)
        response_text = str(response)
        baseline_responses.append(response_text)

        spans = list(telemetry.in_memory_exporter.get_finished_spans())
        session = None
        if spans:
            session = mapper.map_to_session(spans, session_id=f"eval-baseline-{i}")
        baseline_sessions.append(session)

        # Evaluate helpfulness
        score = None
        if session:
            try:
                ed = EvaluationData(input=prompt, actual_trajectory=session)
                res = helpfulness_eval.evaluate(ed)
                if res:
                    score = res[0].score
            except Exception as e:
                print(f"    [WARN] Helpfulness eval failed: {e}")
        baseline_helpfulness.append(score)
        print(f"    Baseline prompt {i}/5: helpfulness={score}")

        try:
            telemetry.in_memory_exporter.clear()
        except Exception:
            pass

    eval_results["_baseline"] = {
        "responses": baseline_responses,
        "helpfulness": baseline_helpfulness,
    }

    # Now evaluate each agent (contenders) against baseline
    for agent_data in all_agent_results:
        name = agent_data["name"]
        model_key = agent_data["model_key"]
        print(f"\n  [EVAL] Evaluating {name} against baseline...")

        agent = create_agent(model_key)
        agent_scores = {"correctness": [], "helpfulness": []}

        for i, prompt in enumerate(PROMPTS, 1):
            try:
                telemetry.in_memory_exporter.clear()
            except Exception:
                pass

            response = agent(prompt)
            spans = list(telemetry.in_memory_exporter.get_finished_spans())
            session = None
            if spans:
                session = mapper.map_to_session(spans, session_id=f"eval-{name}-{i}")

            correctness = None
            helpfulness = None

            # Correctness vs baseline
            if session and baseline_responses[i - 1]:
                try:
                    ed = EvaluationData(
                        input=prompt,
                        actual_trajectory=session,
                        expected_assertion=baseline_responses[i - 1],
                    )
                    res = correctness_eval.evaluate(ed)
                    if res:
                        correctness = res[0].score
                except Exception as e:
                    print(f"    [WARN] Correctness: {e}")

            # Helpfulness (absolute)
            if session:
                try:
                    ed = EvaluationData(input=prompt, actual_trajectory=session)
                    res = helpfulness_eval.evaluate(ed)
                    if res:
                        helpfulness = res[0].score
                except Exception as e:
                    print(f"    [WARN] Helpfulness: {e}")

            agent_scores["correctness"].append(correctness)
            agent_scores["helpfulness"].append(helpfulness)

            c_str = "✓" if correctness == 1.0 else "✗" if correctness == 0.0 else str(correctness)
            print(f"    {name} prompt {i}/5: correctness={c_str}, helpfulness={helpfulness}")

            try:
                telemetry.in_memory_exporter.clear()
            except Exception:
                pass

        eval_results[name] = agent_scores

    return eval_results


# ---------------------------------------------------------------------------
# Phase 3: Update registry + generate report
# ---------------------------------------------------------------------------

def update_registry_with_scores(eval_results: dict) -> None:
    """Update registry.json with latest eval scores."""
    timestamp = datetime.now(timezone.utc).isoformat()

    # Update baseline (sonnet managed + byo)
    baseline_h = eval_results.get("_baseline", {}).get("helpfulness", [])
    avg_baseline_h = _avg(baseline_h)
    if avg_baseline_h is not None:
        scores = {"helpfulness": round(avg_baseline_h, 3), "role": "baseline"}
        update_eval_results("multiplier_hr_sonnet", scores, timestamp, "registry_comparison")
        update_eval_results("multiplier_byo_sonnet", scores, timestamp, "registry_comparison")

    # Update contenders
    for name, scores_data in eval_results.items():
        if name.startswith("_"):
            continue
        avg_c = _avg(scores_data.get("correctness", []))
        avg_h = _avg(scores_data.get("helpfulness", []))
        scores = {}
        if avg_c is not None:
            scores["correctness"] = round(avg_c, 3)
        if avg_h is not None:
            scores["helpfulness"] = round(avg_h, 3)
        if scores:
            update_eval_results(name, scores, timestamp, "registry_comparison")


def generate_report(all_agent_results: list[dict], eval_results: dict) -> str:
    """Generate results/registry_comparison.md."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    agents = get_all_agents()
    stale = get_stale_agents(days=7)

    lines = [
        "# Agent Registry Comparison — All 6 Agents",
        "",
        f"**Generated:** {timestamp}",
        "**Agents:** 3 managed (AgentCore Runtime) + 3 BYO (ADOT → CloudWatch)",
        f"**Prompts:** {len(PROMPTS)} per agent = {len(PROMPTS) * 6} total invocations",
        "**Execution:** ThreadPoolExecutor(max_workers=6) — all agents run concurrently",
        "",
        "---",
        "",
        "## Registry Overview",
        "",
        "| Agent | Model | Type | Last Eval Score | Last Eval Time |",
        "|-------|-------|------|----------------|----------------|",
    ]

    for agent in agents:
        score = agent.get("last_eval_score")
        score_str = json.dumps(score) if score else "—"
        ts = agent.get("last_eval_timestamp")
        ts_str = ts[:19] if ts else "never"
        lines.append(f"| {agent['name']} | {agent['model'][:30]} | {agent['deployment_type']} | {score_str} | {ts_str} |")

    lines += [
        "",
        "---",
        "",
        "## Phase 1: Invocation Results (30 invocations)",
        "",
    ]

    # Per-agent invocation summary
    for agent_data in all_agent_results:
        name = agent_data["name"]
        agent_type = agent_data["type"]
        results = agent_data["results"]
        successes = sum(1 for r in results if r["success"])
        avg_dur = _avg([r["duration_ms"] for r in results])
        avg_dur_str = f"{avg_dur:.0f}ms" if avg_dur else "N/A"
        lines.append(f"### {name} ({agent_type})")
        lines.append("")
        lines.append(f"- Success: {successes}/{len(results)}")
        lines.append(f"- Avg latency: {avg_dur_str}")
        lines.append("")

        lines.append("| # | Prompt | Status | Latency |")
        lines.append("|---|--------|--------|---------|")
        for i, r in enumerate(results, 1):
            short_prompt = PROMPTS[i - 1][:55] + "..." if len(PROMPTS[i - 1]) > 55 else PROMPTS[i - 1]
            status = "✓" if r["success"] else "✗"
            lines.append(f"| {i} | {short_prompt} | {status} | {r['duration_ms']:.0f}ms |")
        lines.append("")

    # Phase 2: Evaluation results
    lines += [
        "---",
        "",
        "## Phase 2: Evaluation Results (Ground-Truth Comparison)",
        "",
        "**Baseline:** Sonnet (in-memory invocation, HelpfulnessEvaluator)",
        "**Contenders:** All 6 agents evaluated with CorrectnessEvaluator vs Sonnet baseline",
        "",
        "### Summary",
        "",
        "| Agent | Type | Avg Correctness | Avg Helpfulness |",
        "|-------|------|----------------|-----------------|",
    ]

    baseline_h = eval_results.get("_baseline", {}).get("helpfulness", [])
    avg_bh = _avg(baseline_h)
    bh_str = f"{avg_bh:.3f}" if avg_bh is not None else "N/A"
    lines.append(f"| sonnet (baseline) | — | — | {bh_str} |")

    for agent_data in all_agent_results:
        name = agent_data["name"]
        if name not in eval_results:
            continue
        scores = eval_results[name]
        avg_c = _avg(scores.get("correctness", []))
        avg_h = _avg(scores.get("helpfulness", []))
        c_str = f"{avg_c:.3f}" if avg_c is not None else "N/A"
        h_str = f"{avg_h:.3f}" if avg_h is not None else "N/A"
        lines.append(f"| {name} | {agent_data['type']} | {c_str} | {h_str} |")

    # Per-prompt correctness
    lines += [
        "",
        "### Per-Prompt Correctness (vs Sonnet Baseline)",
        "",
        "| # | Prompt | " + " | ".join(a["name"][:20] for a in all_agent_results) + " |",
        "|---|--------| " + " | ".join("---" for _ in all_agent_results) + " |",
    ]

    for i, prompt in enumerate(PROMPTS):
        short = prompt[:45] + "..." if len(prompt) > 45 else prompt
        scores = []
        for agent_data in all_agent_results:
            name = agent_data["name"]
            if name in eval_results:
                c = eval_results[name]["correctness"][i]
                scores.append("✓" if c == 1.0 else "✗" if c == 0.0 else str(c) if c is not None else "—")
            else:
                scores.append("—")
        lines.append(f"| {i+1} | {short} | " + " | ".join(scores) + " |")

    # Stale agents
    lines += [
        "",
        "---",
        "",
        "## Stale Agent Detection",
        "",
    ]

    if stale:
        lines.append(f"**{len(stale)} agents** have not been evaluated in the last 7 days:")
        lines.append("")
        for agent in stale:
            ts = agent.get("last_eval_timestamp", "never")
            lines.append(f"- `{agent['name']}` — last eval: {ts}")
    else:
        lines.append("All agents have been evaluated within the last 7 days. ✅")

    lines += [
        "",
        "---",
        "",
        "## How to Run",
        "",
        "```bash",
        "AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \\",
        "  .venv/bin/python scripts/run_registry_comparison.py",
        "```",
        "",
        "This script:",
        "1. Loads all 6 agents from registry.json",
        "2. Runs all 6 concurrently (ThreadPoolExecutor, max_workers=6)",
        "3. Each agent runs 5 prompts sequentially (avoids per-model throttling)",
        "4. Phase 2: In-memory evaluation (ground-truth vs contender)",
        "5. Updates registry.json with latest scores",
        "6. Generates this report",
    ]

    return "\n".join(lines)


def _avg(scores: list) -> float | None:
    """Average of non-None scores."""
    valid = [s for s in scores if s is not None]
    return sum(valid) / len(valid) if valid else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print("AGENT REGISTRY COMPARISON — 6 Agents × 5 Prompts = 30 Invocations")
    print("=" * 70)
    print(f"  Managed: {', '.join(a['name'] for a in MANAGED_AGENTS)}")
    print(f"  BYO:     {', '.join(a['name'] for a in BYO_AGENTS)}")
    print(f"  Prompts: {len(PROMPTS)}")
    print(f"  Execution: ThreadPoolExecutor(max_workers=6)")
    print()

    # List agents from AWS Agent Registry
    print("[Registry] Reading agents from AWS Agent Registry...")
    try:
        session = boto3.Session(
            profile_name=os.environ.get("AWS_PROFILE", "ml-sandbox"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        reg_client = session.client("bedrock-agentcore-control")
        resp = reg_client.list_registry_records(registryId=AWS_REGISTRY_ID)
        records = resp.get("registryRecords", resp.get("registryRecordSummaries", []))
        print(f"  Found {len(records)} records in registry {AWS_REGISTRY_ID}")
        for rec in records:
            name = rec.get("name", rec.get("recordId", "?"))
            status = rec.get("status", "?")
            print(f"    - {name} ({status})")
    except Exception as e:
        print(f"  [WARN] Could not list registry records: {e}")
    print()

    # Phase 1: Run all 6 agents concurrently
    print("[Phase 1] Running all 6 agents concurrently...")
    start_time = time.time()
    all_results = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}
        for agent_info in MANAGED_AGENTS:
            f = executor.submit(run_managed_agent, agent_info)
            futures[f] = agent_info["name"]
        for agent_info in BYO_AGENTS:
            f = executor.submit(run_byo_agent, agent_info)
            futures[f] = agent_info["name"]

        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                all_results.append(result)
                successes = sum(1 for r in result["results"] if r["success"])
                print(f"  ✅ {name}: {successes}/5 successful")
            except Exception as e:
                print(f"  ❌ {name}: FAILED — {e}")
                all_results.append({
                    "name": name,
                    "type": "unknown",
                    "model_key": "unknown",
                    "results": [{"response": f"ERROR: {e}", "duration_ms": 0, "success": False}] * 5,
                })

    phase1_duration = time.time() - start_time
    total_success = sum(1 for a in all_results for r in a["results"] if r["success"])
    print(f"\n  Phase 1 complete: {total_success}/30 invocations successful ({phase1_duration:.1f}s)")

    # Phase 2: In-memory evaluation
    print("\n[Phase 2] Running in-memory evaluation (ground-truth comparison)...")
    eval_start = time.time()
    eval_results = run_evaluation(all_results)
    eval_duration = time.time() - eval_start
    print(f"\n  Phase 2 complete ({eval_duration:.1f}s)")

    # Phase 3: Update registry + generate report
    print("\n[Phase 3] Updating registry and generating report...")
    update_registry_with_scores(eval_results)

    # Update AWS Agent Registry records
    print("\n  Updating AWS Agent Registry records...")
    update_aws_registry_records(eval_results)

    # Sort results for consistent report ordering
    order = [a["name"] for a in MANAGED_AGENTS] + [a["name"] for a in BYO_AGENTS]
    all_results.sort(key=lambda x: order.index(x["name"]) if x["name"] in order else 99)

    report = generate_report(all_results, eval_results)
    out_path = PROJECT_ROOT / "results" / "registry_comparison.md"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(report)

    total_duration = time.time() - start_time
    print(f"\n{'=' * 70}")
    print("COMPLETE")
    print(f"{'=' * 70}")
    print(f"  Total time: {total_duration:.1f}s")
    print(f"  Report: results/registry_comparison.md")
    print(f"  Registry: registry/registry.json (updated with scores)")

    # Print summary
    print(f"\n  Evaluation Summary:")
    baseline_h = eval_results.get("_baseline", {}).get("helpfulness", [])
    avg_bh = _avg(baseline_h)
    if avg_bh is not None:
        print(f"    Baseline (sonnet): helpfulness={avg_bh:.3f}")
    for agent_data in all_results:
        name = agent_data["name"]
        if name in eval_results:
            scores = eval_results[name]
            avg_c = _avg(scores.get("correctness", []))
            avg_h = _avg(scores.get("helpfulness", []))
            c_str = f"{avg_c:.3f}" if avg_c is not None else "N/A"
            h_str = f"{avg_h:.3f}" if avg_h is not None else "N/A"
            print(f"    {name}: correctness={c_str}, helpfulness={h_str}")


if __name__ == "__main__":
    main()
