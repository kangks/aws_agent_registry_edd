"""
AgentCore Runtime entry point for the Multiplier EDD POC.

Uses BedrockAgentCoreApp wrapper to implement the runtime service contract.
The aws-opentelemetry-distro (ADOT) handles automatic instrumentation.
strands-agents[otel] provides the Strands-specific span instrumentation.
Do NOT manually call get_tracer() - let ADOT handle the tracer provider.
"""

import os
import sys

os.environ["BYPASS_TOOL_CONSENT"] = "true"

# Add the agents directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
