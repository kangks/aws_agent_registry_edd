"""
Data models and constants for the unified evaluation comparison script.

This module defines the dataclasses used across all phases of the unified
evaluation pipeline (invocation, wait, evaluation, report generation) and
the agent/prompt/path constants shared by the script.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

PYTHON = str(PROJECT_ROOT / ".venv" / "bin" / "python")
OTEL_INSTRUMENT = str(PROJECT_ROOT / ".venv" / "bin" / "opentelemetry-instrument")
BYO_RUNNER = str(PROJECT_ROOT / "agents" / "byo_runner.py")
AGENTCORE = "agentcore"

# ---------------------------------------------------------------------------
# Agent Configuration
# ---------------------------------------------------------------------------

MANAGED_AGENTS = [
    {"name": "multiplier_hr_sonnet", "runtime": "multiplier_hr_sonnet", "model_key": "sonnet"},
    {"name": "multiplier_hr_nova_2_pro", "runtime": "multiplier_hr_nova_2_pro", "model_key": "nova_2_pro"},
    {"name": "multiplier_hr_glm_5", "runtime": "multiplier_hr_glm_5", "model_key": "glm_5"},
]

BYO_AGENTS = [
    {"name": "multiplier_byo_sonnet", "model_key": "sonnet", "service_name": "multiplier-byo-sonnet"},
    {"name": "multiplier_byo_nova_2_pro", "model_key": "nova_2_pro", "service_name": "multiplier-byo-nova-2-pro"},
    {"name": "multiplier_byo_glm_5", "model_key": "glm_5", "service_name": "multiplier-byo-glm-5"},
]

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PROMPTS = [
    "What is the employment status of employee EMP-12345 in Singapore?",
    "What are the notice periods and regulatory requirements for employees in Thailand?",
    "Calculate the monthly payroll breakdown for an employee earning 90000 SGD annually in Singapore.",
    "Check the leave balance for employee EMP-67890. How many days do they have remaining?",
    "For employee EMP-11111 in India, look up their details, check compliance rules, and calculate their payroll breakdown in INR.",
]

# ---------------------------------------------------------------------------
# ADOT Environment Variables
# ---------------------------------------------------------------------------

ADOT_ENV = {
    "AGENT_OBSERVABILITY_ENABLED": "true",
    "OTEL_PYTHON_DISTRO": "aws_distro",
    "OTEL_PYTHON_CONFIGURATOR": "aws_configurator",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_TRACES_EXPORTER": "otlp",
    "BYPASS_TOOL_CONSENT": "true",
}

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class InvocationResult:
    """Result of a single agent invocation."""

    response: str
    session_id: Optional[str]
    duration_ms: float
    success: bool
    service_name: Optional[str] = None
    invocation_timestamp: Optional[datetime] = None
    error: Optional[str] = None


@dataclass
class AgentInvocationData:
    """All invocation results for a single agent."""

    name: str
    agent_type: str  # "managed" or "byo"
    model_key: str
    runtime_arn: Optional[str] = None
    service_name: Optional[str] = None
    results: list[InvocationResult] = field(default_factory=list)


@dataclass
class EvaluationScore:
    """Score from a single evaluator for a single prompt."""

    evaluator_name: str
    score: float
    justification: Optional[str] = None


@dataclass
class PromptEvaluation:
    """Evaluation results for a single prompt."""

    prompt_index: int
    prompt_text: str
    scores: list[EvaluationScore] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None


@dataclass
class AgentEvalResults:
    """All evaluation results for a single agent."""

    agent_name: str
    agent_type: str
    model_key: str
    prompt_evaluations: list[PromptEvaluation] = field(default_factory=list)


@dataclass
class ReportMetadata:
    """Metadata for the comparison report."""

    timestamp: str
    evaluator_names: list[str]
    successful_evaluations: int
    total_evaluations: int
    total_execution_time_seconds: float


@dataclass
class IntermediateState:
    """Intermediate state saved between phases for resumption."""

    phase_completed: str  # "invocation", "wait", "evaluation", "report"
    invocation_data: Optional[list[dict]] = None
    retrieved_spans: Optional[dict[str, list[dict]]] = None
    evaluation_results: Optional[dict[str, dict]] = None
