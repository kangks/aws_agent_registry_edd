# Requirements Document

## Introduction

Lean POC demonstrating AgentCore custom evaluators and observability for multi-model evaluation. Build a simple HR/compliance agent, deploy it two ways (managed AgentCore Runtime + local with OTEL), run custom evaluators against traces, compare 3 models. Prove the evaluation capability so Multiplier can point their own agents at it.

### Prerequisites

- AWS account with Bedrock model access (Sonnet 4, Haiku 3.5, Nova Pro)
- `agentcore` CLI installed (`npm install -g @aws/agentcore`) — required for managed deployment (Demo 1) and AgentCore-hosted evaluators
- Python 3.11 with `strands-agents`, `strands-agents[otel]`, `strands-agents-evals`, `boto3`
- AWS credentials configured (default profile, us-east-1)

## Glossary

- **POC_Agent**: A Strands-based AI agent with 4 HR/compliance tools that serves as the vehicle for demonstrating evaluation patterns
- **AgentCore_Runtime**: AWS Bedrock AgentCore managed runtime for deploying and invoking agents
- **Custom_LLM_Judge**: A custom evaluator using Claude Sonnet 4.5 to score agent responses on domain-specific criteria
- **Custom_Lambda_Evaluator**: A Lambda function that performs deterministic checks on agent traces
- **OTEL_Instrumentation**: OpenTelemetry instrumentation that exports traces to CloudWatch
- **InMemorySpanExporter**: OpenTelemetry SDK span exporter that captures trace spans in memory for programmatic access without requiring external collectors
- **StrandsInMemorySessionMapper**: Strands Evals mapper that converts in-memory OTEL spans into structured session data for evaluation
- **ActorSimulator**: Strands Evals component that simulates realistic goal-driven users for multi-turn conversation testing
- **Multi_Turn_Evaluation**: Evaluation of agent performance across multiple conversation turns with adaptive simulated users

## Requirements

### Requirement 1: Strands Agent with Configurable Model

**User Story:** As a DoiT engineer, I want a generic HR/compliance agent with swappable model configuration, so that I can evaluate the same agent logic across multiple LLMs.

#### Acceptance Criteria

1. THE POC_Agent SHALL expose a `create_agent(model_key)` factory function that accepts a model identifier and returns a configured agent instance
2. THE POC_Agent SHALL support 3 model configurations: Claude Sonnet 4, Claude 3.5 Haiku, and Nova Pro
3. THE POC_Agent SHALL include 4 tools: employee_lookup, compliance_checker, payroll_calculator, and leave_manager
4. THE employee_lookup tool SHALL accept employee_id and country and return employment status, department, salary, and start date
5. THE compliance_checker tool SHALL accept country and return notice periods, benefits, tax rates, and regulatory requirements
6. THE payroll_calculator tool SHALL accept annual_salary, country, and currency and return gross_monthly, tax, social_security, net_monthly, and employer_cost
7. THE leave_manager tool SHALL accept employee_id and action and return annual entitlement, used days, and remaining balance

### Requirement 2: AgentCore Runtime Deployment (Demo 1)

**User Story:** As a DoiT engineer, I want to deploy the agent to AgentCore Runtime and generate traces, so that I can demonstrate the managed deployment path.

#### Acceptance Criteria

1. WHEN the agent is deployed via `agentcore deploy`, THE AgentCore_Runtime SHALL host the agent and accept invocations
2. WHEN the agent is invoked, THE AgentCore_Runtime SHALL generate traces visible in CloudWatch
3. WHEN an evaluation is run against traces, evaluators SHALL return numeric scores

### Requirement 3: BYO Agent with OTEL (Demo 2)

**User Story:** As a DoiT engineer, I want to run the agent locally with OTEL exporting to CloudWatch, so that I can demonstrate the BYO path uses the same evaluators.

#### Acceptance Criteria

1. WHEN local_runner.py is executed with OTEL configured, traces SHALL export to CloudWatch
2. WHEN the local agent is run with `--model sonnet`, the service.name SHALL be `multiplier-local-sonnet`
3. WHEN an evaluation is run against local traces, the same evaluators SHALL return scores

### Requirement 4: Custom LLM-as-a-Judge Evaluator

**User Story:** As a DoiT engineer, I want a custom LLM judge evaluator scoring domain accuracy, so that I can show how customers create evaluators for their business domain.

#### Acceptance Criteria

1. THE Custom_LLM_Judge SHALL use Claude Sonnet 4.5 as the judge model
2. THE Custom_LLM_Judge SHALL evaluate factual accuracy, completeness, and compliance safety
3. THE Custom_LLM_Judge SHALL use a 5-level rating scale from 0.0 to 1.0
4. WHEN deployed, THE Custom_LLM_Judge SHALL be registered as "multiplier_domain_accuracy" at TRACE level

### Requirement 5: Custom Lambda Evaluator

**User Story:** As a DoiT engineer, I want a Lambda evaluator with deterministic checks, so that I can show programmatic quality gates without LLM inference.

#### Acceptance Criteria

1. THE Custom_Lambda_Evaluator SHALL check: tool usage occurred, no empty params, non-empty response, latency below threshold
2. THE Custom_Lambda_Evaluator SHALL return a proportional score (passing_checks / 4)
3. WHEN registered, THE Custom_Lambda_Evaluator SHALL be named "multiplier_deterministic" at TRACE level

### Requirement 6: Model Comparison

**User Story:** As a DoiT engineer, I want to compare evaluation scores across 3 models, so that I can demonstrate data-driven model selection.

#### Acceptance Criteria

1. THE comparison script SHALL run 5 prompts against each of 3 models
2. THE comparison script SHALL invoke all 8 evaluators (6 built-in + 2 custom) on each model's traces
3. THE comparison script SHALL output a table with models as columns and evaluators as rows
4. THE comparison script SHALL include cost-per-interaction for each model

### Requirement 7: README Documentation

**User Story:** As a Multiplier engineer, I want clear run instructions so I can reproduce the demo and adapt it to my agents.

#### Acceptance Criteria

1. THE README SHALL include prerequisites, setup, and run commands for both demos
2. THE README SHALL explain how to plug in a different agent

### Requirement 8: Full Agent Traces and Tool Use Graph in Comparison Output

**User Story:** As a DoiT engineer, I want the comparison output to include full agent traces and a tool use graph, so that I can inspect model reasoning paths and tool orchestration patterns alongside evaluation scores.

#### Acceptance Criteria

1. WHEN the comparison script runs, THE comparison script SHALL capture full agent traces using OpenTelemetry InMemorySpanExporter from strands-agents
2. WHEN traces are captured, THE comparison script SHALL map spans into structured session data using StrandsInMemorySessionMapper from strands-agents-evals
3. THE comparison script SHALL save per-model trace files in a `results/traces/` directory with one file per model per prompt
4. WHEN a trace file is generated, THE trace file SHALL include the complete span tree: model calls, tool invocations, parameters, responses, and timing
5. THE comparison script SHALL generate a tool use summary section in `results/comparison.md` showing which tools were called, in what order, with what parameters, and how they connect per prompt
6. THE comparison script SHALL save a `results/traces/tool_use_graph.md` file containing a per-model visualization of tool call sequences and their relationships
7. THE comparison script SHALL generate a "Detailed Agentic Traces" section in `results/comparison.md` showing per-prompt per-model reasoning loops including: model reasoning text before/between tool calls, tool call decisions with parameters, tool results, and final synthesis
8. WHEN a model makes parallel tool calls, THE detailed traces section SHALL identify and label them as parallel (vs sequential)
9. THE detailed traces section SHALL include per-prompt summary statistics: inference span count, tool call count with parallel/sequential breakdown, and total duration

### Requirement 9: Multi-Turn Evaluation using ActorSimulator

**User Story:** As a DoiT engineer, I want to evaluate agent performance across multi-turn conversations with simulated users, so that I can demonstrate realistic interaction quality beyond single-turn prompts.

#### Acceptance Criteria

1. THE Multi_Turn_Evaluation script SHALL use `ActorSimulator.from_case_for_user_simulator()` to create simulated users with defined personas and goals
2. THE Multi_Turn_Evaluation script SHALL run conversations of 3 to 5 turns where the simulated user pursues a goal
3. WHEN a multi-turn conversation runs, THE Multi_Turn_Evaluation script SHALL capture full conversation traces across all turns using InMemorySpanExporter
4. WHEN multi-turn traces are captured, THE Multi_Turn_Evaluation script SHALL evaluate conversations using HelpfulnessEvaluator and goal success rate
5. THE Multi_Turn_Evaluation script SHALL compare multi-turn performance across 3 models: sonnet, haiku, and nova_pro
6. THE Multi_Turn_Evaluation script SHALL define at least 3 custom actor profiles for HR-specific personas: an impatient HR manager, a new employee with questions, and a compliance auditor
7. WHEN a multi-turn evaluation completes, THE Multi_Turn_Evaluation script SHALL save results to `results/multi_turn_comparison.md` with per-model scores and conversation summaries

### Requirement 10: Local SDK-Based Evaluation Scores

**User Story:** As a DoiT engineer, I want evaluation scores computed locally using strands-agents-evals SDK evaluators, so that the comparison report shows actual quality scores without requiring the agentcore CLI to be installed.

#### Acceptance Criteria

1. THE comparison script SHALL evaluate captured sessions using ALL available strands-agents-evals SDK evaluators: HelpfulnessEvaluator, FaithfulnessEvaluator, CoherenceEvaluator, CorrectnessEvaluator, HarmfulnessEvaluator, ResponseRelevanceEvaluator, ToolSelectionAccuracyEvaluator, and ToolParameterAccuracyEvaluator
2. WHEN a session is captured via InMemorySpanExporter, THE comparison script SHALL pass it to each SDK evaluator to produce a score (0.0–1.0) per prompt per model
3. THE comparison report SHALL display actual numeric scores (not N/A) for all SDK evaluators
4. THE comparison script SHALL also run the custom evaluators (multiplier_domain_accuracy, multiplier_deterministic) via agentcore CLI if available, falling back to N/A if CLI fails

### Requirement 11: Visual Trace Diagrams (Optional)

**User Story:** As a DoiT engineer, I want graphical Mermaid diagrams of the agentic trace trees, so that I can visually compare reasoning flows and tool orchestration patterns between models in presentations and documentation.

#### Acceptance Criteria

1. THE comparison script SHALL generate a `results/traces/trace_diagrams.md` file containing Mermaid sequence diagrams for each prompt showing the reasoning flow per model
2. WHEN a diagram is generated, THE diagram SHALL show: user prompt → model reasoning → tool calls (with parameters) → tool results → model synthesis, with timing annotations
3. WHEN a model makes parallel tool calls, THE diagram SHALL render them as parallel branches (using Mermaid `par` blocks)
4. THE diagrams SHALL be renderable in any Mermaid-compatible viewer (GitHub, VS Code, documentation sites)
5. FOR multi-tool prompts, THE diagram SHALL clearly show the sequential dependency chain (e.g., model uses employee salary from lookup to feed into payroll calculator)
