"""
BYO Model Comparison — Local SDK Evaluation

Demonstrates the BYO path: same agent code, evaluated locally using
strands-agents-evals SDK evaluators. No AgentCore Runtime needed.

Usage:
    python scripts/run_byo_comparison.py --models sonnet,haiku,nova_pro
"""

import argparse
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
from strands_evals.evaluators import HelpfulnessEvaluator
from strands_evals.types.evaluation import EvaluationData

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


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def run_comparison(model_keys: list[str]) -> dict[str, list[dict]]:
    """Run all prompts against each model with telemetry capture and evaluation.

    Args:
        model_keys: List of model keys to evaluate.

    Returns:
        Dict of model_key -> list of result dicts with prompt, score, duration.
    """
    # Setup telemetry BEFORE creating any agents
    telemetry = StrandsEvalsTelemetry()
    telemetry.setup_in_memory_exporter()
    mapper = StrandsInMemorySessionMapper()
    evaluator = HelpfulnessEvaluator()

    all_results = {}

    for model_key in model_keys:
        print(f"\n{'='*60}")
        print(f"Model: {model_key} ({SUPPORTED_MODELS[model_key]})")
        print(f"{'='*60}")

        agent = create_agent(model_key)
        model_results = []

        for i, prompt in enumerate(PROMPTS, 1):
            print(f"\n  Prompt {i}/{len(PROMPTS)}: {prompt[:70]}...")

            # Clear spans before each invocation
            try:
                telemetry.in_memory_exporter.clear()
            except Exception:
                pass

            start_time = time.time()
            score = None
            response_text = ""

            try:
                # Invoke agent
                response = agent(prompt)
                duration_ms = (time.time() - start_time) * 1000
                response_text = str(response)

                # Capture spans and map to session
                spans = list(telemetry.in_memory_exporter.get_finished_spans())
                if spans:
                    session_id = f"byo-{model_key}-prompt-{i}"
                    session = mapper.map_to_session(spans, session_id=session_id)

                    # Evaluate with HelpfulnessEvaluator
                    eval_data = EvaluationData(
                        input=prompt,
                        actual_trajectory=session,
                    )
                    eval_results = evaluator.evaluate(eval_data)
                    if eval_results:
                        score = eval_results[0].score
                else:
                    print(f"    [WARN] No spans captured")

                # Clear for next invocation
                telemetry.in_memory_exporter.clear()

                print(f"    ✓ Score: {score} | Duration: {duration_ms:.0f}ms")

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                print(f"    ✗ Failed: {e}")
                # Clear exporter on failure
                try:
                    telemetry.in_memory_exporter.clear()
                except Exception:
                    pass

            model_results.append({
                "prompt": prompt,
                "prompt_idx": i,
                "score": score,
                "duration_ms": round(duration_ms, 1),
                "response": response_text[:200],
            })

        all_results[model_key] = model_results

    return all_results


def generate_report(all_results: dict[str, list[dict]]) -> str:
    """Generate markdown comparison report.

    Args:
        all_results: Dict of model_key -> list of result dicts.

    Returns:
        Markdown string with the full report.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# BYO Agent — End-to-End Model Comparison with Local SDK Evaluation",
        "",
        f"**Generated:** {timestamp}",
        "",
        "## Overview",
        "",
        "This report demonstrates the **BYO (Bring Your Own)** evaluation path:",
        "the same agent code runs locally and is evaluated using `strands-agents-evals`",
        "SDK evaluators — no AgentCore Runtime needed.",
        "",
        "The `agentcore run eval` command only works with managed runtime traces.",
        "This script provides an alternative: capture traces in-memory with",
        "`StrandsEvalsTelemetry`, then evaluate locally with `HelpfulnessEvaluator`.",
        "",
        "## Summary",
        "",
        "| Model | Avg Helpfulness | Prompts Evaluated |",
        "| --- | --- | --- |",
    ]

    # Summary table
    for model_key, results in all_results.items():
        valid_scores = [r["score"] for r in results if r["score"] is not None]
        avg = sum(valid_scores) / len(valid_scores) if valid_scores else None
        avg_str = f"{avg:.3f}" if avg is not None else "N/A"
        evaluated = len(valid_scores)
        lines.append(f"| {model_key} ({SUPPORTED_MODELS[model_key]}) | {avg_str} | {evaluated}/{len(results)} |")

    lines.append("")

    # Per-prompt detail
    lines.append("## Per-Prompt Scores")
    lines.append("")
    header = "| Prompt | " + " | ".join(all_results.keys()) + " |"
    separator = "| --- | " + " | ".join(["---"] * len(all_results)) + " |"
    lines.append(header)
    lines.append(separator)

    for i, prompt in enumerate(PROMPTS):
        short_prompt = prompt[:60] + "..." if len(prompt) > 60 else prompt
        scores = []
        for model_key in all_results:
            result = all_results[model_key][i]
            s = result["score"]
            scores.append(f"{s:.3f}" if s is not None else "N/A")
        lines.append(f"| {short_prompt} | " + " | ".join(scores) + " |")

    lines.append("")

    # Duration comparison
    lines.append("## Response Times (ms)")
    lines.append("")
    header = "| Prompt | " + " | ".join(all_results.keys()) + " |"
    separator = "| --- | " + " | ".join(["---"] * len(all_results)) + " |"
    lines.append(header)
    lines.append(separator)

    for i, prompt in enumerate(PROMPTS):
        short_prompt = f"Prompt {i+1}"
        durations = []
        for model_key in all_results:
            result = all_results[model_key][i]
            durations.append(f"{result['duration_ms']:.0f}")
        lines.append(f"| {short_prompt} | " + " | ".join(durations) + " |")

    lines.append("")

    # How to run
    lines.append("## How to Run")
    lines.append("")
    lines.append("```bash")
    lines.append("AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \\")
    lines.append("  .venv/bin/python scripts/run_byo_comparison.py --models sonnet,haiku,nova_pro")
    lines.append("```")
    lines.append("")

    # Key observations
    lines.append("## Key Observations")
    lines.append("")

    # Calculate averages for observations
    model_avgs = {}
    for model_key, results in all_results.items():
        valid_scores = [r["score"] for r in results if r["score"] is not None]
        model_avgs[model_key] = sum(valid_scores) / len(valid_scores) if valid_scores else 0

    if model_avgs:
        best_model = max(model_avgs, key=model_avgs.get)
        lines.append(f"- **Best performer:** {best_model} with avg helpfulness {model_avgs[best_model]:.3f}")

    lines.append("- **Evaluation method:** Local SDK-based (`strands-agents-evals` HelpfulnessEvaluator)")
    lines.append("- **No AgentCore Runtime required** — proves BYO agents can be fully evaluated locally")
    lines.append("- **Same agent code** used across all models (only the model ID changes)")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="BYO Model Comparison — Local SDK Evaluation"
    )
    parser.add_argument(
        "--models",
        type=str,
        default="sonnet,haiku,nova_pro",
        help="Comma-separated model keys (default: sonnet,haiku,nova_pro)",
    )
    args = parser.parse_args()

    model_keys = [m.strip() for m in args.models.split(",")]

    # Validate model keys
    for key in model_keys:
        if key not in SUPPORTED_MODELS:
            print(f"ERROR: Unknown model key '{key}'. Supported: {list(SUPPORTED_MODELS.keys())}")
            sys.exit(1)

    print("=" * 60)
    print("BYO Agent — Local SDK Evaluation Comparison")
    print(f"Models: {model_keys}")
    print(f"Prompts: {len(PROMPTS)}")
    print(f"Evaluator: HelpfulnessEvaluator")
    print("=" * 60)

    # Run comparison
    all_results = run_comparison(model_keys)

    # Generate and save report
    report = generate_report(all_results)

    results_dir = PROJECT_ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    output_path = results_dir / "byo_comparison.md"
    output_path.write_text(report)

    # Print summary to console
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)

    for model_key, results in all_results.items():
        valid_scores = [r["score"] for r in results if r["score"] is not None]
        avg = sum(valid_scores) / len(valid_scores) if valid_scores else None
        avg_str = f"{avg:.3f}" if avg is not None else "N/A"
        print(f"  {model_key}: avg helpfulness = {avg_str} ({len(valid_scores)}/{len(results)} evaluated)")

    print(f"\nReport saved to: {output_path.relative_to(PROJECT_ROOT)}")
    print("Done!")


if __name__ == "__main__":
    main()
