"""
AgentCore Runtime entry point for the Multiplier EDD POC.

Uses BedrockAgentCoreApp wrapper to implement the runtime service contract.
Initializes strands.telemetry.tracer to emit spans with the correct scope
that AgentCore evaluators require ('strands.telemetry.tracer').
"""

import os
import sys

os.environ["BYPASS_TOOL_CONSENT"] = "true"
# Suppress the "already instrumented" warning - this is expected
# because the runtime's ADOT sets up a tracer provider first,
# and Strands hooks into it.
os.environ.setdefault("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", "")

# Add the agents directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Initialize Strands OTEL tracer to emit spans with 'strands.telemetry.tracer' scope.
# This must happen BEFORE creating the agent so the agent's spans use this tracer.
from strands.telemetry.tracer import get_tracer
get_tracer()

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from agent import create_agent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL_KEY = os.environ.get("AGENT_MODEL_KEY", "sonnet")

# Create the AgentCore Runtime app
app = BedrockAgentCoreApp()

# Create agent instance
agent = create_agent(MODEL_KEY)


@app.entrypoint
def invoke(payload):
    """Entry point called by AgentCore Runtime on each invocation."""
    prompt = payload.get("prompt") or payload.get("input") or ""

    if not prompt:
        return "No prompt provided."

    response = agent(prompt)
    return str(response)


if __name__ == "__main__":
    app.run()
