"""
Agent Registry — unified catalog of managed + BYO agents with eval history.

Combines:
1. AgentCore `list_agent_runtimes` (source of truth for managed agents)
2. Local registry.json (augments with BYO agents, eval scores, timestamps)

Usage:
    from registry import get_all_agents, get_agent, update_eval_results, get_stale_agents

    agents = get_all_agents()
    agent = get_agent("multiplier_hr_sonnet")
    update_eval_results("multiplier_hr_sonnet", {"correctness": 0.8, "helpfulness": 1.0}, "2025-01-15T10:00:00Z")
    stale = get_stale_agents(days=7)
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import boto3

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REGISTRY_DIR = Path(__file__).resolve().parent
REGISTRY_FILE = REGISTRY_DIR / "registry.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_registry() -> dict:
    """Load registry.json from disk."""
    if not REGISTRY_FILE.exists():
        return {"agents": [], "metadata": {}}
    with open(REGISTRY_FILE, "r") as f:
        return json.load(f)


def _save_registry(data: dict) -> None:
    """Save registry.json to disk."""
    with open(REGISTRY_FILE, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _get_agentcore_client():
    """Create a boto3 client for bedrock-agentcore."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    session = boto3.Session(
        profile_name=os.environ.get("AWS_PROFILE"),
        region_name=region,
    )
    return session.client("bedrock-agentcore")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def refresh_managed_agents() -> list[dict]:
    """
    Query AgentCore list_agent_runtimes and return metadata for managed agents.

    Returns a list of dicts with: name, runtime_id, arn, status, version, created, updated.
    """
    try:
        client = _get_agentcore_client()
        response = client.list_agent_runtimes()
        runtimes = response.get("agentRuntimeSummaries", [])

        managed = []
        for rt in runtimes:
            managed.append({
                "name": rt.get("agentRuntimeName", ""),
                "runtime_id": rt.get("agentRuntimeId", ""),
                "arn": rt.get("agentRuntimeArn", ""),
                "status": rt.get("status", ""),
                "created": rt.get("createdAt", ""),
                "updated": rt.get("lastUpdatedAt", ""),
            })
        return managed
    except Exception as e:
        print(f"[WARN] Could not query AgentCore: {e}")
        return []


def get_all_agents() -> list[dict]:
    """
    Get all agents (managed + BYO) from the registry.

    Returns a list of agent metadata dicts. Managed agents are enriched
    with live status from AgentCore if available.
    """
    registry = _load_registry()
    agents = registry.get("agents", [])

    # Try to enrich managed agents with live status
    try:
        managed_live = refresh_managed_agents()
        managed_map = {m["name"]: m for m in managed_live}

        for agent in agents:
            if agent.get("deployment_type") == "managed":
                live = managed_map.get(agent["name"])
                if live:
                    agent["live_status"] = live.get("status")
                    agent["live_arn"] = live.get("arn")
    except Exception:
        pass  # Graceful degradation — registry still works without live data

    return agents


def get_agent(name: str) -> Optional[dict]:
    """
    Get a single agent by name.

    Args:
        name: Agent name (e.g., "multiplier_hr_sonnet" or "multiplier_byo_haiku")

    Returns:
        Agent metadata dict, or None if not found.
    """
    agents = get_all_agents()
    for agent in agents:
        if agent.get("name") == name:
            return agent
    return None


def update_eval_results(
    name: str,
    scores: dict[str, Any],
    timestamp: Optional[str] = None,
    evaluator: Optional[str] = None,
) -> bool:
    """
    Update the registry with latest evaluation results for an agent.

    Args:
        name: Agent name
        scores: Dict of score name → value (e.g., {"correctness": 0.8, "helpfulness": 1.0})
        timestamp: ISO timestamp of evaluation (defaults to now)
        evaluator: Name of the evaluator used

    Returns:
        True if agent was found and updated, False otherwise.
    """
    registry = _load_registry()
    agents = registry.get("agents", [])

    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()

    for agent in agents:
        if agent.get("name") == name:
            agent["last_eval_score"] = scores
            agent["last_eval_timestamp"] = timestamp
            if evaluator:
                agent["last_eval_evaluator"] = evaluator
            _save_registry(registry)
            return True

    return False


def get_stale_agents(days: int = 7) -> list[dict]:
    """
    Get agents that haven't been evaluated in N days.

    Useful for scheduled evaluation triggers — identifies agents
    that need re-evaluation.

    Args:
        days: Number of days after which an agent is considered stale.

    Returns:
        List of agent metadata dicts that are stale (never evaluated or
        last evaluated more than N days ago).
    """
    registry = _load_registry()
    agents = registry.get("agents", [])
    now = datetime.now(timezone.utc)
    stale = []

    for agent in agents:
        last_eval = agent.get("last_eval_timestamp")
        if last_eval is None:
            stale.append(agent)
            continue

        try:
            # Parse ISO timestamp
            eval_time = datetime.fromisoformat(last_eval.replace("Z", "+00:00"))
            if (now - eval_time).days >= days:
                stale.append(agent)
        except (ValueError, TypeError):
            stale.append(agent)

    return stale
