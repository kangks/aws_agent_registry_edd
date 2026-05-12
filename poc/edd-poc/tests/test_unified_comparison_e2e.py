"""
End-to-end tests for the unified evaluation comparison script.

Tests the full pipeline with mocked AWS services:
- subprocess.run mocked for agentcore invoke and opentelemetry-instrument
- boto3.Session mocked to provide fake bedrock-agentcore, logs, and
  bedrock-agentcore-control clients
- CloudWatch Logs Insights responses with realistic span data
- AgentCore Evaluate API responses with scores and justifications

Test infrastructure (task 10.1) provides the foundation for:
- Full pipeline happy path (task 10.2)
- Skip-invocation resume path (task 10.3)
- Partial failure scenarios (task 10.4)
- Report content validation (task 10.5)
- CLI argument parsing (task 10.6)
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
from botocore.exceptions import ClientError

# Add scripts directory to path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from run_unified_comparison import (
    UnifiedConfig,
    build_config,
    build_spans_query,
    compute_average_score,
    evaluate_agent,
    generate_unified_report,
    invoke_byo,
    invoke_managed,
    load_intermediate_state,
    main,
    parse_evaluation_response,
    parse_evaluators,
    parse_session_id,
    parse_spans_from_log_events,
    poll_query_results,
    retrieve_byo_spans,
    run_byo_agent,
    run_evaluation_phase,
    run_invocation_phase,
    run_managed_agent,
    run_wait_phase,
    save_intermediate_state,
    update_registries,
    _call_evaluate_with_retry,
    _is_throttling_error,
)
from unified_models import (
    ADOT_ENV,
    BYO_AGENTS,
    MANAGED_AGENTS,
    PROMPTS,
    AgentEvalResults,
    AgentInvocationData,
    EvaluationScore,
    IntermediateState,
    InvocationResult,
    PromptEvaluation,
    ReportMetadata,
)


# ---------------------------------------------------------------------------
# Test Infrastructure Verification Tests (Task 10.1)
# ---------------------------------------------------------------------------


class TestMockSubprocessRun:
    """Verify that the subprocess.run mock correctly handles both invocation types."""

    def test_managed_invoke_returns_session_id(self, mock_subprocess_run):
        """Mock subprocess.run returns stdout with Session: <id> for agentcore invoke."""
        cmd = ["agentcore", "invoke", "--runtime", "multiplier_hr_sonnet", "test prompt"]
        result = mock_subprocess_run(cmd, capture_output=True, text=True, timeout=120)

        assert result.returncode == 0
        assert "Session: session-multiplier_hr_sonnet-" in result.stdout
        assert result.stderr == ""

    def test_byo_invoke_returns_response(self, mock_subprocess_run):
        """Mock subprocess.run returns BYO agent response for opentelemetry-instrument."""
        cmd = [
            "opentelemetry-instrument", "python", "byo_runner.py",
            "--model", "sonnet", "--prompt", "test prompt",
        ]
        result = mock_subprocess_run(cmd, capture_output=True, text=True, timeout=300)

        assert result.returncode == 0
        assert "Agent response:" in result.stdout
        assert "[OTEL]" in result.stdout
        assert result.stderr == ""

    def test_unknown_command_returns_error(self, mock_subprocess_run):
        """Mock subprocess.run returns error for unknown commands."""
        cmd = ["unknown_command", "arg1"]
        result = mock_subprocess_run(cmd, capture_output=True, text=True)

        assert result.returncode == 1

    def test_managed_invoke_increments_counter(self, mock_subprocess_run):
        """Each managed invoke call gets a unique session ID based on counter."""
        cmd = ["agentcore", "invoke", "--runtime", "multiplier_hr_sonnet", "prompt"]

        result1 = mock_subprocess_run(cmd, capture_output=True, text=True, timeout=120)
        result2 = mock_subprocess_run(cmd, capture_output=True, text=True, timeout=120)

        # Session IDs should differ (counter increments)
        session1 = parse_session_id(result1.stdout)
        session2 = parse_session_id(result2.stdout)
        assert session1 != session2


class TestMockBoto3Session:
    """Verify that the mock boto3.Session provides all required clients."""

    def test_session_provides_agentcore_client(self, mock_boto3_session):
        """boto3.Session.client('bedrock-agentcore') returns the evaluate mock."""
        client = mock_boto3_session.client("bedrock-agentcore")
        assert hasattr(client, "evaluate")
        assert callable(client.evaluate)

    def test_session_provides_logs_client(self, mock_boto3_session):
        """boto3.Session.client('logs') returns the CloudWatch Logs mock."""
        client = mock_boto3_session.client("logs")
        assert hasattr(client, "start_query")
        assert hasattr(client, "get_query_results")

    def test_session_provides_agentcore_control_client(self, mock_boto3_session):
        """boto3.Session.client('bedrock-agentcore-control') returns the registry mock."""
        client = mock_boto3_session.client("bedrock-agentcore-control")
        assert hasattr(client, "get_registry_record")
        assert hasattr(client, "update_registry_record")


class TestMockCloudWatchLogsInsights:
    """Verify CloudWatch Logs Insights mock responses contain realistic span data."""

    def test_start_query_returns_query_id(self, mock_logs_client):
        """start_query returns a dict with queryId."""
        response = mock_logs_client.start_query(
            logGroupName="aws/spans",
            startTime=1700000000,
            endTime=1700001000,
            queryString='fields @timestamp, @message | filter @message like /"multiplier-byo-sonnet"/',
        )
        assert "queryId" in response
        assert response["queryId"].startswith("query-")

    def test_get_query_results_returns_span_data(self, mock_logs_client):
        """get_query_results returns Complete status with span data in @message fields."""
        # First call start_query to register the service name
        mock_logs_client.start_query(
            logGroupName="aws/spans",
            startTime=1700000000,
            endTime=1700001000,
            queryString='fields @timestamp, @message | filter @message like /"multiplier-byo-sonnet"/',
        )

        response = mock_logs_client.get_query_results(queryId="query-0000")

        assert response["status"] == "Complete"
        assert len(response["results"]) > 0

        # Each result row should have @timestamp and @message fields
        for row in response["results"]:
            fields = {item["field"]: item["value"] for item in row}
            assert "@timestamp" in fields
            assert "@message" in fields

            # @message should be valid JSON span data
            span = json.loads(fields["@message"])
            assert "traceId" in span
            assert "spanId" in span
            assert "name" in span

    def test_span_data_contains_service_name(self, mock_logs_client, sample_span_data):
        """Span data includes the correct service.name attribute."""
        spans = sample_span_data("multiplier-byo-sonnet", 0)

        for span in spans:
            # Check resource attributes contain service.name
            resource_attrs = span.get("resource", {}).get("attributes", [])
            service_names = [
                attr["value"]["stringValue"]
                for attr in resource_attrs
                if attr["key"] == "service.name"
            ]
            assert "multiplier-byo-sonnet" in service_names

    def test_multiple_queries_get_unique_ids(self, mock_logs_client):
        """Each start_query call returns a unique query ID."""
        query_string = 'fields @timestamp, @message | filter @message like /"multiplier-byo-sonnet"/'

        resp1 = mock_logs_client.start_query(
            logGroupName="aws/spans", startTime=1700000000,
            endTime=1700001000, queryString=query_string,
        )
        resp2 = mock_logs_client.start_query(
            logGroupName="aws/spans", startTime=1700000000,
            endTime=1700001000, queryString=query_string,
        )

        assert resp1["queryId"] != resp2["queryId"]


class TestMockAgentCoreEvaluateAPI:
    """Verify AgentCore Evaluate API mock returns scores with justifications."""

    def test_evaluate_returns_scores(self, mock_agentcore_client):
        """evaluate() returns evaluationResults with scores and justifications."""
        response = mock_agentcore_client.evaluate(
            agentRuntimeArn="arn:aws:bedrock-agentcore:us-east-1:654654616949:runtime/multiplier_hr_sonnet",
            sessionId="session-001",
            evaluatorNames=["multiplier_domain_accuracy"],
        )

        assert "evaluationResults" in response
        results = response["evaluationResults"]
        assert len(results) == 1
        assert results[0]["evaluatorName"] == "multiplier_domain_accuracy"
        assert isinstance(results[0]["score"], float)
        assert 0.0 <= results[0]["score"] <= 5.0
        assert "justification" in results[0]
        assert len(results[0]["justification"]) > 0

    def test_evaluate_tracks_calls(self, mock_agentcore_client):
        """All evaluate() calls are tracked for assertion."""
        mock_agentcore_client.evaluate(
            agentRuntimeArn="arn:aws:bedrock-agentcore:us-east-1:654654616949:runtime/test",
            sessionId="session-001",
            evaluatorNames=["multiplier_domain_accuracy"],
        )
        mock_agentcore_client.evaluate(
            sessionSpans=[{"traceId": "trace-001", "spanId": "span-001"}],
            evaluatorNames=["multiplier_domain_accuracy"],
        )

        assert len(mock_agentcore_client._evaluate_calls) == 2
        assert "agentRuntimeArn" in mock_agentcore_client._evaluate_calls[0]
        assert "sessionSpans" in mock_agentcore_client._evaluate_calls[1]

    def test_evaluate_supports_multiple_evaluators(self, mock_agentcore_client):
        """evaluate() returns scores for each evaluator in the list."""
        response = mock_agentcore_client.evaluate(
            agentRuntimeArn="arn:aws:bedrock-agentcore:us-east-1:654654616949:runtime/test",
            sessionId="session-001",
            evaluatorNames=["multiplier_domain_accuracy", "faithfulness", "coherence"],
        )

        results = response["evaluationResults"]
        assert len(results) == 3
        evaluator_names = [r["evaluatorName"] for r in results]
        assert "multiplier_domain_accuracy" in evaluator_names
        assert "faithfulness" in evaluator_names
        assert "coherence" in evaluator_names


class TestMockRegistryClient:
    """Verify bedrock-agentcore-control mock handles registry operations."""

    def test_get_registry_record_returns_descriptors(self, mock_agentcore_control_client):
        """get_registry_record returns existing custom descriptors."""
        response = mock_agentcore_control_client.get_registry_record(
            registryId="Rqbs73eeqpMEEwf9",
            recordId="rec-managed-sonnet-001",
        )

        assert "descriptors" in response
        assert "custom" in response["descriptors"]
        custom_content = json.loads(response["descriptors"]["custom"]["inlineContent"])
        assert "last_eval_score" in custom_content

    def test_update_registry_record_tracks_calls(self, mock_agentcore_control_client):
        """update_registry_record calls are tracked for assertion."""
        mock_agentcore_control_client.update_registry_record(
            registryId="Rqbs73eeqpMEEwf9",
            recordId="rec-managed-sonnet-001",
            descriptorType="CUSTOM",
            descriptors={"optionalValue": {"custom": {"optionalValue": {"inlineContent": "{}"}}}},
        )

        assert len(mock_agentcore_control_client._update_calls) == 1
        assert mock_agentcore_control_client._update_calls[0]["recordId"] == "rec-managed-sonnet-001"


class TestSampleInvocationData:
    """Verify the sample_invocation_data fixture provides correct structure."""

    def test_has_six_agents(self, sample_invocation_data):
        """Sample data contains all 6 agents (3 managed + 3 BYO)."""
        assert len(sample_invocation_data) == 6

    def test_has_three_managed_agents(self, sample_invocation_data):
        """Sample data contains 3 managed agents."""
        managed = [a for a in sample_invocation_data if a.agent_type == "managed"]
        assert len(managed) == 3

    def test_has_three_byo_agents(self, sample_invocation_data):
        """Sample data contains 3 BYO agents."""
        byo = [a for a in sample_invocation_data if a.agent_type == "byo"]
        assert len(byo) == 3

    def test_each_agent_has_five_prompts(self, sample_invocation_data):
        """Each agent has exactly 5 invocation results."""
        for agent_data in sample_invocation_data:
            assert len(agent_data.results) == 5

    def test_managed_agents_have_session_ids(self, sample_invocation_data):
        """Managed agent results have session IDs."""
        managed = [a for a in sample_invocation_data if a.agent_type == "managed"]
        for agent_data in managed:
            for result in agent_data.results:
                assert result.session_id is not None
                assert result.session_id.startswith("session-")

    def test_byo_agents_have_service_names(self, sample_invocation_data):
        """BYO agent results have service names and timestamps."""
        byo = [a for a in sample_invocation_data if a.agent_type == "byo"]
        for agent_data in byo:
            for result in agent_data.results:
                assert result.service_name is not None
                assert result.invocation_timestamp is not None

    def test_all_invocations_successful(self, sample_invocation_data):
        """All invocations in sample data are marked as successful."""
        for agent_data in sample_invocation_data:
            for result in agent_data.results:
                assert result.success is True


class TestUnifiedConfigFixture:
    """Verify the unified_config fixture provides correct test configuration."""

    def test_wait_seconds_is_zero(self, unified_config):
        """Test config uses 0s wait for fast tests."""
        assert unified_config.wait_seconds == 0

    def test_uses_test_profile(self, unified_config):
        """Test config uses a test AWS profile."""
        assert unified_config.aws_profile == "test-profile"

    def test_uses_tmp_paths(self, unified_config):
        """Test config uses /tmp paths for output files."""
        assert unified_config.intermediate_path.startswith("/tmp/")
        assert unified_config.output_path.startswith("/tmp/")

    def test_has_default_evaluator(self, unified_config):
        """Test config includes the default evaluator."""
        assert "multiplier_domain_accuracy" in unified_config.evaluators


# ---------------------------------------------------------------------------
# End-to-End Test: --skip-invocation Resume Path (Task 10.3)
# ---------------------------------------------------------------------------


class TestSkipInvocationResumePath:
    """End-to-end test for the --skip-invocation resume path.

    Validates Requirements 7.4 and 8.4:
    - --skip-invocation flag causes main() to load intermediate state from JSON
    - Invocation phase is skipped (no subprocess calls)
    - Wait phase is skipped
    - Evaluation phase runs using loaded intermediate data
    - Report is generated correctly from resumed state
    """

    @pytest.fixture
    def intermediate_state_file(self, sample_invocation_data, tmp_path):
        """Create a mock intermediate state JSON file with pre-populated invocation data."""
        from dataclasses import asdict

        state = IntermediateState(
            phase_completed="invocation",
            invocation_data=[asdict(d) for d in sample_invocation_data],
        )

        state_path = tmp_path / "unified_intermediate.json"
        with open(state_path, "w") as f:
            json.dump(asdict(state), f, indent=2, default=str)

        return str(state_path)

    @pytest.fixture
    def skip_invocation_config(self, intermediate_state_file, tmp_path):
        """Return a UnifiedConfig with skip_invocation=True pointing to the intermediate state file."""
        return UnifiedConfig(
            aws_profile="test-profile",
            aws_region="us-east-1",
            wait_seconds=0,
            evaluators=["multiplier_domain_accuracy"],
            skip_invocation=True,
            intermediate_path=intermediate_state_file,
            output_path=str(tmp_path / "test_unified_comparison.md"),
            max_workers=6,
            managed_timeout=120,
            byo_timeout=300,
        )

    def test_skip_invocation_no_subprocess_calls(
        self, skip_invocation_config, mock_boto3_session
    ):
        """Assert invocation phase is skipped — no subprocess.run calls are made."""
        with patch("run_unified_comparison.build_config", return_value=skip_invocation_config), \
             patch("run_unified_comparison.boto3.Session", return_value=mock_boto3_session), \
             patch("run_unified_comparison.subprocess.run") as mock_subproc, \
             patch("run_unified_comparison.update_registries"), \
             patch("run_unified_comparison.time.sleep"):
            main()

        # No subprocess calls should have been made (invocation phase skipped)
        mock_subproc.assert_not_called()

    def test_skip_invocation_wait_phase_skipped(
        self, skip_invocation_config, mock_boto3_session
    ):
        """Assert wait phase is skipped when --skip-invocation is set."""
        with patch("run_unified_comparison.build_config", return_value=skip_invocation_config), \
             patch("run_unified_comparison.boto3.Session", return_value=mock_boto3_session), \
             patch("run_unified_comparison.run_wait_phase") as mock_wait, \
             patch("run_unified_comparison.update_registries"), \
             patch("run_unified_comparison.time.sleep"):
            main()

        # run_wait_phase should never be called
        mock_wait.assert_not_called()

    def test_skip_invocation_evaluation_runs_with_loaded_data(
        self, skip_invocation_config, mock_boto3_session, sample_invocation_data
    ):
        """Assert evaluation phase runs using loaded intermediate data."""
        with patch("run_unified_comparison.build_config", return_value=skip_invocation_config), \
             patch("run_unified_comparison.boto3.Session", return_value=mock_boto3_session), \
             patch("run_unified_comparison.update_registries"), \
             patch("run_unified_comparison.time.sleep"):
            main()

        # Verify the evaluate API was called (evaluation phase ran)
        agentcore_client = mock_boto3_session.client("bedrock-agentcore")
        assert agentcore_client.evaluate.call_count > 0

        # Should have been called for each successful agent+prompt combination
        # 6 agents × 5 prompts = 30 evaluations expected
        # (all invocations in sample_invocation_data are successful)
        assert agentcore_client.evaluate.call_count == 30

    def test_skip_invocation_report_generated(
        self, skip_invocation_config, mock_boto3_session, tmp_path
    ):
        """Assert report is generated correctly from resumed state."""
        with patch("run_unified_comparison.build_config", return_value=skip_invocation_config), \
             patch("run_unified_comparison.boto3.Session", return_value=mock_boto3_session), \
             patch("run_unified_comparison.update_registries"), \
             patch("run_unified_comparison.time.sleep"):
            main()

        # The report should be generated at the configured output path
        # Note: generate_unified_report writes to PROJECT_ROOT/results/unified_comparison.md
        # We check that the report generation function was called by verifying
        # the report file exists at the project-level path
        project_root = Path(__file__).resolve().parent.parent
        report_path = project_root / "results" / "unified_comparison.md"

        assert report_path.exists(), f"Report not found at {report_path}"

        report_content = report_path.read_text()

        # Verify report structure
        assert "# Unified Evaluation Comparison Report" in report_content
        assert "## Metadata" in report_content
        assert "## Summary" in report_content
        assert "## Per-Model Comparison (Managed vs BYO)" in report_content
        assert "## Per-Prompt Detail" in report_content

        # Verify all 6 agents appear in the report
        assert "multiplier_hr_sonnet" in report_content
        assert "multiplier_hr_nova_2_pro" in report_content
        assert "multiplier_hr_glm_5" in report_content
        assert "multiplier_byo_sonnet" in report_content
        assert "multiplier_byo_nova_2_pro" in report_content
        assert "multiplier_byo_glm_5" in report_content

        # Verify metadata fields
        assert "multiplier_domain_accuracy" in report_content
        assert "Successful Evaluations" in report_content

    def test_skip_invocation_loads_correct_agent_count(
        self, skip_invocation_config, mock_boto3_session, capsys
    ):
        """Assert that 6 agents are loaded from intermediate state."""
        with patch("run_unified_comparison.build_config", return_value=skip_invocation_config), \
             patch("run_unified_comparison.boto3.Session", return_value=mock_boto3_session), \
             patch("run_unified_comparison.update_registries"), \
             patch("run_unified_comparison.time.sleep"):
            main()

        captured = capsys.readouterr()
        # main() prints "Loaded 6 agents from intermediate state"
        assert "Loaded 6 agents from intermediate state" in captured.out

    def test_skip_invocation_prints_skip_message(
        self, skip_invocation_config, mock_boto3_session, capsys
    ):
        """Assert that the skip message is printed when --skip-invocation is set."""
        with patch("run_unified_comparison.build_config", return_value=skip_invocation_config), \
             patch("run_unified_comparison.boto3.Session", return_value=mock_boto3_session), \
             patch("run_unified_comparison.update_registries"), \
             patch("run_unified_comparison.time.sleep"):
            main()

        captured = capsys.readouterr()
        assert "[SKIP] Invocation and wait phases skipped (--skip-invocation)" in captured.out

    def test_skip_invocation_managed_agents_evaluated_with_session_ids(
        self, skip_invocation_config, mock_boto3_session
    ):
        """Assert managed agents are evaluated using session IDs from intermediate state."""
        with patch("run_unified_comparison.build_config", return_value=skip_invocation_config), \
             patch("run_unified_comparison.boto3.Session", return_value=mock_boto3_session), \
             patch("run_unified_comparison.update_registries"), \
             patch("run_unified_comparison.time.sleep"):
            main()

        agentcore_client = mock_boto3_session.client("bedrock-agentcore")
        evaluate_calls = agentcore_client._evaluate_calls

        # Find calls with agentRuntimeArn (managed agent evaluations)
        managed_calls = [c for c in evaluate_calls if "agentRuntimeArn" in c]
        # 3 managed agents × 5 prompts = 15 managed evaluations
        assert len(managed_calls) == 15

        # Each managed call should have a sessionId
        for call_kwargs in managed_calls:
            assert "sessionId" in call_kwargs
            assert call_kwargs["sessionId"] is not None
            assert call_kwargs["sessionId"].startswith("session-")

    def test_skip_invocation_byo_agents_evaluated_with_spans(
        self, skip_invocation_config, mock_boto3_session
    ):
        """Assert BYO agents are evaluated using spans retrieved from CloudWatch."""
        with patch("run_unified_comparison.build_config", return_value=skip_invocation_config), \
             patch("run_unified_comparison.boto3.Session", return_value=mock_boto3_session), \
             patch("run_unified_comparison.update_registries"), \
             patch("run_unified_comparison.time.sleep"):
            main()

        agentcore_client = mock_boto3_session.client("bedrock-agentcore")
        evaluate_calls = agentcore_client._evaluate_calls

        # Find calls with sessionSpans (BYO agent evaluations)
        byo_calls = [c for c in evaluate_calls if "sessionSpans" in c]
        # 3 BYO agents × 5 prompts = 15 BYO evaluations
        assert len(byo_calls) == 15

        # Each BYO call should have sessionSpans with span data
        for call_kwargs in byo_calls:
            assert isinstance(call_kwargs["sessionSpans"], list)
            assert len(call_kwargs["sessionSpans"]) > 0

    def test_skip_invocation_cloudwatch_queried_for_byo_spans(
        self, skip_invocation_config, mock_boto3_session
    ):
        """Assert CloudWatch Logs Insights is queried for BYO agent spans."""
        with patch("run_unified_comparison.build_config", return_value=skip_invocation_config), \
             patch("run_unified_comparison.boto3.Session", return_value=mock_boto3_session), \
             patch("run_unified_comparison.update_registries"), \
             patch("run_unified_comparison.time.sleep"):
            main()

        logs_client = mock_boto3_session.client("logs")

        # start_query should have been called for BYO agent span retrieval
        # 3 BYO agents × 5 prompts = 15 queries
        assert logs_client.start_query.call_count == 15

    def test_skip_invocation_final_summary_printed(
        self, skip_invocation_config, mock_boto3_session, capsys
    ):
        """Assert final summary is printed with correct counts."""
        with patch("run_unified_comparison.build_config", return_value=skip_invocation_config), \
             patch("run_unified_comparison.boto3.Session", return_value=mock_boto3_session), \
             patch("run_unified_comparison.update_registries"), \
             patch("run_unified_comparison.time.sleep"):
            main()

        captured = capsys.readouterr()

        # Final summary should show all 6 agents evaluated
        assert "Total agents evaluated:" in captured.out
        assert "6" in captured.out
        # Total prompts scored should be 30 (6 agents × 5 prompts)
        assert "Total prompts scored:" in captured.out
        assert "30" in captured.out


# ---------------------------------------------------------------------------
# CLI Argument Parsing and Configuration Tests (Task 10.6)
# ---------------------------------------------------------------------------


class TestCLIArgumentParsing:
    """End-to-end tests for CLI argument parsing and configuration via build_config.

    Validates: Requirements 7.1, 7.2, 7.3, 7.4
    """

    def test_wait_seconds_override(self):
        """--wait-seconds 300 sets config.wait_seconds to 300."""
        with patch("sys.argv", ["run_unified_comparison.py", "--wait-seconds", "300"]):
            config = build_config()
        assert config.wait_seconds == 300

    def test_evaluators_comma_separated(self):
        """--evaluators "a,b,c" sets config.evaluators to ["a", "b", "c"]."""
        with patch("sys.argv", ["run_unified_comparison.py", "--evaluators", "a,b,c"]):
            config = build_config()
        assert config.evaluators == ["a", "b", "c"]

    def test_evaluators_trims_whitespace(self):
        """--evaluators "a, b , c" trims whitespace correctly."""
        with patch("sys.argv", ["run_unified_comparison.py", "--evaluators", "a, b , c"]):
            config = build_config()
        assert config.evaluators == ["a", "b", "c"]

    def test_skip_invocation_flag(self):
        """--skip-invocation sets config.skip_invocation to True."""
        with patch("sys.argv", ["run_unified_comparison.py", "--skip-invocation"]):
            config = build_config()
        assert config.skip_invocation is True

    def test_env_vars_aws_profile_and_region(self):
        """AWS_PROFILE=custom and AWS_REGION=eu-west-1 env vars are read correctly."""
        env_overrides = {"AWS_PROFILE": "custom", "AWS_REGION": "eu-west-1"}
        with patch("sys.argv", ["run_unified_comparison.py"]), \
             patch.dict(os.environ, env_overrides, clear=False):
            config = build_config()
        assert config.aws_profile == "custom"
        assert config.aws_region == "eu-west-1"

    def test_defaults(self):
        """Default values: wait_seconds=120, evaluators=["multiplier_domain_accuracy"], skip_invocation=False, aws_profile="ml-sandbox", aws_region="us-east-1"."""
        # Clear AWS_PROFILE and AWS_REGION to ensure defaults are used
        env_clean = {k: v for k, v in os.environ.items() if k not in ("AWS_PROFILE", "AWS_REGION")}
        with patch("sys.argv", ["run_unified_comparison.py"]), \
             patch.dict(os.environ, env_clean, clear=True):
            config = build_config()
        assert config.wait_seconds == 120
        assert config.evaluators == ["multiplier_domain_accuracy"]
        assert config.skip_invocation is False
        assert config.aws_profile == "ml-sandbox"
        assert config.aws_region == "us-east-1"


class TestParseEvaluators:
    """Unit tests for the parse_evaluators helper function.

    Validates: Requirements 7.3
    """

    def test_single_evaluator(self):
        """Single evaluator name is returned as a one-element list."""
        assert parse_evaluators("multiplier_domain_accuracy") == ["multiplier_domain_accuracy"]

    def test_multiple_evaluators(self):
        """Comma-separated evaluators are split into a list."""
        assert parse_evaluators("a,b,c") == ["a", "b", "c"]

    def test_whitespace_trimming(self):
        """Whitespace around evaluator names is trimmed."""
        assert parse_evaluators("a, b , c") == ["a", "b", "c"]

    def test_empty_segments_excluded(self):
        """Empty segments from trailing commas or double commas are excluded."""
        assert parse_evaluators("a,,b,") == ["a", "b"]

    def test_empty_string_returns_empty_list(self):
        """Empty string returns an empty list."""
        assert parse_evaluators("") == []


# ---------------------------------------------------------------------------
# End-to-End Full Pipeline Happy Path Tests (Task 10.2)
# ---------------------------------------------------------------------------


class TestFullPipelineHappyPath:
    """End-to-end test for the full pipeline: invocation → wait → evaluation → report.

    Validates Requirements: 1.1–1.7, 2.1–2.3, 3.1–3.5, 4.1–4.6, 5.1–5.6, 6.1–6.4, 7.1–7.5, 8.5
    """

    @pytest.fixture
    def pipeline_mocks(
        self,
        mock_subprocess_run,
        mock_boto3_session,
        mock_agentcore_client,
        mock_logs_client,
        mock_agentcore_control_client,
        tmp_path,
    ):
        """Set up all mocks needed for a full pipeline run.

        Returns a dict with all mock objects and paths for assertions.
        """
        intermediate_path = str(tmp_path / "unified_intermediate.json")
        output_path = str(tmp_path / "unified_comparison.md")
        registry_info_path = tmp_path / "registry_info.json"

        # Write a mock registry_info.json
        registry_info = {
            "records": [
                {"name": "multiplier-hr-sonnet-managed", "record_id": "rec-managed-sonnet-001"},
                {"name": "multiplier-hr-nova-2-pro-managed", "record_id": "rec-managed-nova-001"},
                {"name": "multiplier-hr-glm-5-managed", "record_id": "rec-managed-glm-001"},
                {"name": "multiplier-hr-sonnet-byo", "record_id": "rec-byo-sonnet-001"},
                {"name": "multiplier-hr-nova-2-pro-byo", "record_id": "rec-byo-nova-001"},
                {"name": "multiplier-hr-glm-5-byo", "record_id": "rec-byo-glm-001"},
            ]
        }
        registry_info_path.write_text(json.dumps(registry_info))

        return {
            "subprocess_run": mock_subprocess_run,
            "boto3_session": mock_boto3_session,
            "agentcore_client": mock_agentcore_client,
            "logs_client": mock_logs_client,
            "control_client": mock_agentcore_control_client,
            "intermediate_path": intermediate_path,
            "output_path": output_path,
            "registry_info_path": registry_info_path,
            "tmp_path": tmp_path,
        }

    @pytest.fixture
    def run_full_pipeline(self, pipeline_mocks):
        """Execute main() with all mocks and return captured output + mocks.

        Patches:
        - sys.argv for CLI args (0s wait)
        - subprocess.run for agent invocations
        - boto3.Session for AWS clients
        - time.sleep to avoid delays
        - File paths for report and intermediate state
        - agent_registry module for local registry update
        """
        mocks = pipeline_mocks
        captured_prints = []

        # Mock the local registry update function
        mock_update_eval_results = MagicMock(return_value=True)

        # Track intermediate state saves
        intermediate_saves = []
        original_save = save_intermediate_state

        def _mock_save_intermediate(state, path):
            intermediate_saves.append({"state": state, "path": path})
            # Save to tmp_path instead of the real path
            tmp_intermediate = Path(mocks["tmp_path"]) / "unified_intermediate.json"
            tmp_intermediate.parent.mkdir(parents=True, exist_ok=True)
            import json as _json
            from dataclasses import asdict as _asdict
            with open(tmp_intermediate, "w") as f:
                _json.dump(_asdict(state), f, indent=2, default=str)

        with (
            patch("sys.argv", ["run_unified_comparison.py", "--wait-seconds", "0"]),
            patch("run_unified_comparison.subprocess.run", side_effect=mocks["subprocess_run"]),
            patch("run_unified_comparison.boto3.Session", return_value=mocks["boto3_session"]),
            patch("run_unified_comparison.time.sleep"),
            patch(
                "run_unified_comparison.save_intermediate_state",
                side_effect=_mock_save_intermediate,
            ),
            patch(
                "run_unified_comparison.REGISTRY_INFO_PATH",
                mocks["registry_info_path"],
            ),
            patch(
                "run_unified_comparison.PROJECT_ROOT",
                mocks["tmp_path"],
            ),
            patch.dict("sys.modules", {"agent_registry": MagicMock(
                update_eval_results=mock_update_eval_results,
            )}),
            patch("builtins.print", side_effect=lambda *args, **kwargs: captured_prints.append(" ".join(str(a) for a in args))),
        ):
            main()

        mocks["captured_prints"] = captured_prints
        mocks["mock_update_eval_results"] = mock_update_eval_results
        mocks["intermediate_saves"] = intermediate_saves
        return mocks

    def test_all_six_agents_invoked(self, run_full_pipeline):
        """All 6 agents are invoked: 3 managed via agentcore invoke, 3 BYO via otel-instrument.

        Validates: Requirements 1.1, 1.2, 1.3
        """
        mocks = run_full_pipeline
        # The subprocess mock tracks managed and BYO calls via the side_effect
        # We can verify by checking the agentcore evaluate calls which happen
        # for all 6 agents × 5 prompts = 30 total evaluations
        evaluate_calls = mocks["agentcore_client"]._evaluate_calls

        # All 6 agents should have been evaluated (30 total calls: 6 agents × 5 prompts)
        assert len(evaluate_calls) == 30

    def test_managed_agents_invoked_via_agentcore(self, run_full_pipeline):
        """3 managed agents are invoked via agentcore invoke CLI.

        Validates: Requirements 1.1, 1.4
        """
        mocks = run_full_pipeline
        evaluate_calls = mocks["agentcore_client"]._evaluate_calls

        # Managed agent evaluations use agentRuntimeArn + sessionId
        managed_calls = [c for c in evaluate_calls if "agentRuntimeArn" in c]
        # 3 managed agents × 5 prompts = 15 managed evaluation calls
        assert len(managed_calls) == 15

        # Verify all 3 managed runtimes are represented
        runtime_arns = {c["agentRuntimeArn"] for c in managed_calls}
        assert any("multiplier_hr_sonnet" in arn for arn in runtime_arns)
        assert any("multiplier_hr_nova_2_pro" in arn for arn in runtime_arns)
        assert any("multiplier_hr_glm_5" in arn for arn in runtime_arns)

    def test_byo_agents_invoked_via_otel_instrument(self, run_full_pipeline):
        """3 BYO agents are invoked via opentelemetry-instrument.

        Validates: Requirements 1.2, 1.5
        """
        mocks = run_full_pipeline
        evaluate_calls = mocks["agentcore_client"]._evaluate_calls

        # BYO agent evaluations use sessionSpans
        byo_calls = [c for c in evaluate_calls if "sessionSpans" in c]
        # 3 BYO agents × 5 prompts = 15 BYO evaluation calls
        assert len(byo_calls) == 15

    def test_intermediate_state_saved_after_invocation(self, run_full_pipeline):
        """Intermediate state is saved after invocation phase.

        Validates: Requirements 8.4
        """
        mocks = run_full_pipeline
        intermediate_saves = mocks["intermediate_saves"]

        # The pipeline saves intermediate state after invocation, wait, and evaluation phases
        assert len(intermediate_saves) >= 2

        # First save should be after invocation phase
        phases_saved = [s["state"].phase_completed for s in intermediate_saves]
        assert "invocation" in phases_saved

        # Invocation save should include invocation_data
        invocation_save = next(s for s in intermediate_saves if s["state"].phase_completed == "invocation")
        assert invocation_save["state"].invocation_data is not None
        assert len(invocation_save["state"].invocation_data) == 6  # All 6 agents

    def test_cloudwatch_logs_queried_for_each_byo_agent(self, run_full_pipeline):
        """CloudWatch Logs Insights is queried for each BYO agent.

        Validates: Requirements 3.1, 3.2, 3.3
        """
        mocks = run_full_pipeline
        logs_client = mocks["logs_client"]

        # start_query should have been called for BYO agents
        # 3 BYO agents × 5 prompts = 15 queries (minimum)
        assert logs_client.start_query.call_count >= 15

        # Verify all 3 BYO service names were queried
        queried_services = logs_client._queried_services
        assert "multiplier-byo-sonnet" in queried_services
        assert "multiplier-byo-nova-2-pro" in queried_services
        assert "multiplier-byo-glm-5" in queried_services

    def test_evaluate_api_called_for_all_agent_prompt_combinations(self, run_full_pipeline):
        """AgentCore Evaluate API is called for each agent+prompt combination (30 total).

        Validates: Requirements 4.1, 4.2, 4.3, 4.6
        """
        mocks = run_full_pipeline
        evaluate_calls = mocks["agentcore_client"]._evaluate_calls

        # 6 agents × 5 prompts = 30 total evaluation calls
        assert len(evaluate_calls) == 30

        # All calls should include evaluatorNames
        for call_params in evaluate_calls:
            assert "evaluatorNames" in call_params
            assert "multiplier_domain_accuracy" in call_params["evaluatorNames"]

    def test_report_generated_with_correct_structure(self, run_full_pipeline):
        """results/unified_comparison.md is generated with correct structure.

        Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5
        """
        mocks = run_full_pipeline
        report_path = Path(mocks["tmp_path"]) / "results" / "unified_comparison.md"

        assert report_path.exists()
        report_content = report_path.read_text()

        # Section 1: Metadata
        assert "## Metadata" in report_content
        assert "**Generated:**" in report_content
        assert "**Evaluators:**" in report_content
        assert "multiplier_domain_accuracy" in report_content
        assert "**Successful Evaluations:**" in report_content
        assert "**Total Execution Time:**" in report_content

        # Section 2: Summary table with correct columns
        assert "## Summary" in report_content
        assert "| Agent Name | Deployment Type | Model | Average Score | Per-Prompt Scores |" in report_content

        # All 6 agents should appear in the summary table
        assert "multiplier_hr_sonnet" in report_content
        assert "multiplier_hr_nova_2_pro" in report_content
        assert "multiplier_hr_glm_5" in report_content
        assert "multiplier_byo_sonnet" in report_content
        assert "multiplier_byo_nova_2_pro" in report_content
        assert "multiplier_byo_glm_5" in report_content

        # Section 3: Per-model comparison
        assert "## Per-Model Comparison (Managed vs BYO)" in report_content
        assert "### Model: sonnet" in report_content
        assert "### Model: nova_2_pro" in report_content
        assert "### Model: glm_5" in report_content

        # Section 4: Per-prompt detail
        assert "## Per-Prompt Detail" in report_content

    def test_registry_update_attempted_for_all_agents(self, run_full_pipeline):
        """Registry update is attempted for all 6 agents.

        Validates: Requirements 6.1, 6.2, 6.3, 6.4
        """
        mocks = run_full_pipeline
        control_client = mocks["control_client"]

        # AWS registry update should be attempted for all 6 agents
        update_calls = control_client._update_calls
        assert len(update_calls) == 6

        # Verify all record IDs are present
        updated_record_ids = {c["recordId"] for c in update_calls}
        expected_record_ids = {
            "rec-managed-sonnet-001",
            "rec-managed-nova-001",
            "rec-managed-glm-001",
            "rec-byo-sonnet-001",
            "rec-byo-nova-001",
            "rec-byo-glm-001",
        }
        assert updated_record_ids == expected_record_ids

    def test_local_registry_update_attempted_for_all_agents(self, run_full_pipeline):
        """Local registry update is attempted for all 6 agents.

        Validates: Requirements 6.1, 6.3
        """
        mocks = run_full_pipeline
        mock_update = mocks["mock_update_eval_results"]

        # Local registry should be updated for all 6 agents
        assert mock_update.call_count == 6

        # Verify evaluator name is "unified_comparison"
        for call_args in mock_update.call_args_list:
            args = call_args[0]
            # update_eval_results(agent_name, scores_dict, timestamp, evaluator_name)
            evaluator_name = args[3]
            assert evaluator_name == "unified_comparison"

    def test_final_summary_printed_with_correct_counts(self, run_full_pipeline):
        """Final summary is printed with correct counts.

        Validates: Requirements 8.5
        """
        mocks = run_full_pipeline
        captured = mocks["captured_prints"]
        all_output = "\n".join(captured)

        # Final summary should include key metrics
        assert "Total agents evaluated:" in all_output
        assert "Total prompts scored:" in all_output
        assert "Avg score (managed):" in all_output
        assert "Avg score (BYO):" in all_output
        assert "Total wall-clock time:" in all_output

        # Should show 6 agents evaluated
        assert "6" in all_output

    def test_managed_evaluations_use_runtime_arn_and_session_id(self, run_full_pipeline):
        """Managed agent evaluations pass agentRuntimeArn and sessionId.

        Validates: Requirements 4.1
        """
        mocks = run_full_pipeline
        evaluate_calls = mocks["agentcore_client"]._evaluate_calls

        managed_calls = [c for c in evaluate_calls if "agentRuntimeArn" in c]
        for call_params in managed_calls:
            assert "agentRuntimeArn" in call_params
            assert "sessionId" in call_params
            # ARN should contain the runtime name
            assert "arn:aws:bedrock-agentcore:" in call_params["agentRuntimeArn"]
            # Session ID should be non-empty
            assert call_params["sessionId"] is not None
            assert len(call_params["sessionId"]) > 0

    def test_byo_evaluations_use_session_spans(self, run_full_pipeline):
        """BYO agent evaluations pass sessionSpans from CloudWatch.

        Validates: Requirements 4.2
        """
        mocks = run_full_pipeline
        evaluate_calls = mocks["agentcore_client"]._evaluate_calls

        byo_calls = [c for c in evaluate_calls if "sessionSpans" in c]
        for call_params in byo_calls:
            assert "sessionSpans" in call_params
            # Spans should be a non-empty list
            assert isinstance(call_params["sessionSpans"], list)
            assert len(call_params["sessionSpans"]) > 0

    def test_report_contains_per_prompt_scores(self, run_full_pipeline):
        """Report summary table contains per-prompt scores for each agent.

        Validates: Requirements 5.2
        """
        mocks = run_full_pipeline
        report_path = Path(mocks["tmp_path"]) / "results" / "unified_comparison.md"
        report_content = report_path.read_text()

        # Per-prompt scores should appear as P1:X.X P2:X.X etc.
        assert "P1:" in report_content
        assert "P2:" in report_content
        assert "P3:" in report_content
        assert "P4:" in report_content
        assert "P5:" in report_content

    def test_report_metadata_includes_execution_time(self, run_full_pipeline):
        """Report metadata includes total execution time.

        Validates: Requirements 5.4
        """
        mocks = run_full_pipeline
        report_path = Path(mocks["tmp_path"]) / "results" / "unified_comparison.md"
        report_content = report_path.read_text()

        # Execution time should be a number followed by 's'
        assert "**Total Execution Time:**" in report_content
        # Should contain a numeric value (e.g., "0.1s")
        import re
        match = re.search(r"\*\*Total Execution Time:\*\*\s+[\d.]+s", report_content)
        assert match is not None

    def test_aws_registry_updates_include_unified_comparison_evaluator(self, run_full_pipeline):
        """AWS registry updates record evaluator as 'unified_comparison'.

        Validates: Requirements 6.3
        """
        mocks = run_full_pipeline
        control_client = mocks["control_client"]

        for update_call in control_client._update_calls:
            # Extract the inline content from the nested descriptor structure
            descriptors = update_call.get("descriptors", {})
            inline_content = (
                descriptors.get("optionalValue", {})
                .get("custom", {})
                .get("optionalValue", {})
                .get("inlineContent", "{}")
            )
            metadata = json.loads(inline_content)
            assert metadata.get("last_eval_details", {}).get("evaluator") == "unified_comparison"


# ---------------------------------------------------------------------------
# Report Content Validation Tests (Task 10.5)
# ---------------------------------------------------------------------------


class TestReportContentValidation:
    """End-to-end tests for report content validation.

    Generates a report from known evaluation results with fixed/deterministic
    scores and justifications, then asserts the Markdown output contains the
    expected structure, columns, groupings, and computed values.

    Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 6.1
    """

    @pytest.fixture
    def fixed_evaluation_results(self) -> dict[str, AgentEvalResults]:
        """Build evaluation results with fixed, deterministic scores.

        Creates 6 agents (3 managed + 3 BYO) with 5 prompts each.
        Scores are fixed so assertions can be exact.
        """
        agents = [
            ("multiplier_hr_sonnet", "managed", "sonnet"),
            ("multiplier_hr_nova_2_pro", "managed", "nova_2_pro"),
            ("multiplier_hr_glm_5", "managed", "glm_5"),
            ("multiplier_byo_sonnet", "byo", "sonnet"),
            ("multiplier_byo_nova_2_pro", "byo", "nova_2_pro"),
            ("multiplier_byo_glm_5", "byo", "glm_5"),
        ]

        # Fixed scores per agent per prompt (agent_idx * 5 + prompt_idx)
        fixed_scores = [
            [4.0, 3.5, 5.0, 4.5, 3.0],  # multiplier_hr_sonnet
            [3.0, 4.0, 3.5, 4.0, 4.5],  # multiplier_hr_nova_2_pro
            [5.0, 5.0, 4.0, 3.5, 4.0],  # multiplier_hr_glm_5
            [4.5, 4.0, 3.0, 5.0, 4.0],  # multiplier_byo_sonnet
            [3.5, 3.0, 4.5, 4.0, 3.5],  # multiplier_byo_nova_2_pro
            [4.0, 4.5, 5.0, 3.0, 3.5],  # multiplier_byo_glm_5
        ]

        fixed_justifications = [
            "Accurate response with correct domain knowledge",
            "Partially correct but missing some details",
            "Excellent comprehensive answer",
            "Good response with minor inaccuracies",
            "Basic response lacking depth",
        ]

        results: dict[str, AgentEvalResults] = {}

        for agent_idx, (name, agent_type, model_key) in enumerate(agents):
            prompt_evaluations = []
            for prompt_idx in range(5):
                score_val = fixed_scores[agent_idx][prompt_idx]
                justification = fixed_justifications[prompt_idx]

                prompt_evaluations.append(
                    PromptEvaluation(
                        prompt_index=prompt_idx,
                        prompt_text=PROMPTS[prompt_idx],
                        scores=[
                            EvaluationScore(
                                evaluator_name="multiplier_domain_accuracy",
                                score=score_val,
                                justification=justification,
                            )
                        ],
                        success=True,
                    )
                )

            results[name] = AgentEvalResults(
                agent_name=name,
                agent_type=agent_type,
                model_key=model_key,
                prompt_evaluations=prompt_evaluations,
            )

        return results

    @pytest.fixture
    def fixed_metadata(self) -> ReportMetadata:
        """Build fixed metadata for deterministic report assertions."""
        return ReportMetadata(
            timestamp="2024-01-15 10:30:00",
            evaluator_names=["multiplier_domain_accuracy"],
            successful_evaluations=30,
            total_evaluations=30,
            total_execution_time_seconds=245.7,
        )

    @pytest.fixture
    def generated_report(self, fixed_evaluation_results, fixed_metadata, tmp_path, monkeypatch) -> str:
        """Generate a report from fixed evaluation results and return the markdown string."""
        # Monkeypatch PROJECT_ROOT so the report writes to tmp_path
        import run_unified_comparison as module
        monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)

        report = generate_unified_report(fixed_evaluation_results, fixed_metadata)
        return report

    # -------------------------------------------------------------------
    # Summary Table Tests (Requirement 5.2)
    # -------------------------------------------------------------------

    def test_summary_table_has_correct_columns(self, generated_report):
        """Summary table has columns: Agent Name, Deployment Type, Model, Average Score, Per-Prompt Scores."""
        assert "| Agent Name | Deployment Type | Model | Average Score | Per-Prompt Scores |" in generated_report

    def test_summary_table_contains_all_agents(self, generated_report):
        """Summary table contains rows for all 6 agents."""
        expected_agents = [
            "multiplier_hr_sonnet",
            "multiplier_hr_nova_2_pro",
            "multiplier_hr_glm_5",
            "multiplier_byo_sonnet",
            "multiplier_byo_nova_2_pro",
            "multiplier_byo_glm_5",
        ]
        for agent_name in expected_agents:
            assert agent_name in generated_report

    def test_summary_table_shows_deployment_types(self, generated_report):
        """Summary table shows correct deployment type for each agent."""
        # Managed agents should have "managed" in their row
        lines = generated_report.split("\n")
        for line in lines:
            if "multiplier_hr_sonnet" in line and "|" in line:
                assert "managed" in line
            if "multiplier_byo_sonnet" in line and "|" in line:
                assert "byo" in line

    def test_summary_table_shows_model_keys(self, generated_report):
        """Summary table shows correct model key for each agent."""
        lines = generated_report.split("\n")
        for line in lines:
            if "multiplier_hr_sonnet" in line and "| managed |" in line:
                assert "sonnet" in line
            if "multiplier_hr_nova_2_pro" in line and "| managed |" in line:
                assert "nova_2_pro" in line
            if "multiplier_hr_glm_5" in line and "| managed |" in line:
                assert "glm_5" in line

    def test_summary_table_per_prompt_scores(self, generated_report):
        """Summary table shows per-prompt scores for each agent."""
        # multiplier_hr_sonnet has scores [4.0, 3.5, 5.0, 4.5, 3.0]
        lines = generated_report.split("\n")
        for line in lines:
            if "multiplier_hr_sonnet" in line and "| managed |" in line:
                assert "P1:4.0" in line
                assert "P2:3.5" in line
                assert "P3:5.0" in line
                assert "P4:4.5" in line
                assert "P5:3.0" in line
                break
        else:
            pytest.fail("multiplier_hr_sonnet row not found in summary table")

    # -------------------------------------------------------------------
    # Per-Model Comparison Tests (Requirement 5.5)
    # -------------------------------------------------------------------

    def test_per_model_comparison_section_exists(self, generated_report):
        """Report contains a per-model comparison section."""
        assert "## Per-Model Comparison (Managed vs BYO)" in generated_report

    def test_per_model_groups_sonnet(self, generated_report):
        """Per-model comparison groups sonnet agents (managed + BYO)."""
        assert "### Model: sonnet" in generated_report
        # Both sonnet agents should appear under this heading
        sonnet_section = generated_report.split("### Model: sonnet")[1].split("### Model:")[0]
        assert "multiplier_hr_sonnet" in sonnet_section
        assert "multiplier_byo_sonnet" in sonnet_section

    def test_per_model_groups_nova_2_pro(self, generated_report):
        """Per-model comparison groups nova_2_pro agents (managed + BYO)."""
        assert "### Model: nova_2_pro" in generated_report
        nova_section = generated_report.split("### Model: nova_2_pro")[1].split("### Model:")[0]
        assert "multiplier_hr_nova_2_pro" in nova_section
        assert "multiplier_byo_nova_2_pro" in nova_section

    def test_per_model_groups_glm_5(self, generated_report):
        """Per-model comparison groups glm_5 agents (managed + BYO)."""
        assert "### Model: glm_5" in generated_report
        glm_section = generated_report.split("### Model: glm_5")[1].split("###")[0]
        assert "multiplier_hr_glm_5" in glm_section
        assert "multiplier_byo_glm_5" in glm_section

    def test_per_model_comparison_shows_deployment_types(self, generated_report):
        """Per-model comparison table shows deployment type for each agent."""
        # Each model section should have a table with Deployment Type column
        assert "| Agent Name | Deployment Type | Average Score |" in generated_report

    # -------------------------------------------------------------------
    # Per-Prompt Detail Tests (Requirement 5.3)
    # -------------------------------------------------------------------

    def test_per_prompt_detail_section_exists(self, generated_report):
        """Report contains a per-prompt detail section."""
        assert "## Per-Prompt Detail" in generated_report

    def test_per_prompt_detail_includes_scores(self, generated_report):
        """Per-prompt detail includes score for each agent-prompt combination."""
        # multiplier_hr_sonnet prompt 1 should have score 4.00
        detail_section = generated_report.split("## Per-Prompt Detail")[1]
        sonnet_section = detail_section.split("### multiplier_hr_sonnet")[1].split("###")[0]
        assert "4.00" in sonnet_section  # First prompt score

    def test_per_prompt_detail_includes_justifications(self, generated_report):
        """Per-prompt detail includes justification for each agent-prompt combination."""
        detail_section = generated_report.split("## Per-Prompt Detail")[1]
        # Check that justifications appear in the detail section
        assert "Accurate response with correct domain knowledge" in detail_section
        assert "Partially correct but missing some details" in detail_section
        assert "Excellent comprehensive answer" in detail_section

    def test_per_prompt_detail_includes_evaluator_name(self, generated_report):
        """Per-prompt detail shows the evaluator name for each score."""
        detail_section = generated_report.split("## Per-Prompt Detail")[1]
        assert "multiplier_domain_accuracy" in detail_section

    def test_per_prompt_detail_includes_prompt_text(self, generated_report):
        """Per-prompt detail includes the prompt text."""
        detail_section = generated_report.split("## Per-Prompt Detail")[1]
        # Check that prompt texts appear
        assert "What is the employment status of employee EMP-12345 in Singapore?" in detail_section
        assert "What are the notice periods and regulatory requirements for employees in Thailand?" in detail_section

    def test_per_prompt_detail_all_agents_present(self, generated_report):
        """Per-prompt detail has a subsection for each agent."""
        detail_section = generated_report.split("## Per-Prompt Detail")[1]
        expected_agents = [
            "multiplier_hr_sonnet",
            "multiplier_hr_nova_2_pro",
            "multiplier_hr_glm_5",
            "multiplier_byo_sonnet",
            "multiplier_byo_nova_2_pro",
            "multiplier_byo_glm_5",
        ]
        for agent_name in expected_agents:
            assert agent_name in detail_section

    # -------------------------------------------------------------------
    # Metadata Section Tests (Requirement 5.4)
    # -------------------------------------------------------------------

    def test_metadata_section_exists(self, generated_report):
        """Report contains a metadata section."""
        assert "## Metadata" in generated_report

    def test_metadata_includes_timestamp(self, generated_report):
        """Metadata section includes the generation timestamp."""
        assert "2024-01-15 10:30:00" in generated_report

    def test_metadata_includes_evaluator_names(self, generated_report):
        """Metadata section includes the evaluator names."""
        metadata_section = generated_report.split("## Metadata")[1].split("##")[0]
        assert "multiplier_domain_accuracy" in metadata_section

    def test_metadata_includes_successful_total_counts(self, generated_report):
        """Metadata section includes successful/total evaluation counts."""
        metadata_section = generated_report.split("## Metadata")[1].split("##")[0]
        assert "30 / 30" in metadata_section

    def test_metadata_includes_execution_time(self, generated_report):
        """Metadata section includes total execution time."""
        metadata_section = generated_report.split("## Metadata")[1].split("##")[0]
        assert "245.7s" in metadata_section

    # -------------------------------------------------------------------
    # Failures Section Tests (Requirement 5.6)
    # -------------------------------------------------------------------

    def test_no_failures_section_when_all_succeed(self, generated_report):
        """Failures section does NOT appear when all evaluations succeed."""
        assert "## Failures" not in generated_report

    def test_failures_section_appears_when_failures_exist(self, fixed_metadata, tmp_path, monkeypatch):
        """Failures section appears when some evaluations failed."""
        import run_unified_comparison as module
        monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)

        # Create results with one failure
        results: dict[str, AgentEvalResults] = {
            "multiplier_hr_sonnet": AgentEvalResults(
                agent_name="multiplier_hr_sonnet",
                agent_type="managed",
                model_key="sonnet",
                prompt_evaluations=[
                    PromptEvaluation(
                        prompt_index=0,
                        prompt_text=PROMPTS[0],
                        scores=[EvaluationScore(evaluator_name="multiplier_domain_accuracy", score=4.0)],
                        success=True,
                    ),
                    PromptEvaluation(
                        prompt_index=1,
                        prompt_text=PROMPTS[1],
                        scores=[],
                        success=False,
                        error="Timeout after 120s",
                    ),
                ],
            ),
        }

        report = generate_unified_report(results, fixed_metadata)
        assert "## Failures" in report

    def test_failures_section_lists_correct_details(self, fixed_metadata, tmp_path, monkeypatch):
        """Failures section lists agent name, prompt index, and failure reason."""
        import run_unified_comparison as module
        monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)

        results: dict[str, AgentEvalResults] = {
            "multiplier_hr_sonnet": AgentEvalResults(
                agent_name="multiplier_hr_sonnet",
                agent_type="managed",
                model_key="sonnet",
                prompt_evaluations=[
                    PromptEvaluation(
                        prompt_index=0,
                        prompt_text=PROMPTS[0],
                        scores=[EvaluationScore(evaluator_name="multiplier_domain_accuracy", score=4.0)],
                        success=True,
                    ),
                    PromptEvaluation(
                        prompt_index=2,
                        prompt_text=PROMPTS[2],
                        scores=[],
                        success=False,
                        error="No spans retrieved from CloudWatch",
                    ),
                ],
            ),
        }

        report = generate_unified_report(results, fixed_metadata)
        failures_section = report.split("## Failures")[1]
        assert "multiplier_hr_sonnet" in failures_section
        assert "3" in failures_section  # prompt_index 2 displayed as 3 (1-indexed)
        assert "No spans retrieved from CloudWatch" in failures_section

    # -------------------------------------------------------------------
    # Average Score Computation Tests (Requirement 6.1)
    # -------------------------------------------------------------------

    def test_average_score_computed_correctly(self, fixed_evaluation_results):
        """Average scores are computed as arithmetic mean of non-None values."""
        # multiplier_hr_sonnet has scores [4.0, 3.5, 5.0, 4.5, 3.0]
        # Expected average: (4.0 + 3.5 + 5.0 + 4.5 + 3.0) / 5 = 4.0
        sonnet_results = fixed_evaluation_results["multiplier_hr_sonnet"]
        avg = compute_average_score(sonnet_results.prompt_evaluations)
        assert avg == 4.0

    def test_average_score_nova_2_pro(self, fixed_evaluation_results):
        """Average score for nova_2_pro is correct."""
        # multiplier_hr_nova_2_pro has scores [3.0, 4.0, 3.5, 4.0, 4.5]
        # Expected average: (3.0 + 4.0 + 3.5 + 4.0 + 4.5) / 5 = 3.8
        nova_results = fixed_evaluation_results["multiplier_hr_nova_2_pro"]
        avg = compute_average_score(nova_results.prompt_evaluations)
        assert avg == pytest.approx(3.8)

    def test_average_score_byo_glm_5(self, fixed_evaluation_results):
        """Average score for BYO glm_5 is correct."""
        # multiplier_byo_glm_5 has scores [4.0, 4.5, 5.0, 3.0, 3.5]
        # Expected average: (4.0 + 4.5 + 5.0 + 3.0 + 3.5) / 5 = 4.0
        glm_results = fixed_evaluation_results["multiplier_byo_glm_5"]
        avg = compute_average_score(glm_results.prompt_evaluations)
        assert avg == 4.0

    def test_average_score_in_summary_table(self, generated_report):
        """Summary table shows correctly computed average scores."""
        lines = generated_report.split("\n")
        for line in lines:
            if "multiplier_hr_sonnet" in line and "| managed |" in line:
                # Average of [4.0, 3.5, 5.0, 4.5, 3.0] = 4.0
                assert "4.00" in line
                break
        else:
            pytest.fail("multiplier_hr_sonnet row not found in summary table")

    def test_average_score_skips_failed_evaluations(self, fixed_metadata, tmp_path, monkeypatch):
        """Average score computation skips failed evaluations (non-None values only)."""
        import run_unified_comparison as module
        monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)

        # Create results with 2 successful (4.0, 5.0) and 1 failed prompt
        results: dict[str, AgentEvalResults] = {
            "test_agent": AgentEvalResults(
                agent_name="test_agent",
                agent_type="managed",
                model_key="sonnet",
                prompt_evaluations=[
                    PromptEvaluation(
                        prompt_index=0,
                        prompt_text=PROMPTS[0],
                        scores=[EvaluationScore(evaluator_name="multiplier_domain_accuracy", score=4.0)],
                        success=True,
                    ),
                    PromptEvaluation(
                        prompt_index=1,
                        prompt_text=PROMPTS[1],
                        scores=[],
                        success=False,
                        error="Failed",
                    ),
                    PromptEvaluation(
                        prompt_index=2,
                        prompt_text=PROMPTS[2],
                        scores=[EvaluationScore(evaluator_name="multiplier_domain_accuracy", score=5.0)],
                        success=True,
                    ),
                ],
            ),
        }

        avg = compute_average_score(results["test_agent"].prompt_evaluations)
        # Only non-None scores: (4.0 + 5.0) / 2 = 4.5
        assert avg == 4.5

    def test_average_score_returns_none_when_all_failed(self):
        """Average score returns None when all evaluations failed."""
        prompt_evaluations = [
            PromptEvaluation(
                prompt_index=0,
                prompt_text=PROMPTS[0],
                scores=[],
                success=False,
                error="Failed",
            ),
            PromptEvaluation(
                prompt_index=1,
                prompt_text=PROMPTS[1],
                scores=[],
                success=False,
                error="Failed",
            ),
        ]
        avg = compute_average_score(prompt_evaluations)
        assert avg is None

    # -------------------------------------------------------------------
    # Report Structure Tests (Requirement 5.1)
    # -------------------------------------------------------------------

    def test_report_has_title(self, generated_report):
        """Report starts with the expected title."""
        assert "# Unified Evaluation Comparison Report" in generated_report

    def test_report_sections_in_correct_order(self, generated_report):
        """Report sections appear in the correct order."""
        metadata_pos = generated_report.index("## Metadata")
        summary_pos = generated_report.index("## Summary")
        model_comparison_pos = generated_report.index("## Per-Model Comparison")
        detail_pos = generated_report.index("## Per-Prompt Detail")

        assert metadata_pos < summary_pos
        assert summary_pos < model_comparison_pos
        assert model_comparison_pos < detail_pos


# ---------------------------------------------------------------------------
# Partial Failure Scenario Tests (Task 10.4)
# ---------------------------------------------------------------------------


class TestPartialFailureManagedAgentTimeout:
    """Test scenario: 1 managed agent times out on all 5 prompts → excluded from evaluation.

    Validates: Requirements 1.7, 8.1
    """

    def test_agent_excluded_when_all_prompts_timeout(self, sample_invocation_data, unified_config):
        """Agent with all failed invocations is excluded from evaluation phase."""
        # Mark all prompts for the first managed agent as failed (timeout)
        failed_agent = sample_invocation_data[0]  # multiplier_hr_sonnet
        assert failed_agent.agent_type == "managed"
        for result in failed_agent.results:
            result.success = False
            result.session_id = None
            result.error = "Timeout after 120s"

        with patch("run_unified_comparison.boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session

            mock_eval_client = MagicMock()
            mock_eval_client.evaluate.return_value = {
                "evaluationResults": [
                    {"evaluatorName": "multiplier_domain_accuracy", "score": 4.0, "justification": "Good"}
                ]
            }
            mock_logs_client = MagicMock()
            # Return spans for BYO agents
            mock_logs_client.start_query.return_value = {"queryId": "q-001"}
            mock_logs_client.get_query_results.return_value = {
                "status": "Complete",
                "results": [[
                    {"field": "@timestamp", "value": "2024-01-15 10:00:00.000"},
                    {"field": "@message", "value": json.dumps({"traceId": "t1", "spanId": "s1", "name": "op"})},
                ]],
            }

            mock_session.client.side_effect = lambda svc, **kw: {
                "bedrock-agentcore": mock_eval_client,
                "logs": mock_logs_client,
            }.get(svc, MagicMock())

            with patch("run_unified_comparison.time.sleep"):
                eval_results = run_evaluation_phase(unified_config, sample_invocation_data)

        # The failed agent should be excluded from results
        assert failed_agent.name not in eval_results
        # Other 5 agents should still be evaluated
        assert len(eval_results) == 5

    def test_excluded_agent_noted_in_report(self, sample_invocation_data, unified_config):
        """Excluded agent appears in the report failures section when included in results with failed evals."""
        # Create evaluation results where the failed agent has all failed prompt evaluations
        # (simulating what happens when an agent is included but all its evals fail)
        failed_agent_name = "multiplier_hr_sonnet"

        eval_results = {
            failed_agent_name: AgentEvalResults(
                agent_name=failed_agent_name,
                agent_type="managed",
                model_key="sonnet",
                prompt_evaluations=[
                    PromptEvaluation(
                        prompt_index=i,
                        prompt_text=PROMPTS[i],
                        scores=[],
                        success=False,
                        error="Timeout after 120s",
                    )
                    for i in range(5)
                ],
            ),
            "multiplier_hr_nova_2_pro": AgentEvalResults(
                agent_name="multiplier_hr_nova_2_pro",
                agent_type="managed",
                model_key="nova_2_pro",
                prompt_evaluations=[
                    PromptEvaluation(
                        prompt_index=i,
                        prompt_text=PROMPTS[i],
                        scores=[EvaluationScore("multiplier_domain_accuracy", 4.0, "Good")],
                        success=True,
                    )
                    for i in range(5)
                ],
            ),
        }

        metadata = ReportMetadata(
            timestamp="2024-01-15 10:00:00",
            evaluator_names=["multiplier_domain_accuracy"],
            successful_evaluations=5,
            total_evaluations=10,
            total_execution_time_seconds=60.0,
        )

        with patch("run_unified_comparison.Path.mkdir"), \
             patch("builtins.open", MagicMock()):
            report = generate_unified_report(eval_results, metadata)

        # Report should contain a Failures section with the failed agent
        assert "## Failures" in report
        assert failed_agent_name in report
        assert "Timeout after 120s" in report


class TestPartialFailureBYOSpanRetrievalEmpty:
    """Test scenario: BYO span retrieval returns empty → retry once after 60s → still empty.

    Validates: Requirements 3.5, 8.2
    """

    def test_empty_spans_retries_after_60s(self):
        """When no spans found, retrieve_byo_spans retries once after 60s."""
        mock_cw_client = MagicMock()
        mock_cw_client.start_query.return_value = {"queryId": "q-001"}
        # Both attempts return empty results
        mock_cw_client.get_query_results.return_value = {"status": "Complete", "results": []}

        invocation_start = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        invocation_end = datetime(2024, 1, 15, 10, 0, 5, tzinfo=timezone.utc)

        with patch("run_unified_comparison.time.sleep") as mock_sleep:
            spans = retrieve_byo_spans(
                cw_logs_client=mock_cw_client,
                service_name="multiplier-byo-sonnet",
                invocation_start=invocation_start,
                invocation_end=invocation_end,
            )

        # Should have retried after 60s
        mock_sleep.assert_called_with(60)
        # Should return empty list after retry
        assert spans == []
        # start_query should have been called twice (initial + retry)
        assert mock_cw_client.start_query.call_count == 2

    def test_empty_spans_marked_unevaluated_in_report(self, unified_config):
        """BYO agent with no spans is marked as unevaluated in the report."""
        # Create eval results where BYO agent has failed evaluations due to no spans
        eval_results = {
            "multiplier_byo_sonnet": AgentEvalResults(
                agent_name="multiplier_byo_sonnet",
                agent_type="byo",
                model_key="sonnet",
                prompt_evaluations=[
                    PromptEvaluation(
                        prompt_index=i,
                        prompt_text=PROMPTS[i],
                        scores=[],
                        success=False,
                        error="No spans retrieved from CloudWatch",
                    )
                    for i in range(5)
                ],
            ),
        }

        metadata = ReportMetadata(
            timestamp="2024-01-15 10:00:00",
            evaluator_names=["multiplier_domain_accuracy"],
            successful_evaluations=0,
            total_evaluations=5,
            total_execution_time_seconds=180.0,
        )

        with patch("run_unified_comparison.Path.mkdir"), \
             patch("builtins.open", MagicMock()):
            report = generate_unified_report(eval_results, metadata)

        # Report should contain failures section with the BYO agent
        assert "## Failures" in report
        assert "multiplier_byo_sonnet" in report
        assert "No spans retrieved from CloudWatch" in report

    def test_spans_found_on_retry_succeeds(self):
        """When spans are found on retry, they are returned successfully."""
        mock_cw_client = MagicMock()
        mock_cw_client.start_query.return_value = {"queryId": "q-001"}

        span_data = {"traceId": "t1", "spanId": "s1", "name": "operation"}
        # First attempt: empty, second attempt: has spans
        mock_cw_client.get_query_results.side_effect = [
            {"status": "Complete", "results": []},
            {"status": "Complete", "results": [[
                {"field": "@timestamp", "value": "2024-01-15 10:00:00.000"},
                {"field": "@message", "value": json.dumps(span_data)},
            ]]},
        ]

        invocation_start = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        invocation_end = datetime(2024, 1, 15, 10, 0, 5, tzinfo=timezone.utc)

        with patch("run_unified_comparison.time.sleep"):
            spans = retrieve_byo_spans(
                cw_logs_client=mock_cw_client,
                service_name="multiplier-byo-sonnet",
                invocation_start=invocation_start,
                invocation_end=invocation_end,
            )

        # Should return the spans from the retry
        assert len(spans) == 1
        assert spans[0]["traceId"] == "t1"


class TestPartialFailureThrottlingRetry:
    """Test scenario: Evaluate API returns throttling error → exponential backoff.

    Validates: Requirements 8.3
    """

    def test_throttling_exponential_backoff_delays(self):
        """Verify retry delays follow 10s, 20s, 40s pattern on throttling."""
        mock_client = MagicMock()

        # Create a throttling error
        throttling_error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Evaluate",
        )

        # Fail 3 times with throttling, then succeed on 4th attempt
        mock_client.evaluate.side_effect = [
            throttling_error,
            throttling_error,
            throttling_error,
            {"evaluationResults": [{"evaluatorName": "test", "score": 4.0, "justification": "OK"}]},
        ]

        params = {"evaluatorNames": ["test"], "sessionId": "s1", "agentRuntimeArn": "arn:test"}

        with patch("run_unified_comparison.time.sleep") as mock_sleep:
            scores = _call_evaluate_with_retry(mock_client, params, max_retries=3)

        # Verify exponential backoff delays: 10s, 20s, 40s
        assert mock_sleep.call_count == 3
        mock_sleep.assert_any_call(10)   # 10 * 2^0 = 10
        mock_sleep.assert_any_call(20)   # 10 * 2^1 = 20
        mock_sleep.assert_any_call(40)   # 10 * 2^2 = 40

        # Should eventually succeed
        assert len(scores) == 1
        assert scores[0].score == 4.0

    def test_throttling_raises_after_max_retries(self):
        """After max_retries throttling errors, the exception is raised."""
        mock_client = MagicMock()

        throttling_error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Evaluate",
        )

        # Fail on all attempts (4 total: initial + 3 retries)
        mock_client.evaluate.side_effect = throttling_error

        params = {"evaluatorNames": ["test"], "sessionId": "s1", "agentRuntimeArn": "arn:test"}

        with patch("run_unified_comparison.time.sleep") as mock_sleep:
            with pytest.raises(ClientError) as exc_info:
                _call_evaluate_with_retry(mock_client, params, max_retries=3)

        # Should have retried 3 times before raising
        assert mock_sleep.call_count == 3
        assert "ThrottlingException" in str(exc_info.value)

    def test_throttling_succeeds_on_second_attempt(self):
        """Single throttling error followed by success uses 10s delay."""
        mock_client = MagicMock()

        throttling_error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Evaluate",
        )

        mock_client.evaluate.side_effect = [
            throttling_error,
            {"evaluationResults": [{"evaluatorName": "test", "score": 3.5, "justification": "OK"}]},
        ]

        params = {"evaluatorNames": ["test"], "sessionId": "s1", "agentRuntimeArn": "arn:test"}

        with patch("run_unified_comparison.time.sleep") as mock_sleep:
            scores = _call_evaluate_with_retry(mock_client, params, max_retries=3)

        # Only one retry with 10s delay
        mock_sleep.assert_called_once_with(10)
        assert scores[0].score == 3.5


class TestPartialFailureGeneralError:
    """Test scenario: Evaluate API returns general error → retry once after 10s → raise on second failure.

    Validates: Requirements 4.5
    """

    def test_general_error_retries_once_after_10s(self):
        """General error retries once after 10s, then raises on second failure."""
        mock_client = MagicMock()

        general_error = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Something went wrong"}},
            "Evaluate",
        )

        # Fail twice with general error
        mock_client.evaluate.side_effect = general_error

        params = {"evaluatorNames": ["test"], "sessionId": "s1", "agentRuntimeArn": "arn:test"}

        with patch("run_unified_comparison.time.sleep") as mock_sleep:
            with pytest.raises(ClientError) as exc_info:
                _call_evaluate_with_retry(mock_client, params, max_retries=3)

        # Should have retried once with 10s delay
        mock_sleep.assert_called_once_with(10)
        assert "InternalServerError" in str(exc_info.value)

    def test_general_error_succeeds_on_retry(self):
        """General error on first attempt, success on retry after 10s."""
        mock_client = MagicMock()

        general_error = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Something went wrong"}},
            "Evaluate",
        )

        mock_client.evaluate.side_effect = [
            general_error,
            {"evaluationResults": [{"evaluatorName": "test", "score": 4.5, "justification": "Great"}]},
        ]

        params = {"evaluatorNames": ["test"], "sessionId": "s1", "agentRuntimeArn": "arn:test"}

        with patch("run_unified_comparison.time.sleep") as mock_sleep:
            scores = _call_evaluate_with_retry(mock_client, params, max_retries=3)

        # Should have retried once with 10s delay
        mock_sleep.assert_called_once_with(10)
        assert scores[0].score == 4.5

    def test_non_client_error_retries_once_after_10s(self):
        """Non-ClientError exceptions also retry once after 10s."""
        mock_client = MagicMock()

        # Fail twice with a generic exception
        mock_client.evaluate.side_effect = RuntimeError("Connection reset")

        params = {"evaluatorNames": ["test"], "sessionId": "s1", "agentRuntimeArn": "arn:test"}

        with patch("run_unified_comparison.time.sleep") as mock_sleep:
            with pytest.raises(RuntimeError, match="Connection reset"):
                _call_evaluate_with_retry(mock_client, params, max_retries=3)

        # Should have retried once with 10s delay
        mock_sleep.assert_called_once_with(10)


class TestPartialFailureRegistryUpdate:
    """Test scenario: AWS registry update fails for 1 agent → warning logged, others still updated.

    Validates: Requirements 6.4
    """

    def test_registry_failure_for_one_agent_others_still_updated(self, unified_config):
        """When AWS registry update fails for one agent, other agents are still updated."""
        eval_results = {
            "multiplier_hr_sonnet": AgentEvalResults(
                agent_name="multiplier_hr_sonnet",
                agent_type="managed",
                model_key="sonnet",
                prompt_evaluations=[
                    PromptEvaluation(
                        prompt_index=i,
                        prompt_text=PROMPTS[i],
                        scores=[EvaluationScore("multiplier_domain_accuracy", 4.0, "Good")],
                        success=True,
                    )
                    for i in range(5)
                ],
            ),
            "multiplier_hr_nova_2_pro": AgentEvalResults(
                agent_name="multiplier_hr_nova_2_pro",
                agent_type="managed",
                model_key="nova_2_pro",
                prompt_evaluations=[
                    PromptEvaluation(
                        prompt_index=i,
                        prompt_text=PROMPTS[i],
                        scores=[EvaluationScore("multiplier_domain_accuracy", 3.5, "OK")],
                        success=True,
                    )
                    for i in range(5)
                ],
            ),
        }

        # Mock registry_info.json with records for both agents
        registry_info = {
            "records": [
                {"name": "multiplier-hr-sonnet-managed", "record_id": "rec-001"},
                {"name": "multiplier-hr-nova-2-pro-managed", "record_id": "rec-002"},
            ]
        }

        mock_control_client = MagicMock()
        mock_control_client.get_registry_record.return_value = {
            "descriptors": {"custom": {"inlineContent": "{}"}}
        }

        # First agent update fails, second succeeds
        mock_control_client.update_registry_record.side_effect = [
            Exception("AccessDeniedException: Not authorized"),
            {"recordId": "rec-002"},
        ]

        with patch("run_unified_comparison._load_registry_info", return_value=registry_info), \
             patch("run_unified_comparison.boto3.Session") as mock_session_cls, \
             patch("run_unified_comparison.sys.path"), \
             patch.dict("sys.modules", {"agent_registry": MagicMock()}):

            # Mock the local registry update
            mock_agent_registry = MagicMock()
            mock_agent_registry.update_eval_results.return_value = True

            with patch("run_unified_comparison.update_eval_results", mock_agent_registry.update_eval_results, create=True):
                mock_session = MagicMock()
                mock_session.client.return_value = mock_control_client
                mock_session_cls.return_value = mock_session

                # We need to patch the import inside update_registries
                with patch("builtins.__import__", side_effect=lambda name, *args, **kwargs: (
                    mock_agent_registry if name == "agent_registry" else __import__(name, *args, **kwargs)
                )):
                    update_registries(eval_results, unified_config)

        # update_registry_record should have been called for both agents
        assert mock_control_client.update_registry_record.call_count == 2

    def test_registry_failure_logs_warning(self, unified_config, capsys):
        """Warning is logged when AWS registry update fails for an agent."""
        eval_results = {
            "multiplier_hr_sonnet": AgentEvalResults(
                agent_name="multiplier_hr_sonnet",
                agent_type="managed",
                model_key="sonnet",
                prompt_evaluations=[
                    PromptEvaluation(
                        prompt_index=0,
                        prompt_text=PROMPTS[0],
                        scores=[EvaluationScore("multiplier_domain_accuracy", 4.0, "Good")],
                        success=True,
                    )
                ],
            ),
        }

        registry_info = {
            "records": [
                {"name": "multiplier-hr-sonnet-managed", "record_id": "rec-001"},
            ]
        }

        mock_control_client = MagicMock()
        mock_control_client.get_registry_record.return_value = {
            "descriptors": {"custom": {"inlineContent": "{}"}}
        }
        mock_control_client.update_registry_record.side_effect = Exception("Network timeout")

        with patch("run_unified_comparison._load_registry_info", return_value=registry_info), \
             patch("run_unified_comparison.boto3.Session") as mock_session_cls, \
             patch("run_unified_comparison.sys.path"):

            mock_session = MagicMock()
            mock_session.client.return_value = mock_control_client
            mock_session_cls.return_value = mock_session

            # Mock the import of agent_registry inside update_registries
            mock_update_fn = MagicMock(return_value=True)
            with patch.dict("sys.modules", {"agent_registry": MagicMock(update_eval_results=mock_update_fn)}):
                update_registries(eval_results, unified_config)

        captured = capsys.readouterr()
        assert "[WARN]" in captured.out
        assert "multiplier_hr_sonnet" in captured.out


class TestPartialFailureReportContent:
    """Assert report failures section contains correct agent names, prompt indices, and error messages.

    Validates: Requirements 5.6, 8.1
    """

    def test_failures_section_has_correct_structure(self):
        """Failures section contains agent name, prompt index, and error message."""
        eval_results = {
            "multiplier_hr_sonnet": AgentEvalResults(
                agent_name="multiplier_hr_sonnet",
                agent_type="managed",
                model_key="sonnet",
                prompt_evaluations=[
                    PromptEvaluation(
                        prompt_index=0,
                        prompt_text=PROMPTS[0],
                        scores=[EvaluationScore("multiplier_domain_accuracy", 4.0, "Good")],
                        success=True,
                    ),
                    PromptEvaluation(
                        prompt_index=1,
                        prompt_text=PROMPTS[1],
                        scores=[],
                        success=False,
                        error="Timeout after 120s",
                    ),
                    PromptEvaluation(
                        prompt_index=2,
                        prompt_text=PROMPTS[2],
                        scores=[EvaluationScore("multiplier_domain_accuracy", 3.5, "OK")],
                        success=True,
                    ),
                    PromptEvaluation(
                        prompt_index=3,
                        prompt_text=PROMPTS[3],
                        scores=[],
                        success=False,
                        error="Evaluation API error",
                    ),
                    PromptEvaluation(
                        prompt_index=4,
                        prompt_text=PROMPTS[4],
                        scores=[EvaluationScore("multiplier_domain_accuracy", 4.2, "Good")],
                        success=True,
                    ),
                ],
            ),
            "multiplier_byo_sonnet": AgentEvalResults(
                agent_name="multiplier_byo_sonnet",
                agent_type="byo",
                model_key="sonnet",
                prompt_evaluations=[
                    PromptEvaluation(
                        prompt_index=i,
                        prompt_text=PROMPTS[i],
                        scores=[],
                        success=False,
                        error="No spans retrieved from CloudWatch",
                    )
                    for i in range(5)
                ],
            ),
        }

        metadata = ReportMetadata(
            timestamp="2024-01-15 10:00:00",
            evaluator_names=["multiplier_domain_accuracy"],
            successful_evaluations=3,
            total_evaluations=10,
            total_execution_time_seconds=120.0,
        )

        with patch("run_unified_comparison.Path.mkdir"), \
             patch("builtins.open", MagicMock()):
            report = generate_unified_report(eval_results, metadata)

        # Verify failures section exists
        assert "## Failures" in report

        # Verify correct agent names appear in failures
        assert "multiplier_hr_sonnet" in report
        assert "multiplier_byo_sonnet" in report

        # Verify correct prompt indices (1-indexed in report)
        assert "| multiplier_hr_sonnet | 2 | Timeout after 120s |" in report
        assert "| multiplier_hr_sonnet | 4 | Evaluation API error |" in report

        # Verify BYO agent failures with correct error message
        for i in range(5):
            assert f"| multiplier_byo_sonnet | {i + 1} | No spans retrieved from CloudWatch |" in report

    def test_no_failures_section_when_all_succeed(self):
        """Failures section is absent when all evaluations succeed."""
        eval_results = {
            "multiplier_hr_sonnet": AgentEvalResults(
                agent_name="multiplier_hr_sonnet",
                agent_type="managed",
                model_key="sonnet",
                prompt_evaluations=[
                    PromptEvaluation(
                        prompt_index=i,
                        prompt_text=PROMPTS[i],
                        scores=[EvaluationScore("multiplier_domain_accuracy", 4.0, "Good")],
                        success=True,
                    )
                    for i in range(5)
                ],
            ),
        }

        metadata = ReportMetadata(
            timestamp="2024-01-15 10:00:00",
            evaluator_names=["multiplier_domain_accuracy"],
            successful_evaluations=5,
            total_evaluations=5,
            total_execution_time_seconds=60.0,
        )

        with patch("run_unified_comparison.Path.mkdir"), \
             patch("builtins.open", MagicMock()):
            report = generate_unified_report(eval_results, metadata)

        # No failures section when everything succeeds
        assert "## Failures" not in report
