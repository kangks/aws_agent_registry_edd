"""
BYO (Bring Your Own) agent runner for CloudWatch GenAI Observability.

This script runs the HR/compliance agent locally while ADOT (AWS Distro for
OpenTelemetry) handles all instrumentation and trace export to CloudWatch.

Unlike local_runner.py, this script does NOT manually configure OTEL exporters
or call get_tracer(). Instead, it relies on the `opentelemetry-instrument`
wrapper and ADOT's aws_distro/aws_configurator to handle everything.

Usage:
    AGENT_OBSERVABILITY_ENABLED=true \\
    OTEL_PYTHON_DISTRO=aws_distro \\
    OTEL_PYTHON_CONFIGURATOR=aws_configurator \\
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \\
    OTEL_RESOURCE_ATTRIBUTES="service.name=multiplier-byo-sonnet" \\
    opentelemetry-instrument python agents/byo_runner.py --model sonnet --prompt "..."
"""

import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        description="BYO agent runner — ADOT handles instrumentation."
    )
    parser.add_argument(
        "--model",
        type=str,
        default="sonnet",
        choices=["sonnet", "nova_2_pro", "glm_5"],
        help="Model key to use (default: sonnet)",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="Prompt to send to the agent.",
    )
    args = parser.parse_args()

    model_key = args.model

    # Set service.name for OTEL resource attributes if not already set externally.
    # In normal BYO usage, this is set via env vars BEFORE opentelemetry-instrument runs.
    if "OTEL_RESOURCE_ATTRIBUTES" not in os.environ:
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = f"service.name=multiplier-byo-{model_key}"

    # Set session ID via OTEL baggage for evaluation support
    try:
        from opentelemetry import baggage
        from opentelemetry.context import attach
        import uuid

        session_id = os.environ.get("BYO_SESSION_ID", f"byo-{model_key}-{uuid.uuid4().hex[:8]}")
        ctx = baggage.set_baggage("session.id", session_id)
        attach(ctx)
    except ImportError:
        pass

    # Initialize Strands OTEL tracer to emit spans AND log events with
    # 'strands.telemetry.tracer' scope. This is critical for evaluation —
    # without it, the invoke_agent span won't have a corresponding log event.
    from strands.telemetry.tracer import get_tracer
    get_tracer()

    # Install the custom SpanProcessor that emits the invoke_agent log record.
    # This bridges the gap between BYO agents and the AgentCore Evaluator's
    # LLM-as-a-Judge path (which requires this log record to exist).
    from invoke_agent_log_emitter import install as install_log_emitter
    install_log_emitter()

    # Import agent factory AFTER env vars are set
    from agent import create_agent  # noqa: E402

    # Resolve prompt
    prompt = args.prompt
    if not prompt:
        if sys.stdin.isatty():
            print("No --prompt provided and stdin is a terminal. Exiting.", file=sys.stderr)
            sys.exit(1)
        prompt = sys.stdin.read().strip()

    if not prompt:
        print("Empty prompt. Exiting.", file=sys.stderr)
        sys.exit(1)

    # Store the user query so the log emitter can include it in the log record
    os.environ["_BYO_USER_QUERY"] = prompt

    # Create agent and invoke — ADOT handles all trace instrumentation
    agent = create_agent(model_key)
    result = agent(prompt)

    # Print response to stdout
    print(str(result))


if __name__ == "__main__":
    main()
