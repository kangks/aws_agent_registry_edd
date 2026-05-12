"""
BYO Full End-to-End: 5 prompts × 3 models via ADOT → CloudWatch + Ground-Truth Comparison

Parity with managed path:
- Phase 1: 15 ADOT invocations (5 prompts × 3 models) → traces to CloudWatch
- Phase 2: 15 in-memory invocations → ground-truth comparison with Session objects
  - Sonnet responses = baseline (ground truth)
  - Haiku/Nova Pro evaluated against Sonnet using CorrectnessEvaluator

Usage:
    AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \\
        .venv/bin/python scripts/run_byo_full.py
"""

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

MODELS = ["sonnet", "haiku", "nova_pro"]
BASELINE = "sonnet"
CONTENDERS = ["haiku", "nova_pro"]

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


# ---------------------------------------------------------------------------
# Phase 1: Run via ADOT (traces → CloudWatch)
# ---------------------------------------------------------------------------

def run_via_adot(model_key: str, prompt: str) -> tuple[str, float]:
    """Run one invocation via ADOT. Returns (response_text, duration_ms)."""
    service_name = f"multiplier-byo-{model_key}"
    env = {**os.environ, **ADOT_ENV, "OTEL_RESOURCE_ATTRIBUTES": f"service.name={service_name}"}
    cmd = [OTEL_INSTRUMENT, PYTHON, BYO_RUNNER, "--model", model_key, "--prompt", prompt]
    start = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env, cwd=str(PROJECT_ROOT))
    duration_ms = (time.time() - start) * 1000
    if result.returncode != 0:
        print(f"    [WARN] ADOT failed: {result.stderr[:150]}")
        return f"ERROR: {result.stderr[:150]}", duration_ms
    return result.stdout.strip(), duration_ms


# ---------------------------------------------------------------------------
# Phase 2: In-memory invocation + evaluation
# ---------------------------------------------------------------------------

def invoke_and_capture(agent, prompt: str, telemetry, mapper, session_id: str):
    """Invoke agent, capture spans, return (response_text, session, duration_ms)."""
    try:
        telemetry.in_memory_exporter.clear()
    except Exception:
        pass
    start = time.time()
    response = agent(prompt)
    duration_ms = (time.time() - start) * 1000
    response_text = str(response)
    spans = list(telemetry.in_memory_exporter.get_finished_spans())
    session = None
    if spans:
        session = mapper.map_to_session(spans, session_id=session_id)
    try:
        telemetry.in_memory_exporter.clear()
    except Exception:
        pass
    return response_text, session, duration_ms


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print("BYO Full End-to-End: 5 prompts × 3 models")
    print("Phase 1: ADOT invocations → CloudWatch (15 traces)")
    print("Phase 2: In-memory invocations → ground-truth evaluation")
    print("=" * 70)

    # -----------------------------------------------------------------------
    # Phase 1: ADOT → CloudWatch (15 traces)
    # -----------------------------------------------------------------------
    print("\n[Phase 1] Running all invocations via ADOT (traces → CloudWatch)...")
    adot_responses: dict[str, list[str]] = {m: [] for m in MODELS}
    adot_durations: dict[str, list[float]] = {m: [] for m in MODELS}

    for model_key in MODELS:
        service_name = f"multiplier-byo-{model_key}"
        print(f"\n  Model: {model_key} (service.name={service_name})")
        for i, prompt in enumerate(PROMPTS, 1):
            print(f"    Prompt {i}/5: {prompt[:60]}...")
            response, dur = run_via_adot(model_key, prompt)
            adot_responses[model_key].append(response)
            adot_durations[model_key].append(dur)
            preview = response[:80].replace("\n", " ")
            print(f"    ✓ {dur:.0f}ms | {preview}...")

    print(f"\n  ✅ {len(MODELS) * len(PROMPTS)} traces sent to CloudWatch")
    print("  Service names: multiplier-byo-sonnet, multiplier-byo-haiku, multiplier-byo-nova-pro")

    # -----------------------------------------------------------------------
    # Phase 2: In-memory evaluation (ground-truth vs contender)
    # -----------------------------------------------------------------------
    print("\n[Phase 2] Running in-memory evaluation (ground-truth vs contender)...")

    telemetry = StrandsEvalsTelemetry()
    telemetry.setup_in_memory_exporter()
    mapper = StrandsInMemorySessionMapper()
    correctness_eval = CorrectnessEvaluator()
    helpfulness_eval = HelpfulnessEvaluator()

    # Run baseline (Sonnet) in-memory for session capture
    print(f"\n  Baseline: {BASELINE}")
    baseline_agent = create_agent(BASELINE)
    baseline_responses_eval = []  # responses from in-memory run (for correctness comparison)
    baseline_sessions = []
    baseline_helpfulness = []
    baseline_durations_eval = []

    for i, prompt in enumerate(PROMPTS, 1):
        print(f"    Prompt {i}/5: {prompt[:60]}...")
        resp, session, dur = invoke_and_capture(
            baseline_agent, prompt, telemetry, mapper,
            session_id=f"byo-eval-baseline-{i}"
        )
        baseline_responses_eval.append(resp)
        baseline_sessions.append(session)
        baseline_durations_eval.append(dur)

        score = None
        if session:
            try:
                ed = EvaluationData(input=prompt, actual_trajectory=session)
                res = helpfulness_eval.evaluate(ed)
                if res:
                    score = res[0].score
            except Exception as e:
                print(f"      [WARN] Helpfulness: {e}")
        baseline_helpfulness.append(score)
        print(f"    ✓ Helpfulness: {score} | {dur:.0f}ms")

    # Run contenders in-memory and evaluate against baseline
    contender_results: dict[str, list[dict]] = {}

    for model_key in CONTENDERS:
        print(f"\n  Contender: {model_key}")
        agent = create_agent(model_key)
        results = []

        for i, prompt in enumerate(PROMPTS, 1):
            print(f"    Prompt {i}/5: {prompt[:60]}...")
            resp, session, dur = invoke_and_capture(
                agent, prompt, telemetry, mapper,
                session_id=f"byo-eval-{model_key}-{i}"
            )

            correctness = None
            correctness_reason = ""
            helpfulness = None

            if session and baseline_responses_eval[i - 1]:
                try:
                    ed = EvaluationData(
                        input=prompt,
                        actual_trajectory=session,
                        expected_assertion=baseline_responses_eval[i - 1],
                    )
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

            verdict = "CORRECT ✓" if correctness == 1.0 else "INCORRECT ✗" if correctness == 0.0 else str(correctness)
            print(f"    ✓ Correctness: {verdict} | Helpfulness: {helpfulness} | {dur:.0f}ms")

            results.append({
                "prompt": prompt,
                "prompt_idx": i,
                "correctness": correctness,
                "correctness_reason": correctness_reason[:300],
                "helpfulness": helpfulness,
                "adot_duration_ms": adot_durations[model_key][i - 1],
                "eval_duration_ms": round(dur, 1),
            })

        contender_results[model_key] = results

    # -----------------------------------------------------------------------
    # Generate report
    # -----------------------------------------------------------------------
    def avg(scores):
        valid = [s for s in scores if s is not None]
        return sum(valid) / len(valid) if valid else None

    avg_baseline_h = avg(baseline_helpfulness)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# BYO Full End-to-End — Ground Truth vs Contender",
        "",
        f"**Generated:** {timestamp}",
        f"**Baseline:** Claude Sonnet 4 (`{SUPPORTED_MODELS[BASELINE]}`)",
        "**Contenders:** Claude Haiku 4.5, Amazon Nova Pro",
        f"**Prompts:** {len(PROMPTS)} (same as managed path)",
        f"**CloudWatch traces:** {len(MODELS) * len(PROMPTS)} (5 per model × 3 models)",
        "",
        "## Parity with Managed Path",
        "",
        "| Aspect | Managed Path | BYO Path |",
        "|--------|-------------|----------|",
        "| Prompts per model | 5 | 5 ✅ |",
        "| Traces to CloudWatch | ✅ (runtime sidecar) | ✅ (ADOT) |",
        "| Service names | `eddpoc_multiplier_hr_*.DEFAULT` | `multiplier-byo-*` |",
        "| Evaluator | `multiplier_domain_accuracy` (1-5) | `CorrectnessEvaluator` (binary) |",
        "| Ground truth | `--expected-response` CLI flag | `expected_assertion` field |",
        "",
        "## Summary",
        "",
        "| Comparison | Avg Correctness vs Baseline | Avg Helpfulness |",
        "|------------|---------------------------|-----------------|",
    ]

    bh_str = f"{avg_baseline_h:.3f}" if avg_baseline_h is not None else "N/A"
    lines.append(f"| Sonnet (baseline) | — | {bh_str} |")

    for model_key in CONTENDERS:
        results = contender_results[model_key]
        avg_c = avg([r["correctness"] for r in results])
        avg_h = avg([r["helpfulness"] for r in results])
        correct_count = sum(1 for r in results if r["correctness"] == 1.0)
        c_str = f"{avg_c:.3f} ({correct_count}/{len(results)} correct)" if avg_c is not None else "N/A"
        h_str = f"{avg_h:.3f}" if avg_h is not None else "N/A"
        lines.append(f"| {model_key.replace('_', ' ').title()} vs Sonnet | {c_str} | {h_str} |")

    lines += [
        "",
        "## Per-Prompt Correctness (vs Sonnet Baseline)",
        "",
        "| # | Prompt | Haiku | Nova Pro |",
        "|---|--------|-------|----------|",
    ]

    for i, prompt in enumerate(PROMPTS):
        short = prompt[:55] + "..." if len(prompt) > 55 else prompt
        scores = []
        for model_key in CONTENDERS:
            s = contender_results[model_key][i]["correctness"]
            scores.append("✓" if s == 1.0 else "✗" if s == 0.0 else str(s))
        lines.append(f"| {i+1} | {short} | {scores[0]} | {scores[1]} |")

    lines += [
        "",
        "## Per-Prompt Helpfulness (Absolute Quality)",
        "",
        "| # | Prompt | Sonnet | Haiku | Nova Pro |",
        "|---|--------|--------|-------|----------|",
    ]

    for i, prompt in enumerate(PROMPTS):
        short = prompt[:55] + "..." if len(prompt) > 55 else prompt
        bh = baseline_helpfulness[i]
        bh_s = f"{bh:.3f}" if bh is not None else "N/A"
        scores = [bh_s]
        for model_key in CONTENDERS:
            s = contender_results[model_key][i]["helpfulness"]
            scores.append(f"{s:.3f}" if s is not None else "N/A")
        lines.append(f"| {i+1} | {short} | {scores[0]} | {scores[1]} | {scores[2]} |")

    lines += [
        "",
        "## Response Times (ms) — ADOT Invocations (CloudWatch traces)",
        "",
        "| # | Sonnet | Haiku | Nova Pro |",
        "|---|--------|-------|----------|",
    ]

    for i in range(len(PROMPTS)):
        bd = f"{adot_durations[BASELINE][i]:.0f}"
        contender_ds = [f"{contender_results[m][i]['adot_duration_ms']:.0f}" for m in CONTENDERS]
        lines.append(f"| {i+1} | {bd} | {contender_ds[0]} | {contender_ds[1]} |")

    lines += [
        "",
        "## Correctness Reasoning",
        "",
    ]

    for model_key in CONTENDERS:
        lines.append(f"### {model_key.replace('_', ' ').title()} vs Sonnet")
        lines.append("")
        for r in contender_results[model_key]:
            verdict = "CORRECT ✓" if r["correctness"] == 1.0 else "INCORRECT ✗" if r["correctness"] == 0.0 else str(r["correctness"])
            lines.append(f"**Prompt {r['prompt_idx']}:** {r['prompt'][:60]}...")
            lines.append(f"- Verdict: {verdict}")
            if r["correctness_reason"]:
                lines.append(f"- Reason: {r['correctness_reason'][:250]}")
            lines.append("")

    lines += [
        "## Key Observations",
        "",
    ]

    for model_key in CONTENDERS:
        results = contender_results[model_key]
        correct = sum(1 for r in results if r["correctness"] == 1.0)
        lines.append(f"- **{model_key.replace('_', ' ').title()}:** {correct}/{len(results)} prompts CORRECT vs Sonnet baseline")

    lines += [
        "",
        "## How to Run",
        "",
        "```bash",
        "AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \\",
        "  .venv/bin/python scripts/run_byo_full.py",
        "```",
        "",
        "Phase 1 sends 15 traces to CloudWatch via ADOT.",
        "Phase 2 runs 15 in-memory invocations for evaluation (no CloudWatch).",
    ]

    report = "\n".join(lines)
    out_path = PROJECT_ROOT / "results" / "byo_full_comparison.md"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(report)

    # Print summary
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)
    print(f"  Baseline (sonnet): avg helpfulness = {avg_baseline_h:.3f}" if avg_baseline_h else "  Baseline: N/A")
    for model_key in CONTENDERS:
        results = contender_results[model_key]
        avg_c = avg([r["correctness"] for r in results])
        avg_h = avg([r["helpfulness"] for r in results])
        correct = sum(1 for r in results if r["correctness"] == 1.0)
        c_str = f"{avg_c:.3f} ({correct}/5 correct)" if avg_c is not None else "N/A"
        h_str = f"{avg_h:.3f}" if avg_h is not None else "N/A"
        print(f"  {model_key}: correctness={c_str}, helpfulness={h_str}")

    print(f"\nReport: {out_path.relative_to(PROJECT_ROOT)}")
    print(f"CloudWatch: 15 traces (multiplier-byo-sonnet/haiku/nova_pro, 5 each)")


if __name__ == "__main__":
    main()
