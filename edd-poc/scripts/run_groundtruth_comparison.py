"""
Ground Truth vs Contender Model Comparison

Runs Sonnet as the baseline (ground truth), then evaluates Haiku and Nova Pro
against Sonnet's responses using CorrectnessEvaluator.

This answers the question: "Does the contender produce responses as good as the baseline?"

Usage:
    python scripts/run_groundtruth_comparison.py
"""

import sys
import time
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "agents"))

from agent import create_agent, SUPPORTED_MODELS  # noqa: E402

# ---------------------------------------------------------------------------
# Trace capture and evaluation imports
# ---------------------------------------------------------------------------
from strands_evals import StrandsEvalsTelemetry
from strands_evals.mappers import StrandsInMemorySessionMapper
from strands_evals.evaluators import CorrectnessEvaluator, HelpfulnessEvaluator
from strands_evals.types.evaluation import EvaluationData

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASELINE_MODEL = "sonnet"
CONTENDER_MODELS = ["haiku", "nova_pro"]

PROMPTS = [
    "What is the employment status of employee EMP-12345 in Singapore?",
    "What are the notice periods and regulatory requirements for employees in Thailand?",
    "Calculate the monthly payroll breakdown for an employee earning 90000 SGD annually in Singapore.",
    "Check the leave balance for employee EMP-67890. How many days do they have remaining?",
    "For employee EMP-11111 in India, look up their details, check compliance rules, and calculate their payroll breakdown in INR.",
]


# ---------------------------------------------------------------------------
# Helper: invoke agent and capture response + session
# ---------------------------------------------------------------------------

def invoke_and_capture(agent, prompt, telemetry, mapper, session_id):
    """Invoke agent, capture spans, return (response_text, session, duration_ms)."""
    try:
        telemetry.in_memory_exporter.clear()
    except Exception:
        pass

    start_time = time.time()
    response = agent(prompt)
    duration_ms = (time.time() - start_time) * 1000
    response_text = str(response)

    # Capture spans and map to session
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
# Main logic
# ---------------------------------------------------------------------------

def run_groundtruth_comparison():
    """Run ground-truth vs contender comparison.

    1. Run baseline (Sonnet) against all prompts
    2. Run each contender against same prompts
    3. Evaluate contenders against baseline using CorrectnessEvaluator
    4. Also evaluate helpfulness for absolute quality comparison
    """
    # Setup telemetry
    telemetry = StrandsEvalsTelemetry()
    telemetry.setup_in_memory_exporter()
    mapper = StrandsInMemorySessionMapper()

    correctness_evaluator = CorrectnessEvaluator()
    helpfulness_evaluator = HelpfulnessEvaluator()

    # -----------------------------------------------------------------------
    # Step 1: Run baseline (Sonnet) against all prompts
    # -----------------------------------------------------------------------
    print("=" * 60)
    print(f"BASELINE: {BASELINE_MODEL} ({SUPPORTED_MODELS[BASELINE_MODEL]})")
    print("=" * 60)

    baseline_agent = create_agent(BASELINE_MODEL)
    baseline_responses = []
    baseline_sessions = []
    baseline_durations = []
    baseline_helpfulness_scores = []

    for i, prompt in enumerate(PROMPTS, 1):
        print(f"\n  Prompt {i}/{len(PROMPTS)}: {prompt[:70]}...")

        response_text, session, duration_ms = invoke_and_capture(
            baseline_agent, prompt, telemetry, mapper,
            session_id=f"baseline-{BASELINE_MODEL}-prompt-{i}"
        )

        baseline_responses.append(response_text)
        baseline_sessions.append(session)
        baseline_durations.append(duration_ms)

        # Evaluate helpfulness for baseline
        helpfulness_score = None
        if session:
            try:
                eval_data = EvaluationData(
                    input=prompt,
                    actual_trajectory=session,
                )
                eval_results = helpfulness_evaluator.evaluate(eval_data)
                if eval_results:
                    helpfulness_score = eval_results[0].score
            except Exception as e:
                print(f"    [WARN] Helpfulness eval failed: {e}")

        baseline_helpfulness_scores.append(helpfulness_score)
        print(f"    ✓ Helpfulness: {helpfulness_score} | Duration: {duration_ms:.0f}ms")
        print(f"    Response preview: {response_text[:100]}...")

    # -----------------------------------------------------------------------
    # Step 2: Run contenders and evaluate against baseline
    # -----------------------------------------------------------------------
    contender_results = {}

    for model_key in CONTENDER_MODELS:
        print(f"\n{'=' * 60}")
        print(f"CONTENDER: {model_key} ({SUPPORTED_MODELS[model_key]})")
        print(f"{'=' * 60}")

        agent = create_agent(model_key)
        model_results = []

        for i, prompt in enumerate(PROMPTS, 1):
            print(f"\n  Prompt {i}/{len(PROMPTS)}: {prompt[:70]}...")

            response_text, session, duration_ms = invoke_and_capture(
                agent, prompt, telemetry, mapper,
                session_id=f"contender-{model_key}-prompt-{i}"
            )

            # Evaluate correctness vs baseline using expected_assertion
            correctness_score = None
            correctness_reason = ""
            if session and baseline_responses[i - 1]:
                try:
                    eval_data = EvaluationData(
                        input=prompt,
                        actual_trajectory=session,
                        expected_assertion=baseline_responses[i - 1],
                    )
                    eval_results = correctness_evaluator.evaluate(eval_data)
                    if eval_results:
                        correctness_score = eval_results[0].score
                        correctness_reason = eval_results[0].reason or ""
                except Exception as e:
                    print(f"    [WARN] Correctness eval failed: {e}")

            # Evaluate helpfulness (absolute quality)
            helpfulness_score = None
            if session:
                try:
                    eval_data = EvaluationData(
                        input=prompt,
                        actual_trajectory=session,
                    )
                    eval_results = helpfulness_evaluator.evaluate(eval_data)
                    if eval_results:
                        helpfulness_score = eval_results[0].score
                except Exception as e:
                    print(f"    [WARN] Helpfulness eval failed: {e}")

            print(f"    ✓ Correctness vs baseline: {correctness_score} | Helpfulness: {helpfulness_score} | Duration: {duration_ms:.0f}ms")
            if correctness_reason:
                print(f"    Reason: {correctness_reason[:120]}...")

            model_results.append({
                "prompt": prompt,
                "prompt_idx": i,
                "correctness_score": correctness_score,
                "correctness_reason": correctness_reason,
                "helpfulness_score": helpfulness_score,
                "duration_ms": round(duration_ms, 1),
                "response": response_text[:200],
            })

        contender_results[model_key] = model_results

    return baseline_helpfulness_scores, baseline_durations, contender_results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(baseline_helpfulness_scores, baseline_durations, contender_results):
    """Generate markdown report comparing contenders against baseline."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Calculate averages
    valid_baseline_helpfulness = [s for s in baseline_helpfulness_scores if s is not None]
    avg_baseline_helpfulness = (
        sum(valid_baseline_helpfulness) / len(valid_baseline_helpfulness)
        if valid_baseline_helpfulness else None
    )

    lines = [
        "# Ground Truth vs Contender — Model Comparison",
        "",
        f"**Generated:** {timestamp}",
        "",
        f"**Baseline (Ground Truth):** Claude Sonnet 4 ({SUPPORTED_MODELS[BASELINE_MODEL]})",
        f"**Contenders:** Claude Haiku 4.5, Amazon Nova Pro",
        "",
        "## Methodology",
        "",
        "Instead of scoring each model independently, this comparison:",
        "1. Runs Sonnet (the \"ground truth\" / baseline) against all prompts",
        "2. Runs each contender (Haiku, Nova Pro) against the same prompts",
        "3. Evaluates each contender's response AGAINST Sonnet's response using `CorrectnessEvaluator`",
        "4. This answers: \"Does the contender produce responses as good as the baseline?\"",
        "",
        "The `CorrectnessEvaluator` with `expected_assertion` compares the contender's response",
        "against the baseline's response and returns CORRECT (1.0) or INCORRECT (0.0).",
        "",
        "`HelpfulnessEvaluator` provides an absolute quality score (0.0–1.0) for each model independently.",
        "",
        "## Summary",
        "",
        "| Comparison | Avg Correctness vs Baseline | Avg Helpfulness |",
        "|------------|---------------------------|-----------------|",
    ]

    # Baseline row
    avg_baseline_str = f"{avg_baseline_helpfulness:.3f}" if avg_baseline_helpfulness is not None else "N/A"
    lines.append(f"| Sonnet (baseline) | — | {avg_baseline_str} |")

    # Contender rows
    for model_key, results in contender_results.items():
        valid_correctness = [r["correctness_score"] for r in results if r["correctness_score"] is not None]
        valid_helpfulness = [r["helpfulness_score"] for r in results if r["helpfulness_score"] is not None]

        avg_correctness = sum(valid_correctness) / len(valid_correctness) if valid_correctness else None
        avg_helpfulness = sum(valid_helpfulness) / len(valid_helpfulness) if valid_helpfulness else None

        correctness_str = f"{avg_correctness:.3f}" if avg_correctness is not None else "N/A"
        helpfulness_str = f"{avg_helpfulness:.3f}" if avg_helpfulness is not None else "N/A"

        display_name = f"{model_key.replace('_', ' ').title()} vs Sonnet"
        lines.append(f"| {display_name} | {correctness_str} | {helpfulness_str} |")

    lines.append("")

    # Per-prompt correctness table
    lines.append("## Per-Prompt Correctness (vs Sonnet Baseline)")
    lines.append("")
    lines.append("| Prompt | " + " | ".join([k.replace("_", " ").title() for k in contender_results.keys()]) + " |")
    lines.append("| --- | " + " | ".join(["---"] * len(contender_results)) + " |")

    for i, prompt in enumerate(PROMPTS):
        short_prompt = prompt[:60] + "..." if len(prompt) > 60 else prompt
        scores = []
        for model_key in contender_results:
            result = contender_results[model_key][i]
            s = result["correctness_score"]
            scores.append(f"{s:.3f}" if s is not None else "N/A")
        lines.append(f"| {short_prompt} | " + " | ".join(scores) + " |")

    lines.append("")

    # Per-prompt helpfulness table
    lines.append("## Per-Prompt Helpfulness (Absolute Quality)")
    lines.append("")
    all_models = [BASELINE_MODEL] + list(contender_results.keys())
    lines.append("| Prompt | " + " | ".join([m.replace("_", " ").title() for m in all_models]) + " |")
    lines.append("| --- | " + " | ".join(["---"] * len(all_models)) + " |")

    for i, prompt in enumerate(PROMPTS):
        short_prompt = prompt[:60] + "..." if len(prompt) > 60 else prompt
        scores = []
        # Baseline helpfulness
        s = baseline_helpfulness_scores[i]
        scores.append(f"{s:.3f}" if s is not None else "N/A")
        # Contender helpfulness
        for model_key in contender_results:
            result = contender_results[model_key][i]
            s = result["helpfulness_score"]
            scores.append(f"{s:.3f}" if s is not None else "N/A")
        lines.append(f"| {short_prompt} | " + " | ".join(scores) + " |")

    lines.append("")

    # Correctness reasoning
    lines.append("## Correctness Evaluation Reasoning")
    lines.append("")
    lines.append("Detailed reasoning from the CorrectnessEvaluator for each contender vs baseline:")
    lines.append("")

    for model_key, results in contender_results.items():
        lines.append(f"### {model_key.replace('_', ' ').title()}")
        lines.append("")
        for result in results:
            short_prompt = result["prompt"][:60] + "..." if len(result["prompt"]) > 60 else result["prompt"]
            score_str = f"{result['correctness_score']:.1f}" if result["correctness_score"] is not None else "N/A"
            verdict = "CORRECT ✓" if result["correctness_score"] == 1.0 else "INCORRECT ✗" if result["correctness_score"] == 0.0 else score_str
            lines.append(f"**Prompt {result['prompt_idx']}:** {short_prompt}")
            lines.append(f"- Verdict: {verdict}")
            if result["correctness_reason"]:
                lines.append(f"- Reasoning: {result['correctness_reason'][:300]}")
            lines.append("")

    # Response times
    lines.append("## Response Times (ms)")
    lines.append("")
    lines.append("| Prompt | Sonnet (baseline) | " + " | ".join([k.replace("_", " ").title() for k in contender_results.keys()]) + " |")
    lines.append("| --- | --- | " + " | ".join(["---"] * len(contender_results)) + " |")

    for i in range(len(PROMPTS)):
        durations = [f"{baseline_durations[i]:.0f}"]
        for model_key in contender_results:
            durations.append(f"{contender_results[model_key][i]['duration_ms']:.0f}")
        lines.append(f"| Prompt {i+1} | " + " | ".join(durations) + " |")

    lines.append("")

    # Key observations
    lines.append("## Key Observations")
    lines.append("")

    for model_key, results in contender_results.items():
        valid_correctness = [r["correctness_score"] for r in results if r["correctness_score"] is not None]
        correct_count = sum(1 for s in valid_correctness if s == 1.0)
        total = len(valid_correctness)
        lines.append(f"- **{model_key.replace('_', ' ').title()}:** {correct_count}/{total} prompts rated CORRECT vs Sonnet baseline")

    lines.append("")
    lines.append("- **Evaluation method:** `CorrectnessEvaluator` with `expected_assertion` set to Sonnet's response")
    lines.append("- **Scoring:** Binary — CORRECT (1.0) or INCORRECT (0.0) relative to baseline")
    lines.append("- **Complementary metric:** `HelpfulnessEvaluator` provides absolute quality (0.0–1.0)")
    lines.append("- **Use case:** Determine if a cheaper/faster model can replace the baseline without quality loss")
    lines.append("")

    # How to run
    lines.append("## How to Run")
    lines.append("")
    lines.append("```bash")
    lines.append("AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \\")
    lines.append("  .venv/bin/python scripts/run_groundtruth_comparison.py")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Ground Truth vs Contender — Model Comparison")
    print(f"Baseline: {BASELINE_MODEL} ({SUPPORTED_MODELS[BASELINE_MODEL]})")
    print(f"Contenders: {CONTENDER_MODELS}")
    print(f"Prompts: {len(PROMPTS)}")
    print(f"Evaluators: CorrectnessEvaluator (vs baseline), HelpfulnessEvaluator (absolute)")
    print("=" * 60)

    # Run comparison
    baseline_helpfulness_scores, baseline_durations, contender_results = run_groundtruth_comparison()

    # Generate and save report
    report = generate_report(baseline_helpfulness_scores, baseline_durations, contender_results)

    results_dir = PROJECT_ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    output_path = results_dir / "groundtruth_comparison.md"
    output_path.write_text(report)

    # Print summary
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)

    valid_baseline = [s for s in baseline_helpfulness_scores if s is not None]
    avg_baseline = sum(valid_baseline) / len(valid_baseline) if valid_baseline else None
    print(f"  Baseline ({BASELINE_MODEL}): avg helpfulness = {avg_baseline:.3f}" if avg_baseline else f"  Baseline ({BASELINE_MODEL}): avg helpfulness = N/A")

    for model_key, results in contender_results.items():
        valid_correctness = [r["correctness_score"] for r in results if r["correctness_score"] is not None]
        valid_helpfulness = [r["helpfulness_score"] for r in results if r["helpfulness_score"] is not None]
        avg_c = sum(valid_correctness) / len(valid_correctness) if valid_correctness else None
        avg_h = sum(valid_helpfulness) / len(valid_helpfulness) if valid_helpfulness else None
        correct_count = sum(1 for s in valid_correctness if s == 1.0)
        c_str = f"{avg_c:.3f} ({correct_count}/{len(valid_correctness)} correct)" if avg_c is not None else "N/A"
        h_str = f"{avg_h:.3f}" if avg_h is not None else "N/A"
        print(f"  {model_key}: correctness vs baseline = {c_str}, helpfulness = {h_str}")

    print(f"\nReport saved to: {output_path.relative_to(PROJECT_ROOT)}")
    print("Done!")


if __name__ == "__main__":
    main()
