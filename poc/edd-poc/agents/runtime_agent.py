"""
AgentCore Runtime entry point for the Multiplier EDD POC.

This module exposes the handler that `agentcore deploy` expects.
It imports the agent factory and delegates invocations to the
configured Strands agent.
"""

import os

from agent import create_agent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL_KEY = os.environ.get("AGENT_MODEL_KEY", "sonnet")

# Create agent instance at module level so it persists across invocations
agent = create_agent(MODEL_KEY)


# ---------------------------------------------------------------------------
# AgentCore Runtime entry point
# ---------------------------------------------------------------------------

def handler(event, context=None):
    """Entry point called by AgentCore Runtime on each invocation.

    Args:
        event: Invocation payload containing the user prompt.
            Expected shape: {"prompt": "..."} or {"input": "..."}
        context: Optional runtime context (unused).

    Returns:
        dict with the agent's response text.
    """
    # Accept prompt from common payload keys
    prompt = event.get("prompt") or event.get("input") or ""

    if not prompt:
        return {"response": "No prompt provided.", "status": "error"}

    # Invoke the agent and capture the response
    result = agent(prompt)

    return {"response": str(result), "status": "success"}
