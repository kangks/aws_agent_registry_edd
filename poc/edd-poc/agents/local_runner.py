"""
Local CLI runner with OpenTelemetry instrumentation for the Multiplier EDD POC.

Runs the HR/compliance agent locally, exporting traces to CloudWatch via OTEL.
This demonstrates the BYO (Bring Your Own) agent path where the same evaluators
can score traces from a locally-run agent.

Usage:
    python agents/local_runner.py --model sonnet --prompt "What is employee 12345 status in Singapore?"
"""

import argparse
import os
import sys


def main():
    # -----------------------------------------------------------------------
    # Parse CLI arguments FIRST (before any OTEL/agent imports)
    # -----------------------------------------------------------------------
    parser = argparse.ArgumentParser(
        description="Run the Multiplier EDD agent locally with OTEL instrumentation."
    )
    parser.add_argument(
        "--model",
        type=str,
        default="sonnet",
        choices=["sonnet", "haiku", "nova_pro"],
        help="Model key to use (default: sonnet)",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="Prompt to send to the agent. If not provided, reads from stdin.",
    )
    args = parser.parse_args()

    model_key = args.model

    # -----------------------------------------------------------------------
    # Set OTEL environment variables BEFORE importing agent/telemetry modules
    # -----------------------------------------------------------------------
    service_name = f"multiplier-local-{model_key}"
    os.environ["OTEL_RESOURCE_ATTRIBUTES"] = f"service.name={service_name}"

    # Ensure OTEL exporter sends to CloudWatch (OTLP over gRPC is the default
    # expected by the CloudWatch OTEL collector sidecar or ADOT collector)
    if "OTEL_EXPORTER_OTLP_ENDPOINT" not in os.environ:
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://localhost:4317"

    # -----------------------------------------------------------------------
    # Import agent factory and OTEL instrumentation AFTER env vars are set
    # -----------------------------------------------------------------------
    from strands.telemetry.tracer import get_tracer  # noqa: E402
    from agent import create_agent  # noqa: E402

    # Initialize the tracer (registers the OTEL pipeline)
    _tracer = get_tracer()

    # -----------------------------------------------------------------------
    # Resolve prompt
    # -----------------------------------------------------------------------
    prompt = args.prompt
    if not prompt:
        if sys.stdin.isatty():
            print("No --prompt provided and stdin is a terminal. Exiting.", file=sys.stderr)
            sys.exit(1)
        prompt = sys.stdin.read().strip()

    if not prompt:
        print("Empty prompt. Exiting.", file=sys.stderr)
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Create agent and invoke
    # -----------------------------------------------------------------------
    agent = create_agent(model_key)
    result = agent(prompt)

    # Print response to stdout
    print(str(result))


if __name__ == "__main__":
    main()
