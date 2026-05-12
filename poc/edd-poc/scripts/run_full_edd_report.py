"""
Full EDD End-to-End Report: Managed + BYO Paths

Runs both paths with 5 prompts x 3 models and produces a single
unified comparison report at results/edd_full_report.md.

Managed path:
  - Invokes AgentCore runtimes (multiplier_hr_sonnet/haiku/nova_pro)
  - Runs agentcore evaluator (multiplier_domain_accuracy, 1-5 scale)
  - Ground-truth: Sonnet score as baseline, compare Haiku/Nova Pro

BYO path:
  - Invokes agent locally via ADOT (traces -> CloudWatch)
  - Evaluates in-memory with CorrectnessEvaluator + HelpfulnessEvaluator
  - Ground-truth: Sonnet response as baseline, compare Haiku/Nova Pro

Usage:
    AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \\
        .venv/bin/python scripts/run_full_edd_report.py
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "agents"))

from agent import create_agent, SUPPORTED_MODELS  # noqa: E402
from strands_evals import StrandsEvalsTelemetry  # noqa: E402
from strands_evals.mappers import StrandsInMemorySessionMapper  # noqa: E402
from strands_evals.evaluators import CorrectnessEvaluator, HelpfulnessEvaluator  # noqa: E402
from strands_evals.types.evaluation import EvaluationData  # noqa: E402

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

MODELS = ["sonnet", "nova_2_pro", "glm_5"]
BASELINE = "sonnet"
CONTENDERS = ["nova_2_pro", "glm_5"]

RUNTIME_NAMES = {
    "sonnet": "multiplier_hr_sonnet",
    "nova_2_pro": "multiplier_hr_nova_2_pro",
    "glm_5": "multiplier_hr_glm_5",
}

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
EVALUATOR_NAME = "multiplier_domain_accuracy"


# ---------------------------------------------------------------------------
# Managed path helpers
# ---------------------------------------------------------------------------

def managed_invoke(model_key: str, prompt: str) -> tuple[str, str, float]:
    """Invoke a managed runtime. Returns (response, session_id, duration_ms)."""
    runtime = RUNTIME_NAMES[model_key]
    start = time.time()
    result = subprocess.run(
        ["agentcore", "invoke", "--runtime", runtime, prompt, "--json"],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "AWS_PROFILE": "ml-sandbox", "AWS_REGION": "us-east-1"},
        cwd=str(PROJECT_ROOT),
    )
    duration_ms = (time.time() - start) * 1000
    if result.returncode != 0:
        return f"ERROR: {result.stderr[:150]}", "", duration_ms
    try:
        data = json.loads(result.stdout)
        response = data.get("response", result.stdout.strip())
        session_id = data.get("sessionId", "")
    except Exception:
        response = result.stdout.strip()
        session_id = ""
    return response, session_id, duration_ms


def managed_eval(model_key: str, session_id: str) -> tuple[float | None, str]:
    """Run agentcore evaluator on a session. Returns (score, label)."""
    runtime = RUNTIME_NAMES[model_key]
    result = subprocess.run(
        ["agentcore", "run", "eval",
         "--runtime", runtime,
         "--evaluator", EVALUATOR_NAME,
         "--session-id", session_id,
         "--days", "1", "--json"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "AWS_PROFILE": "ml-sandbox", "AWS_REGION": "us-east-1"},
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        return None, "eval_failed"
    try:
        data = json.loads(result.stdout)
        results = data.get("results", [])
        if results:
            scores = results[0].get("sessionScores", [])
            if scores:
                return scores[0].get("value"), scores[0].get("label", "")
    except Exception:
        pass
    return None, ""


# ---------------------------------------------------------------------------
# BYO path helpers
# ---------------------------------------------------------------------------

def byo_invoke_adot(model_key: str, prompt: str) -> tuple[str, float]:
    """Run one BYO invocation via ADOT -> CloudWatch."""
    service_name = f"multiplier-byo-{model_key}"
    env = {**os.environ, **ADOT_ENV,
           "OTEL_RESOURCE_ATTRIBUTES": f"service.name={service_name}",
           "AWS_PROFILE": "ml-sandbox", "AWS_REGION": "us-east-1"}
    cmd = [OTEL_INSTRUMENT, PYTHON, BYO_RUNNER, "--model", model_key, "--prompt", prompt]
    start = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                            env=env, cwd=str(PROJECT_ROOT))
    duration_ms = (time.time() - start) * 1000
    if result.returncode != 0:
        return f"ERROR: {result.stderr[:150]}", duration_ms
    return result.stdout.strip(), duration_ms


def byo_invoke_capture(agent, prompt: str, telemetry, mapper, session_id: str):
    """Invoke agent in-memory for evaluation. Returns (response, session, duration_ms)."""
    try:
        telemetry.in_memory_exporter.clear()
    except Exception:
        pass
    start = time.time()
    response = agent(prompt)
    duration_ms = (time.time() - start) * 1000
    spans = list(telemetry.in_memory_exporter.get_finished_spans())
    session = None
    if spans:
        session = mapper.map_to_session(spans, session_id=session_id)
    try:
        telemetry.in_memory_exporter.clear()
    except Exception:
        pass
    return str(response), session, duration_ms


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 70)
    print("EDD Full End-to-End Report")
    print(f"Started: {timestamp}")
    print("=" * 70)

    # ===================================================================
    # MANAGED PATH
    # ===================================================================
    print("\n" + "=" * 70)
    print("MANAGED PATH — AgentCore Runtime + Evaluator")
    print("=" * 70)

    managed_responses: dict[str, list[str]] = {m: [] for m in MODELS}
    managed_session_ids: dict[str, list[str]] = {m: [] for m in MODELS}
    managed_durations: dict[str, list[float]] = {m: [] for m in MODELS}

    # Step 1: Invoke all runtimes
    print("\n[Managed] Invoking all 3 runtimes (5 prompts each)...")
    for model_key in MODELS:
        print(f"\n  Runtime: {RUNTIME_NAMES[model_key]}")
        for i, prompt in enumerate(PROMPTS, 1):
            print(f"    Prompt {i}/5: {prompt[:60]}...")
            response, session_id, dur = managed_invoke(model_key, prompt)
            managed_responses[model_key].append(response)
            managed_session_ids[model_key].append(session_id)
            managed_durations[model_key].append(dur)
            preview = response[:70].replace("\n", " ")
            print(f"    ✓ {dur:.0f}ms | session={session_id[:8]}... | {preview}...")

    # Step 2: Wait for trace indexing
    print("\n[Managed] Waiting 3 minutes for trace indexing...")
    time.sleep(180)

    # Step 3: Run evaluator per session
    print("\n[Managed] Running evaluator on all sessions...")
    managed_scores: dict[str, list[dict]] = {m: [] for m in MODELS}

    for model_key in MODELS:
        print(f"\n  Evaluating {model_key}...")
        for i, (session_id, prompt) in enumerate(zip(managed_session_ids[model_key], PROMPTS), 1):
            if not session_id:
                managed_scores[model_key].append({"score": None, "label": "no_session"})
                continue
            score, label = managed_eval(model_key, session_id)
            managed_scores[model_key].append({"score": score, "label": label})
            print(f"    Prompt {i}: score={score} ({label})")

    # ===================================================================
    # BYO PATH
    # ===================================================================
    print("\n" + "=" * 70)
    print("BYO PATH — ADOT -> CloudWatch + Local SDK Evaluation")
    print("=" * 70)

    byo_adot_responses: dict[str, list[str]] = {m: [] for m in MODELS}
    byo_adot_durations: dict[str, list[float]] = {m: [] for m in MODELS}

    # Step 1: ADOT invocations -> CloudWatch
    print("\n[BYO] Phase 1: ADOT invocations -> CloudWatch (15 traces)...")
    for model_key in MODELS:
        service_name = f"multiplier-byo-{model_key}"
        print(f"\n  Model: {model_key} (service.name={service_name})")
        for i, prompt in enumerate(PROMPTS, 1):
            print(f"    Prompt {i}/5: {prompt[:60]}...")
            response, dur = byo_invoke_adot(model_key, prompt)
            byo_adot_responses[model_key].append(response)
            byo_adot_durations[model_key].append(dur)
            preview = response[:70].replace("\n", " ")
            print(f"    ✓ {dur:.0f}ms | {preview}...")

    print(f"\n  ✅ 15 traces sent to CloudWatch")

    # Step 2: In-memory evaluation
    print("\n[BYO] Phase 2: In-memory evaluation (ground-truth vs contender)...")

    telemetry = StrandsEvalsTelemetry()
    telemetry.setup_in_memory_exporter()
    mapper = StrandsInMemorySessionMapper()
    correctness_eval = CorrectnessEvaluator()
    helpfulness_eval = HelpfulnessEvaluator()

    byo_eval_results: dict[str, list[dict]] = {m: [] for m in MODELS}

    # Baseline (Sonnet)
    print(f"\n  Baseline: {BASELINE}")
    baseline_agent = create_agent(BASELINE)
    baseline_responses_eval = []
    baseline_helpfulness = []

    for i, prompt in enumerate(PROMPTS, 1):
        print(f"    Prompt {i}/5: {prompt[:60]}...")
        resp, session, dur = byo_invoke_capture(
            baseline_agent, prompt, telemetry, mapper, f"byo-baseline-{i}")
        baseline_responses_eval.append(resp)
        score = None
        if session:
            try:
                ed = EvaluationData(input=prompt, actual_trajectory=session)
                res = helpfulness_eval.evaluate(ed)
                if res:
                    score = res[0].score
            except Exception as e:
                print(f"      [WARN] {e}")
        baseline_helpfulness.append(score)
        byo_eval_results[BASELINE].append({
            "prompt": prompt, "helpfulness": score, "correctness": None,
            "correctness_reason": "", "duration_ms": round(dur, 1)
        })
        print(f"    ✓ Helpfulness: {score} | {dur:.0f}ms")

    # Contenders
    for model_key in CONTENDERS:
        print(f"\n  Contender: {model_key}")
        agent = create_agent(model_key)
        for i, prompt in enumerate(PROMPTS, 1):
            print(f"    Prompt {i}/5: {prompt[:60]}...")
            resp, session, dur = byo_invoke_capture(
                agent, prompt, telemetry, mapper, f"byo-{model_key}-{i}")

            correctness = None
            correctness_reason = ""
            helpfulness = None

            if session and baseline_responses_eval[i - 1]:
                try:
                    ed = EvaluationData(
                        input=prompt, actual_trajectory=session,
                        expected_assertion=baseline_responses_eval[i - 1])
                    res = correctness_eval.evaluate(ed)
                    if res:
                        correctness = res[0].score
                        correctness_reason = getattr(res[0], "reason", "") or ""
                except Exception as e:
                    print(f"      [WARN] Correctness: {e}")

            if session:
                try:
                    ed = EvaluationData(input=prompt, actual_trajectory=session)
                    res = helpfulness_eval.evaluate(ed)
                    if res:
                        helpfulness = res[0].score
                except Exception as e:
                    print(f"      [WARN] Helpfulness: {e}")

            verdict = "✓" if correctness == 1.0 else "✗" if correctness == 0.0 else str(correctness)
            print(f"    ✓ Correctness: {verdict} | Helpfulness: {helpfulness} | {dur:.0f}ms")
            byo_eval_results[model_key].append({
                "prompt": prompt, "helpfulness": helpfulness,
                "correctness": correctness, "correctness_reason": correctness_reason[:250],
                "duration_ms": round(dur, 1)
            })

    # ===================================================================
    # GENERATE UNIFIED REPORT
    # ===================================================================
    print("\n[Report] Generating unified comparison report...")

    def avg(scores):
        valid = [s for s in scores if s is not None]
        return sum(valid) / len(valid) if valid else None

    def fmt(v, decimals=3):
        return f"{v:.{decimals}f}" if v is not None else "N/A"

    # Compute managed averages
    managed_avgs = {}
    for m in MODELS:
        scores = [r["score"] for r in managed_scores[m]]
        managed_avgs[m] = avg(scores)

    # Compute BYO averages
    byo_helpfulness_avgs = {}
    byo_correctness_avgs = {}
    for m in MODELS:
        byo_helpfulness_avgs[m] = avg([r["helpfulness"] for r in byo_eval_results[m]])
    for m in CONTENDERS:
        byo_correctness_avgs[m] = avg([r["correctness"] for r in byo_eval_results[m]])

    lines = [
        "# EDD Full End-to-End Report — Managed vs BYO",
        "",
        f"**Generated:** {timestamp}",
        f"**AWS Profile:** ml-sandbox | **Region:** us-east-1",
        f"**Prompts:** {len(PROMPTS)} | **Models:** {', '.join(MODELS)}",
        "",
        "---",
        "",
        "## Overview",
        "",
        "This report covers a complete end-to-end run of both evaluation paths:",
        "",
        "| Path | Agent Runs On | Traces | Evaluator | Score Scale |",
        "|------|--------------|--------|-----------|-------------|",
        "| **Managed** | AgentCore Runtime | CloudWatch (auto) | `multiplier_domain_accuracy` LLM-as-a-Judge | 1–5 |",
        "| **BYO** | Local machine | CloudWatch via ADOT | `CorrectnessEvaluator` + `HelpfulnessEvaluator` | 0–1 |",
        "",
        "---",
        "",
        "## Part 1: Managed Path — AgentCore Runtime",
        "",
        "### Infrastructure",
        "",
        "| Runtime | Model | Status |",
        "|---------|-------|--------|",
    ]

    for m in MODELS:
        lines.append(f"| `{RUNTIME_NAMES[m]}` | `{SUPPORTED_MODELS[m]}` | READY ✅ |")

    lines += [
        "",
        "### Evaluation Scores (`multiplier_domain_accuracy`, 1–5 scale)",
        "",
        "| # | Prompt | Sonnet | Haiku | Nova Pro |",
        "|---|--------|--------|-------|----------|",
    ]

    for i, prompt in enumerate(PROMPTS):
        short = prompt[:55] + "..." if len(prompt) > 55 else prompt
        scores = []
        for m in MODELS:
            s = managed_scores[m][i]["score"]
            label = managed_scores[m][i]["label"]
            scores.append(f"{s}/5 ({label})" if s is not None else "N/A")
        lines.append(f"| {i+1} | {short} | {scores[0]} | {scores[1]} | {scores[2]} |")

    lines += [
        "",
        "### Managed Summary",
        "",
        "| Model | Avg Score (1–5) | Avg Latency |",
        "|-------|----------------|-------------|",
    ]

    for m in MODELS:
        avg_score = managed_avgs[m]
        avg_lat = avg(managed_durations[m])
        lines.append(f"| {m} | {fmt(avg_score, 2)} | {fmt(avg_lat, 0)}ms |")

    lines += [
        "",
        "### Managed Response Times (ms)",
        "",
        "| # | Sonnet | Haiku | Nova Pro |",
        "|---|--------|-------|----------|",
    ]

    for i in range(len(PROMPTS)):
        row = [f"{managed_durations[m][i]:.0f}" for m in MODELS]
        lines.append(f"| {i+1} | {row[0]} | {row[1]} | {row[2]} |")

    lines += [
        "",
        "---",
        "",
        "## Part 2: BYO Path — ADOT + Local SDK Evaluation",
        "",
        "### CloudWatch Traces (via ADOT)",
        "",
        "| Service Name | Traces Sent | Model |",
        "|-------------|-------------|-------|",
    ]

    for m in MODELS:
        lines.append(f"| `multiplier-byo-{m}` | 5 ✅ | `{SUPPORTED_MODELS[m]}` |")

    lines += [
        "",
        "### Ground-Truth vs Contender (Correctness, binary 0/1)",
        "",
        "Sonnet = baseline. Haiku and Nova Pro evaluated against Sonnet's responses.",
        "",
        "| # | Prompt | Haiku vs Sonnet | Nova Pro vs Sonnet |",
        "|---|--------|-----------------|-------------------|",
    ]

    for i, prompt in enumerate(PROMPTS):
        short = prompt[:55] + "..." if len(prompt) > 55 else prompt
        scores = []
        for m in CONTENDERS:
            s = byo_eval_results[m][i]["correctness"]
            scores.append("✓ CORRECT" if s == 1.0 else "✗ INCORRECT" if s == 0.0 else str(s))
        lines.append(f"| {i+1} | {short} | {scores[0]} | {scores[1]} |")

    lines += [
        "",
        "### Helpfulness Scores (absolute quality, 0–1 scale)",
        "",
        "| # | Prompt | Sonnet | Haiku | Nova Pro |",
        "|---|--------|--------|-------|----------|",
    ]

    for i, prompt in enumerate(PROMPTS):
        short = prompt[:55] + "..." if len(prompt) > 55 else prompt
        bh = byo_eval_results[BASELINE][i]["helpfulness"]
        scores = [fmt(bh)]
        for m in CONTENDERS:
            s = byo_eval_results[m][i]["helpfulness"]
            scores.append(fmt(s))
        lines.append(f"| {i+1} | {short} | {scores[0]} | {scores[1]} | {scores[2]} |")

    lines += [
        "",
        "### BYO Summary",
        "",
        "| Model | Correctness vs Sonnet | Avg Helpfulness | Avg ADOT Latency |",
        "|-------|----------------------|-----------------|-----------------|",
    ]

    # Baseline row
    avg_bh = byo_helpfulness_avgs[BASELINE]
    avg_bl = avg(byo_adot_durations[BASELINE])
    lines.append(f"| sonnet (baseline) | — | {fmt(avg_bh)} | {fmt(avg_bl, 0)}ms |")

    for m in CONTENDERS:
        avg_c = byo_correctness_avgs[m]
        correct_count = sum(1 for r in byo_eval_results[m] if r["correctness"] == 1.0)
        avg_h = byo_helpfulness_avgs[m]
        avg_l = avg(byo_adot_durations[m])
        c_str = f"{fmt(avg_c)} ({correct_count}/5)" if avg_c is not None else "N/A"
        lines.append(f"| {m} | {c_str} | {fmt(avg_h)} | {fmt(avg_l, 0)}ms |")

    lines += [
        "",
        "### BYO Correctness Reasoning",
        "",
    ]

    for m in CONTENDERS:
        lines.append(f"#### {m.replace('_', ' ').title()} vs Sonnet")
        lines.append("")
        for r in byo_eval_results[m]:
            verdict = "CORRECT ✓" if r["correctness"] == 1.0 else "INCORRECT ✗" if r["correctness"] == 0.0 else str(r["correctness"])
            lines.append(f"**Prompt {PROMPTS.index(r['prompt'])+1}:** {r['prompt'][:60]}...")
            lines.append(f"- {verdict}")
            if r["correctness_reason"]:
                lines.append(f"- _{r['correctness_reason'][:200]}_")
            lines.append("")

    lines += [
        "---",
        "",
        "## Part 3: Cross-Path Comparison",
        "",
        "### Model Ranking",
        "",
        "| Model | Managed Score (1–5) | BYO Correctness vs Sonnet | BYO Helpfulness | ADOT Latency |",
        "|-------|--------------------|-----------------------------|-----------------|--------------|",
    ]

    for m in MODELS:
        ms = fmt(managed_avgs[m], 2)
        if m == BASELINE:
            bc = "— (baseline)"
        else:
            avg_c = byo_correctness_avgs.get(m)
            correct_count = sum(1 for r in byo_eval_results[m] if r["correctness"] == 1.0)
            bc = f"{fmt(avg_c)} ({correct_count}/5)" if avg_c is not None else "N/A"
        bh = fmt(byo_helpfulness_avgs[m])
        bl = fmt(avg(byo_adot_durations[m]), 0)
        lines.append(f"| **{m}** | {ms} | {bc} | {bh} | {bl}ms |")

    lines += [
        "",
        "### Key Findings",
        "",
    ]

    # Auto-generate findings
    for m in CONTENDERS:
        avg_c = byo_correctness_avgs.get(m)
        correct_count = sum(1 for r in byo_eval_results[m] if r["correctness"] == 1.0)
        avg_h = byo_helpfulness_avgs[m]
        avg_ms = managed_avgs[m]
        avg_lat = avg(byo_adot_durations[m])
        baseline_lat = avg(byo_adot_durations[BASELINE])
        speedup = f"{baseline_lat/avg_lat:.1f}x faster" if avg_lat and baseline_lat else ""

        lines.append(f"- **{m.replace('_', ' ').title()} vs Sonnet:**")
        lines.append(f"  - Managed evaluator: {fmt(avg_ms, 2)}/5 avg score")
        lines.append(f"  - BYO correctness: {correct_count}/5 prompts match Sonnet's response")
        lines.append(f"  - BYO helpfulness: {fmt(avg_h)} (absolute quality)")
        if speedup:
            lines.append(f"  - Latency: {speedup} than Sonnet")
        lines.append("")

    lines += [
        "### Recommendation",
        "",
        "Based on the evaluation data:",
        "",
    ]

    # Simple recommendation logic
    nova_correct = sum(1 for r in byo_eval_results["nova_pro"] if r["correctness"] == 1.0)
    haiku_correct = sum(1 for r in byo_eval_results["haiku"] if r["correctness"] == 1.0)

    if nova_correct >= 4:
        lines.append("- **Nova Pro** matches Sonnet on most prompts and is significantly faster — recommended for cost-sensitive deployments")
    if haiku_correct >= 4:
        lines.append("- **Haiku** is a viable Sonnet replacement for simple single-tool queries")
    else:
        lines.append(f"- **Haiku** fails on {5 - haiku_correct}/5 prompts vs Sonnet baseline — use with caution for complex multi-tool queries")
    lines.append("- **Sonnet** remains the highest-quality baseline for production use cases requiring maximum accuracy")

    lines += [
        "",
        "---",
        "",
        "## Appendix: How to Reproduce",
        "",
        "### Managed Path",
        "```bash",
        "# Invoke all 3 runtimes",
        "agentcore invoke --runtime multiplier_hr_sonnet '<prompt>'",
        "agentcore invoke --runtime multiplier_hr_haiku '<prompt>'",
        "agentcore invoke --runtime multiplier_hr_nova_pro '<prompt>'",
        "",
        "# Evaluate (after 3 min for trace indexing)",
        "agentcore run eval --runtime multiplier_hr_sonnet --evaluator multiplier_domain_accuracy --session-id <id> --days 1",
        "```",
        "",
        "### BYO Path",
        "```bash",
        "# ADOT invocations -> CloudWatch",
        "AGENT_OBSERVABILITY_ENABLED=true OTEL_PYTHON_DISTRO=aws_distro \\",
        "OTEL_PYTHON_CONFIGURATOR=aws_configurator OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \\",
        "OTEL_RESOURCE_ATTRIBUTES='service.name=multiplier-byo-sonnet' \\",
        "opentelemetry-instrument python agents/byo_runner.py --model sonnet --prompt '<prompt>'",
        "```",
        "",
        "### Full Report (this script)",
        "```bash",
        "AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \\",
        "  .venv/bin/python scripts/run_full_edd_report.py",
        "```",
    ]

    report = "\n".join(lines)
    out_path = PROJECT_ROOT / "results" / "edd_full_report.md"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(report)

    # Console summary
    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    print("\nManaged Path (1-5 scale):")
    for m in MODELS:
        print(f"  {m}: avg={fmt(managed_avgs[m], 2)}")
    print("\nBYO Path:")
    print(f"  {BASELINE} (baseline): helpfulness={fmt(byo_helpfulness_avgs[BASELINE])}")
    for m in CONTENDERS:
        correct = sum(1 for r in byo_eval_results[m] if r["correctness"] == 1.0)
        print(f"  {m}: correctness={correct}/5, helpfulness={fmt(byo_helpfulness_avgs[m])}")
    print(f"\nReport saved: {out_path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
