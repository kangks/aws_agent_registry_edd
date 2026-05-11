"""
Shared pytest fixtures for the unified evaluation comparison test suite.

Provides:
- Agent configuration fixtures (managed agents, BYO agents, prompts)
- Mock subprocess responses (agentcore invoke, opentelemetry-instrument)
- Mock boto3 client factories (bedrock-agentcore, logs, bedrock-agentcore-control)
- Mock CloudWatch Logs Insights responses with realistic span data
- Mock AgentCore Evaluate API responses with scores and justifications
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts directory to path so we can import the modules under test
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Agent Configuration Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def managed_agents():
    """Return the 3 managed agent configurations."""
    return [
        {"name": "multiplier_hr_sonnet", "runtime": "multiplier_hr_sonnet", "model_key": "sonnet"},
        {"name": "multiplier_hr_nova_2_pro", "runtime": "multiplier_hr_nova_2_pro", "model_key": "nova_2_pro"},
        {"name": "multiplier_hr_glm_5", "runtime": "multiplier_hr_glm_5", "model_key": "glm_5"},
    ]


@pytest.fixture
def byo_agents():
    """Return the 3 BYO agent configurations."""
    return [
        {"name": "multiplier_byo_sonnet", "model_key": "sonnet", "service_name": "multiplier-byo-sonnet"},
        {"name": "multiplier_byo_nova_2_pro", "model_key": "nova_2_pro", "service_name": "multiplier-byo-nova-2-pro"},
        {"name": "multiplier_byo_glm_5", "model_key": "glm_5", "service_name": "multiplier-byo-glm-5"},
    ]


@pytest.fixture
def sample_prompts():
    """Return the 5 sample prompts used in the unified comparison."""
    return [
        "What is the employment status of employee EMP-12345 in Singapore?",
        "What are the notice periods and regulatory requirements for employees in Thailand?",
        "Calculate the monthly payroll breakdown for an employee earning 90000 SGD annually in Singapore.",
        "Check the leave balance for employee EMP-67890. How many days do they have remaining?",
        "For employee EMP-11111 in India, look up their details, check compliance rules, and calculate their payroll breakdown in INR.",
    ]


# ---------------------------------------------------------------------------
# Mock Subprocess Responses
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_managed_invoke_stdout():
    """Return a factory for realistic agentcore invoke stdout with session IDs.

    Usage:
        stdout = mock_managed_invoke_stdout("multiplier_hr_sonnet", 0)
    """
    def _factory(runtime_name: str, prompt_index: int) -> str:
        session_id = f"session-{runtime_name}-{prompt_index:03d}"
        return (
            f"Invoking runtime {runtime_name}...\n"
            f"Session: {session_id}\n"
            f"Response: The employee EMP-12345 is currently active in Singapore.\n"
            f"Duration: 2.3s\n"
        )
    return _factory


@pytest.fixture
def mock_byo_invoke_stdout():
    """Return a factory for realistic opentelemetry-instrument stdout.

    Usage:
        stdout = mock_byo_invoke_stdout("sonnet", 0)
    """
    def _factory(model_key: str, prompt_index: int) -> str:
        return (
            f"[OTEL] Instrumentation active for model={model_key}\n"
            f"Agent response: Based on the HR records, the employee status is active.\n"
            f"[OTEL] Spans exported successfully\n"
        )
    return _factory


@pytest.fixture
def mock_subprocess_run(mock_managed_invoke_stdout, mock_byo_invoke_stdout):
    """Return a mock for subprocess.run that handles both agentcore invoke and otel-instrument.

    The mock inspects the command to determine which type of invocation is being made
    and returns appropriate stdout with session IDs or BYO responses.
    """
    call_counter = {"managed": 0, "byo": 0}

    def _side_effect(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0

        if "agentcore" in cmd[0] or (len(cmd) > 1 and cmd[1] == "invoke"):
            # Managed agent invocation via agentcore invoke
            runtime_name = cmd[cmd.index("--runtime") + 1] if "--runtime" in cmd else "unknown"
            idx = call_counter["managed"]
            call_counter["managed"] += 1
            result.stdout = mock_managed_invoke_stdout(runtime_name, idx % 5)
            result.stderr = ""
        elif "opentelemetry-instrument" in cmd[0]:
            # BYO agent invocation via opentelemetry-instrument
            model_key = cmd[cmd.index("--model") + 1] if "--model" in cmd else "unknown"
            idx = call_counter["byo"]
            call_counter["byo"] += 1
            result.stdout = mock_byo_invoke_stdout(model_key, idx % 5)
            result.stderr = ""
        else:
            result.stdout = ""
            result.stderr = "Unknown command"
            result.returncode = 1

        return result

    return _side_effect


# ---------------------------------------------------------------------------
# Mock CloudWatch Logs Insights Responses
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_span_data():
    """Return a factory for realistic OTEL span data as stored in CloudWatch aws/spans.

    Each span represents a single operation in the BYO agent trace.
    """
    def _factory(service_name: str, prompt_index: int, span_count: int = 3) -> list[dict]:
        base_trace_id = f"trace-{service_name}-{prompt_index:03d}"
        spans = []
        for i in range(span_count):
            span = {
                "traceId": base_trace_id,
                "spanId": f"span-{i:04d}",
                "parentSpanId": f"span-{i - 1:04d}" if i > 0 else "",
                "name": f"operation_{i}",
                "kind": "INTERNAL" if i > 0 else "SERVER",
                "startTimeUnixNano": 1700000000000000000 + (i * 100000000),
                "endTimeUnixNano": 1700000000000000000 + ((i + 1) * 100000000),
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": service_name}},
                    {"key": "gen_ai.system", "value": {"stringValue": "aws.bedrock"}},
                ],
                "status": {"code": "OK"},
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service_name}},
                    ]
                },
            }
            if i == 0:
                span["attributes"].append(
                    {"key": "gen_ai.prompt", "value": {"stringValue": f"prompt_{prompt_index}"}}
                )
            spans.append(span)
        return spans
    return _factory


@pytest.fixture
def mock_cw_logs_start_query_response():
    """Return a factory for CloudWatch Logs start_query responses."""
    query_counter = {"count": 0}

    def _factory(**kwargs) -> dict:
        query_id = f"query-{query_counter['count']:04d}"
        query_counter["count"] += 1
        return {"queryId": query_id}

    return _factory


@pytest.fixture
def mock_cw_logs_get_query_results(sample_span_data):
    """Return a factory for CloudWatch Logs get_query_results responses.

    Returns realistic results with span data in @message fields.
    The service_name is extracted from the query to generate appropriate spans.
    """
    def _factory(service_name: str, prompt_index: int = 0, status: str = "Complete") -> dict:
        if status != "Complete":
            return {"status": status, "results": []}

        spans = sample_span_data(service_name, prompt_index)
        results = []
        for span in spans:
            results.append([
                {"field": "@timestamp", "value": "2024-01-15 10:00:00.000"},
                {"field": "@message", "value": json.dumps(span)},
            ])

        return {"status": "Complete", "results": results}

    return _factory


@pytest.fixture
def mock_logs_client(mock_cw_logs_start_query_response, mock_cw_logs_get_query_results):
    """Return a fully mocked CloudWatch Logs client.

    Handles start_query and get_query_results with realistic responses.
    Tracks which service names have been queried.
    """
    client = MagicMock()
    queried_services = []

    def _start_query(**kwargs):
        query_string = kwargs.get("queryString", "")
        # Extract service name from query string
        for svc in [
            "multiplier-byo-sonnet",
            "multiplier-byo-nova-2-pro",
            "multiplier-byo-glm-5",
        ]:
            if svc in query_string:
                queried_services.append(svc)
                break
        return mock_cw_logs_start_query_response(**kwargs)

    def _get_query_results(**kwargs):
        # Use the last queried service name to generate appropriate spans
        svc = queried_services[-1] if queried_services else "multiplier-byo-sonnet"
        return mock_cw_logs_get_query_results(svc)

    client.start_query.side_effect = _start_query
    client.get_query_results.side_effect = _get_query_results
    client._queried_services = queried_services

    return client


# ---------------------------------------------------------------------------
# Mock AgentCore Evaluate API Responses
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_evaluate_response():
    """Return a factory for AgentCore Evaluate API responses with scores and justifications.

    Usage:
        response = mock_evaluate_response("multiplier_hr_sonnet", 0, ["multiplier_domain_accuracy"])
    """
    def _factory(
        agent_name: str,
        prompt_index: int,
        evaluator_names: list[str] | None = None,
        score: float | None = None,
    ) -> dict:
        if evaluator_names is None:
            evaluator_names = ["multiplier_domain_accuracy"]

        results = []
        for evaluator_name in evaluator_names:
            # Generate a deterministic score based on agent name and prompt index
            if score is not None:
                eval_score = score
            else:
                # Scores between 3.0 and 5.0 for realistic results
                eval_score = 3.0 + ((hash(f"{agent_name}_{prompt_index}_{evaluator_name}") % 20) / 10.0)
                eval_score = min(eval_score, 5.0)

            results.append({
                "evaluatorName": evaluator_name,
                "score": eval_score,
                "justification": (
                    f"The agent {agent_name} provided a relevant response to prompt {prompt_index + 1}. "
                    f"The response demonstrates understanding of the HR domain and provides "
                    f"accurate information based on the available data."
                ),
            })

        return {"evaluationResults": results}

    return _factory


@pytest.fixture
def mock_agentcore_client(mock_evaluate_response):
    """Return a fully mocked bedrock-agentcore client.

    Handles evaluate() calls and returns realistic scores with justifications.
    Tracks all evaluate calls for assertion.
    """
    client = MagicMock()
    evaluate_calls = []

    def _evaluate(**kwargs):
        evaluate_calls.append(kwargs)

        # Determine agent name from params
        if "agentRuntimeArn" in kwargs:
            # Managed agent — extract runtime name from ARN
            arn = kwargs["agentRuntimeArn"]
            runtime_name = arn.split("/")[-1]
            agent_name = runtime_name
        else:
            agent_name = "byo_agent"

        evaluator_names = kwargs.get("evaluatorNames", ["multiplier_domain_accuracy"])
        return mock_evaluate_response(agent_name, 0, evaluator_names)

    client.evaluate.side_effect = _evaluate
    client._evaluate_calls = evaluate_calls

    # Add exceptions attribute for throttling error checks
    client.exceptions = MagicMock()
    client.exceptions.ThrottlingException = type("ThrottlingException", (Exception,), {})

    return client


# ---------------------------------------------------------------------------
# Mock bedrock-agentcore-control Client (Registry Update)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_registry_info():
    """Return sample registry_info.json content with record IDs for all 6 agents."""
    return {
        "records": [
            {"name": "multiplier-hr-sonnet-managed", "record_id": "rec-managed-sonnet-001"},
            {"name": "multiplier-hr-nova-2-pro-managed", "record_id": "rec-managed-nova-001"},
            {"name": "multiplier-hr-glm-5-managed", "record_id": "rec-managed-glm-001"},
            {"name": "multiplier-hr-sonnet-byo", "record_id": "rec-byo-sonnet-001"},
            {"name": "multiplier-hr-nova-2-pro-byo", "record_id": "rec-byo-nova-001"},
            {"name": "multiplier-hr-glm-5-byo", "record_id": "rec-byo-glm-001"},
        ]
    }


@pytest.fixture
def mock_agentcore_control_client(mock_registry_info):
    """Return a fully mocked bedrock-agentcore-control client.

    Handles get_registry_record and update_registry_record calls.
    Tracks all update calls for assertion.
    """
    client = MagicMock()
    update_calls = []

    def _get_registry_record(**kwargs):
        record_id = kwargs.get("recordId", "")
        return {
            "recordId": record_id,
            "descriptors": {
                "custom": {
                    "inlineContent": json.dumps({
                        "last_eval_score": {"legacy_eval": 3.5},
                        "last_eval_date": "2024-01-01T00:00:00+00:00",
                    })
                }
            },
        }

    def _update_registry_record(**kwargs):
        update_calls.append(kwargs)
        return {"recordId": kwargs.get("recordId", "")}

    client.get_registry_record.side_effect = _get_registry_record
    client.update_registry_record.side_effect = _update_registry_record
    client._update_calls = update_calls

    return client


# ---------------------------------------------------------------------------
# Mock boto3.Session Factory
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_boto3_session(mock_agentcore_client, mock_logs_client, mock_agentcore_control_client):
    """Return a mock boto3.Session that provides all required clients.

    Maps service names to mock clients:
    - "bedrock-agentcore" → mock_agentcore_client
    - "logs" → mock_logs_client
    - "bedrock-agentcore-control" → mock_agentcore_control_client
    """
    session = MagicMock()

    client_map = {
        "bedrock-agentcore": mock_agentcore_client,
        "logs": mock_logs_client,
        "bedrock-agentcore-control": mock_agentcore_control_client,
    }

    def _client(service_name, **kwargs):
        return client_map.get(service_name, MagicMock())

    session.client.side_effect = _client
    session._client_map = client_map

    return session


# ---------------------------------------------------------------------------
# Unified Config Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def unified_config():
    """Return a UnifiedConfig instance suitable for testing (0s wait, tmp paths)."""
    from run_unified_comparison import UnifiedConfig

    return UnifiedConfig(
        aws_profile="test-profile",
        aws_region="us-east-1",
        wait_seconds=0,
        evaluators=["multiplier_domain_accuracy"],
        skip_invocation=False,
        intermediate_path="/tmp/test_unified_intermediate.json",
        output_path="/tmp/test_unified_comparison.md",
        max_workers=6,
        managed_timeout=120,
        byo_timeout=300,
    )


# ---------------------------------------------------------------------------
# Sample Invocation Data Fixture (for skip-invocation tests)
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_invocation_data():
    """Return pre-built AgentInvocationData for all 6 agents with 5 prompts each.

    Useful for testing evaluation phase without running invocation.
    """
    from unified_models import AgentInvocationData, InvocationResult

    invocation_data = []
    now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

    # 3 managed agents
    for agent in [
        {"name": "multiplier_hr_sonnet", "runtime": "multiplier_hr_sonnet", "model_key": "sonnet"},
        {"name": "multiplier_hr_nova_2_pro", "runtime": "multiplier_hr_nova_2_pro", "model_key": "nova_2_pro"},
        {"name": "multiplier_hr_glm_5", "runtime": "multiplier_hr_glm_5", "model_key": "glm_5"},
    ]:
        results = []
        for i in range(5):
            results.append(InvocationResult(
                response=f"Response from {agent['name']} for prompt {i + 1}",
                session_id=f"session-{agent['name']}-{i:03d}",
                duration_ms=2500.0 + (i * 100),
                success=True,
            ))
        invocation_data.append(AgentInvocationData(
            name=agent["name"],
            agent_type="managed",
            model_key=agent["model_key"],
            runtime_arn=agent["runtime"],
            results=results,
        ))

    # 3 BYO agents
    for agent in [
        {"name": "multiplier_byo_sonnet", "model_key": "sonnet", "service_name": "multiplier-byo-sonnet"},
        {"name": "multiplier_byo_nova_2_pro", "model_key": "nova_2_pro", "service_name": "multiplier-byo-nova-2-pro"},
        {"name": "multiplier_byo_glm_5", "model_key": "glm_5", "service_name": "multiplier-byo-glm-5"},
    ]:
        results = []
        for i in range(5):
            results.append(InvocationResult(
                response=f"Response from {agent['name']} for prompt {i + 1}",
                session_id=None,
                duration_ms=4500.0 + (i * 200),
                success=True,
                service_name=agent["service_name"],
                invocation_timestamp=now,
            ))
        invocation_data.append(AgentInvocationData(
            name=agent["name"],
            agent_type="byo",
            model_key=agent["model_key"],
            service_name=agent["service_name"],
            results=results,
        ))

    return invocation_data
