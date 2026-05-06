"""
Multi-Turn Evaluation Script for the Multiplier EDD POC.

Evaluates agent performance across multi-turn conversations using
ActorSimulator with HR-specific personas. Compares models on helpfulness,
goal success rate, and conversation efficiency.

Usage:
    python scripts/run_multi_turn.py --models sonnet,haiku,nova_pro --max-turns 5
"""

import argparse
import json
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
# Strands Evals imports
# ---------------------------------------------------------------------------
from strands_evals import ActorSimulator, Case, StrandsEvalsTelemetry
from strands_evals.evaluators import HelpfulnessEvaluator
from strands_evals.mappers import StrandsInMemorySessionMapper
from strands_evals.types.simulation import ActorProfile
from strands_evals.types.evaluation import EvaluationData

# ---------------------------------------------------------------------------
# Actor Profiles — 3 HR-specific personas
# ---------------------------------------------------------------------------

impatient_hr_manager = ActorProfile(
    traits={
        "personality": "impatient and results-driven",
        "communication_style": "direct and brief",
        "expertise_level": "expert",
        "patience_level": "low",
    },
    context="Senior HR manager handling urgent onboarding for a new Singapore office",
    actor_goal="Get complete payroll and compliance info for new Singapore hire",
)

new_employee = ActorProfile(
    traits={
        "personality": "curious and uncertain",
        "communication_style": "polite with many questions",
        "expertise_level": "novice",
        "patience_level": "high",
    },
    context="Recently hired employee trying to understand their benefits and leave",
    actor_goal="Understand leave entitlement, payroll breakdown, and compliance requirements",
)

compliance_auditor = ActorProfile(
    traits={
        "personality": "thorough and detail-oriented",
        "communication_style": "formal and precise",
        "expertise_level": "expert",
        "patience_level": "medium",
    },
    context="External compliance auditor reviewing HR processes across multiple countries",
    actor_goal="Verify compliance requirements for Thailand and Singapore, cross-reference with employee records",
)

ACTOR_PROFILES = {
    "impatient_hr_manager": impatient_hr_manager,
    "new_employee": new_employee,
    "compliance_auditor": compliance_auditor,
}

# ---------------------------------------------------------------------------
# Test Cases — 3 cases exercising multi-tool conversations
# ---------------------------------------------------------------------------

CASES = [
    Case(
        input="I need the full onboarding package for a new hire in Singapore — payroll, compliance, the works.",
        metadata={
            "task_description": "Complete onboarding with payroll, compliance, and leave info for Singapore",
            "persona": "impatient_hr_manager",
            "expected_tools": ["payroll_calculator", "compliance_checker", "employee_lookup"],
        },
    ),
    Case(
        input="Hi, I just started at the company as EMP-67890 in Singapore. Can you help me understand my leave and pay?",
        metadata={
            "task_description": "Help new employee understand leave entitlement, payroll breakdown, and compliance",
            "persona": "new_employee",
            "expected_tools": ["leave_manager", "employee_lookup", "payroll_calculator"],
        },
    ),
    Case(
        input="I need to verify the compliance requirements for both Thailand and Singapore operations. Please provide detailed regulatory information.",
        metadata={
            "task_description": "Cross-country compliance verification for Thailand and Singapore with employee records",
            "persona": "compliance_auditor",
            "expected_tools": ["compliance_checker", "employee_lookup"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Telemetry setup
# ---------------------------------------------------------------------------


def setup_telemetry() -> StrandsEvalsTelemetry:
    """Initialize StrandsEvalsTelemetry with in-memory exporter."""
    telemetry = StrandsEvalsTelemetry()
    telemetry.setup_in_memory_exporter()
    return telemetry


# ---------------------------------------------------------------------------
# Multi-turn conversation runner
# ---------------------------------------------------------------------------


def run_conversation(
    model_key: str,
    case: Case,
    max_turns: int,
    telemetry: StrandsEvalsTelemetry,
    mapper: StrandsInMemorySessionMapper,
) -> dict:
    """Run a multi-turn conversation between the agent and a simulated user.

    Args:
        model_key: Model key from SUPPORTED_MODELS.
        case: The test case with input and metadata.
        max_turns: Maximum conversation turns.
        telemetry: Telemetry instance for trace capture.
        mapper: Session mapper for span processing.

    Returns:
        Dict with conversation history, timing, and tool usage.
    """
    persona_name = case.metadata.get("persona", "unknown")
    print(f"    Persona: {persona_name}, Max turns: {max_turns}")

    # Create the target agent
    agent = create_agent(model_key)

    # Create the user simulator from the case
    user_sim = ActorSimulator.from_case_for_user_simulator(
        case=case,
        max_turns=max_turns,
    )

    # Clear any leftover spans
    try:
        telemetry.in_memory_exporter.clear()
    except Exception:
        pass

    # Run conversation loop
    conversation_history = []
    turn_details = []
    total_start = time.time()
    user_message = case.input

    turn_num = 0
    while user_sim.has_next():
        turn_num += 1
        turn_start = time.time()

        # Agent responds
        try:
            agent_response = agent(user_message)
            agent_message = str(agent_response)
        except Exception as e:
            agent_message = f"[ERROR: {e}]"
            print(f"      Turn {turn_num}: Agent error — {e}")
            break

        turn_duration_ms = (time.time() - turn_start) * 1000

        # Record agent turn
        conversation_history.append({"role": "user", "content": user_message})
        conversation_history.append({"role": "assistant", "content": agent_message})

        # Extract tool usage from agent response (if available)
        tools_used = []
        if hasattr(agent_response, "tool_results"):
            for tr in agent_response.tool_results or []:
                if isinstance(tr, dict) and "tool" in tr:
                    tools_used.append(tr["tool"])

        turn_details.append({
            "turn": turn_num,
            "user_message": user_message,
            "agent_message": agent_message[:500],
            "tools_used": tools_used,
            "duration_ms": round(turn_duration_ms, 1),
        })

        print(f"      Turn {turn_num}: {turn_duration_ms:.0f}ms")

        # User simulator generates next message
        try:
            user_result = user_sim.act(agent_message)
            user_message = str(user_result.structured_output.message)
        except Exception as e:
            print(f"      User sim error at turn {turn_num}: {e}")
            break

    total_duration_ms = (time.time() - total_start) * 1000

    # Capture traces
    session_dict = None
    try:
        exporter = telemetry.in_memory_exporter
        spans = list(exporter.get_finished_spans())
        if spans:
            session_id = f"{model_key}-{persona_name}-conversation"
            session_dict = mapper.map_to_session(spans, session_id=session_id)
        exporter.clear()
    except Exception as e:
        print(f"      [WARN] Trace capture failed: {e}")
        try:
            telemetry.in_memory_exporter.clear()
        except Exception:
            pass

    return {
        "model_key": model_key,
        "persona": persona_name,
        "case_input": case.input,
        "task_description": case.metadata.get("task_description", ""),
        "conversation_history": conversation_history,
        "turn_details": turn_details,
        "total_turns": turn_num,
        "total_duration_ms": round(total_duration_ms, 1),
        "session": session_dict,
    }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_conversation(conversation_result: dict) -> dict:
    """Evaluate a completed conversation using HelpfulnessEvaluator.

    The HelpfulnessEvaluator requires a Session object (from trace capture)
    as actual_trajectory. If trace capture succeeded, we use it. Otherwise,
    we fall back to a simple LLM-based scoring approach.

    Args:
        conversation_result: Dict from run_conversation().

    Returns:
        Dict with helpfulness_score, goal_achieved, and reason.
    """
    history = conversation_result["conversation_history"]
    task_desc = conversation_result["task_description"]
    session = conversation_result.get("session")

    if not history:
        return {
            "helpfulness_score": 0.0,
            "goal_achieved": False,
            "reason": "No conversation history",
        }

    input_text = history[0]["content"] if history else ""

    # Try using HelpfulnessEvaluator with Session-based trajectory
    if session is not None:
        try:
            evaluator = HelpfulnessEvaluator()
            eval_data = EvaluationData(
                input=input_text,
                actual_trajectory=session,
            )
            results = evaluator.evaluate(eval_data)

            if results:
                score = results[0].score
                reason = results[0].reason or ""
                return {
                    "helpfulness_score": score,
                    "goal_achieved": score >= 0.7,
                    "reason": reason,
                }
        except Exception as e:
            print(f"      [WARN] HelpfulnessEvaluator with session failed: {e}")

    # Fallback: use a simple LLM-based evaluation without trace parsing
    try:
        from strands import Agent as EvalAgent

        output_parts = [
            msg["content"] for msg in history if msg["role"] == "assistant"
        ]
        actual_output = "\n\n".join(output_parts)

        eval_agent = EvalAgent(
            model="us.anthropic.claude-sonnet-4-6",
            system_prompt=(
                "You are an evaluation judge. Rate the helpfulness of an AI assistant's "
                "responses in a multi-turn conversation. Consider: completeness, accuracy, "
                "tool usage, and whether the user's goal was achieved.\n\n"
                "Respond with ONLY a JSON object: {\"score\": <0.0-1.0>, \"reason\": \"<brief reason>\"}"
            ),
            callback_handler=None,
        )

        eval_prompt = (
            f"Task: {task_desc}\n\n"
            f"User's initial query: {input_text}\n\n"
            f"Assistant's responses across the conversation:\n{actual_output[:3000]}\n\n"
            f"Rate helpfulness (0.0 to 1.0):"
        )

        eval_response = eval_agent(eval_prompt)
        response_text = str(eval_response)

        # Parse JSON from response
        import re
        json_match = re.search(r'\{[^}]+\}', response_text)
        if json_match:
            eval_result = json.loads(json_match.group())
            score = float(eval_result.get("score", 0.5))
            reason = eval_result.get("reason", "")
            return {
                "helpfulness_score": score,
                "goal_achieved": score >= 0.7,
                "reason": reason,
            }
    except Exception as e:
        print(f"      [WARN] Fallback evaluation failed: {e}")

    # Last resort: heuristic scoring based on conversation length and completion
    total_turns = conversation_result.get("total_turns", 0)
    if total_turns > 0:
        # Basic heuristic: conversations that complete are somewhat helpful
        score = min(0.6, total_turns * 0.2)
        return {
            "helpfulness_score": score,
            "goal_achieved": False,
            "reason": "Heuristic score (evaluation unavailable)",
        }

    return {
        "helpfulness_score": 0.0,
        "goal_achieved": False,
        "reason": "Evaluation failed",
    }


# ---------------------------------------------------------------------------
# Output generation
# ---------------------------------------------------------------------------


def generate_report(all_results: dict[str, list[dict]], evaluations: dict[str, list[dict]]):
    """Generate results/multi_turn_comparison.md.

    Args:
        all_results: Dict of model_key -> list of conversation results.
        evaluations: Dict of model_key -> list of evaluation dicts.
    """
    results_dir = PROJECT_ROOT / "results"
    results_dir.mkdir(exist_ok=True)
    output_path = results_dir / "multi_turn_comparison.md"

    models = list(all_results.keys())
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        "# Multi-Turn Evaluation Results",
        "",
        f"Generated: {timestamp}",
        "",
        f"Models evaluated: {', '.join(models)}",
        f"Test cases: {len(CASES)}",
        f"Actor profiles: {len(ACTOR_PROFILES)}",
        "",
        "## Summary",
        "",
        "| Model | Avg Helpfulness | Goal Success Rate | Avg Turns | Avg Latency |",
        "| --- | --- | --- | --- | --- |",
    ]

    # Summary table
    for model_key in models:
        evals = evaluations.get(model_key, [])
        results = all_results.get(model_key, [])

        if evals:
            avg_helpfulness = sum(e["helpfulness_score"] for e in evals) / len(evals)
            goal_success = sum(1 for e in evals if e["goal_achieved"])
            goal_rate = f"{goal_success}/{len(evals)}"
        else:
            avg_helpfulness = 0.0
            goal_rate = "0/0"

        if results:
            avg_turns = sum(r["total_turns"] for r in results) / len(results)
            avg_latency_s = sum(r["total_duration_ms"] for r in results) / len(results) / 1000
        else:
            avg_turns = 0
            avg_latency_s = 0

        lines.append(
            f"| {model_key} | {avg_helpfulness:.2f} | {goal_rate} | {avg_turns:.1f} | {avg_latency_s:.1f}s |"
        )

    lines.append("")

    # Per-case details
    for case_idx, case in enumerate(CASES, 1):
        persona = case.metadata.get("persona", "unknown")
        task_desc = case.metadata.get("task_description", "")
        profile = ACTOR_PROFILES.get(persona)
        goal = profile.actor_goal if profile else "N/A"

        lines.extend([
            f"## Case {case_idx}: {persona.replace('_', ' ').title()} — {task_desc}",
            "",
            f"**Goal:** {goal}",
            "",
        ])

        for model_key in models:
            results = all_results.get(model_key, [])
            evals = evaluations.get(model_key, [])

            if case_idx - 1 < len(results):
                result = results[case_idx - 1]
                eval_result = evals[case_idx - 1] if case_idx - 1 < len(evals) else {}

                helpfulness = eval_result.get("helpfulness_score", 0.0)
                goal_achieved = "✓" if eval_result.get("goal_achieved") else "✗"

                lines.append(
                    f"### {model_key} (Helpfulness: {helpfulness:.2f}, Goal: {goal_achieved})"
                )
                lines.append("")

                # Conversation transcript
                for turn in result.get("turn_details", []):
                    tools_str = ""
                    if turn.get("tools_used"):
                        tools_str = f" [called {', '.join(turn['tools_used'])}]"

                    user_msg = turn["user_message"][:200]
                    agent_msg = turn["agent_message"][:300]

                    lines.append(
                        f"**Turn {turn['turn']}** ({turn['duration_ms']:.0f}ms){tools_str}"
                    )
                    lines.append(f"- User: {user_msg}")
                    lines.append(f"- Agent: {agent_msg}...")
                    lines.append("")

            lines.append("")

    # Write output
    output_path.write_text("\n".join(lines))
    print(f"\nResults saved to: {output_path.relative_to(PROJECT_ROOT)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Run multi-turn evaluation with ActorSimulator."
    )
    parser.add_argument(
        "--models",
        type=str,
        default="sonnet,haiku,nova_pro",
        help="Comma-separated model keys (default: sonnet,haiku,nova_pro)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=5,
        help="Maximum conversation turns per case (default: 5)",
    )
    args = parser.parse_args()

    # Parse and validate model keys
    model_keys = [m.strip() for m in args.models.split(",")]
    for key in model_keys:
        if key not in SUPPORTED_MODELS:
            print(f"ERROR: Unknown model key '{key}'. Supported: {list(SUPPORTED_MODELS.keys())}")
            sys.exit(1)

    print("=" * 60)
    print("MULTIPLIER EDD POC — Multi-Turn Evaluation")
    print("=" * 60)
    print(f"Models: {model_keys}")
    print(f"Cases: {len(CASES)}")
    print(f"Max turns: {args.max_turns}")
    print("=" * 60)

    # Setup telemetry
    print("\n[Setup] Initializing telemetry...")
    telemetry = setup_telemetry()
    mapper = StrandsInMemorySessionMapper()
    print("  ✓ Telemetry configured")

    # Run conversations
    all_results: dict[str, list[dict]] = {}
    for model_key in model_keys:
        print(f"\n[Run] Model: {model_key} ({SUPPORTED_MODELS[model_key]})")
        model_results = []

        for case_idx, case in enumerate(CASES, 1):
            persona = case.metadata.get("persona", "unknown")
            print(f"  Case {case_idx}/{len(CASES)}: {persona}")

            result = run_conversation(
                model_key=model_key,
                case=case,
                max_turns=args.max_turns,
                telemetry=telemetry,
                mapper=mapper,
            )
            model_results.append(result)
            print(f"    ✓ Completed in {result['total_turns']} turns, {result['total_duration_ms']:.0f}ms")

        all_results[model_key] = model_results

    # Evaluate conversations
    print("\n[Evaluate] Running HelpfulnessEvaluator...")
    evaluations: dict[str, list[dict]] = {}
    for model_key in model_keys:
        print(f"  Evaluating {model_key}...")
        model_evals = []
        for result in all_results[model_key]:
            eval_result = evaluate_conversation(result)
            model_evals.append(eval_result)
            persona = result["persona"]
            score = eval_result["helpfulness_score"]
            goal = "✓" if eval_result["goal_achieved"] else "✗"
            print(f"    {persona}: helpfulness={score:.2f}, goal={goal}")
        evaluations[model_key] = model_evals

    # Generate report
    print("\n[Report] Generating multi_turn_comparison.md...")
    generate_report(all_results, evaluations)

    # Print summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for model_key in model_keys:
        evals = evaluations[model_key]
        avg_score = sum(e["helpfulness_score"] for e in evals) / len(evals) if evals else 0
        goals = sum(1 for e in evals if e["goal_achieved"])
        print(f"  {model_key}: avg_helpfulness={avg_score:.2f}, goals={goals}/{len(evals)}")
    print("=" * 60)
    print("\nDone!")


if __name__ == "__main__":
    main()
