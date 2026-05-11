# Requirements Document

## Introduction

The Unified Eval Path feature replaces the previous dual-evaluation architecture with a **single AgentCore code-based (Lambda) evaluator** that scores both managed (AgentCore Runtime) and BYO (Bring-Your-Own, Strands + ADOT outside AgentCore) agents.

Previously, managed agents were evaluated via `agentcore run eval` (CLI-based) while BYO agents used the in-memory `strands-agents-evals` SDK. An intermediate attempt to unify both via the LLM-as-a-Judge evaluator (`multiplier_domain_accuracy`) was blocked for BYO agents by `LogEventMissingException` — the AgentCore Evaluate service requires an `invoke_agent` log event that the AgentCore Runtime sidecar emits but the Strands SDK does not. This is documented in `BYO_AgentCore_Observability_issue.md`.

The POC in `BYO_AgentCore_POC_Results.md` proved the working path: **an AgentCore code-based evaluator (AWS Lambda) scores both deployment types through the same `evaluate()` API call, producing directly comparable trajectory scores.** The Lambda evaluator inspects span METADATA only (the service strips event bodies before invoking Lambda), so it scores on trajectory structure — tool selection, tool success rate, tool variety, error-free execution, latency, and efficiency — rather than content quality. Because both deployment types normalize to the same span metadata shape when processed by the service, the same Lambda scores both at parity.

A **single comparator** (`scripts/run_trajectory_comparison.py`) invokes all agents, calls the `evaluate()` API once per session with the Lambda evaluator, and produces a single comparison report.

## Glossary

- **Comparator_Script**: The Python script `scripts/run_trajectory_comparison.py` that orchestrates invocation and evaluation for all agents through a single code path.
- **Trajectory_Evaluator**: The AWS Lambda function `eddpoc-trajectory-evaluator` registered as an AgentCore code-based evaluator (`multiplier_trajectory_eval`). It implements the [AgentCore code-based evaluator contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html).
- **AgentCore_Evaluate_API**: The `bedrock-agentcore` boto3 client `evaluate()` method that invokes a registered evaluator against a set of sessionSpans.
- **Managed_Agent**: An agent deployed to AgentCore Runtime (e.g., `multiplier_hr_sonnet`) whose traces are emitted to CloudWatch via the AgentCore Runtime OTEL sidecar.
- **BYO_Agent**: A Bring-Your-Own agent running outside AgentCore Runtime (e.g., `multiplier_byo_sonnet`) whose traces are emitted to CloudWatch via ADOT (`opentelemetry-instrument`) wrapped around the Strands SDK.
- **Session_Spans**: The list of span items passed to the Evaluate API's `evaluationInput.sessionSpans` parameter. The service normalizes these and strips log event bodies before invoking the code-based evaluator Lambda — only span metadata (attributes, status, scope, timing) reaches the Lambda.
- **CloudWatch_Spans_Log_Group**: The `aws/spans` CloudWatch log group where both managed and BYO OTEL spans are written.
- **Agent_Log_Group**: The per-agent log group (e.g., `/aws/bedrock-agentcore/runtimes/<service-name>`) where log events are written. Events are only read by the LLM-as-a-judge path; code-based evaluators do not receive them.
- **Trace_Propagation_Delay**: The delay (default 120 s) between finishing an invocation and calling `evaluate()`, to give CloudWatch time to make spans available.
- **Trajectory_Score**: The normalized 0.0–1.0 score returned by the Trajectory_Evaluator, with categorical label (`Excellent`, `Very Good`, `Good`, `Poor`, `Unacceptable`) and a plain-text explanation of the scoring breakdown.
- **Comparison_Report**: The generated Markdown report `results/trajectory_comparison.md` containing the summary table, per-model parity table, per-prompt detail, and failures section.

## Requirements

### Requirement 1: Single Evaluator Deployment

**User Story:** As a developer, I want one evaluator artifact (an AWS Lambda function registered as an AgentCore code-based evaluator) that can score any agent — managed or BYO — so there is a single source of truth for scoring.

#### Acceptance Criteria

1. THE system SHALL include an AWS Lambda function named `eddpoc-trajectory-evaluator` with runtime `python3.11`.
2. THE Lambda SHALL implement the AgentCore code-based evaluator Lambda contract: accept `{schemaVersion, evaluatorId, evaluationLevel, evaluationInput.sessionSpans, evaluationTarget}` and return `{label, value, explanation}` on success or `{errorCode, errorMessage}` on error.
3. THE Lambda SHALL be granted invoke permission to the service principal `bedrock-agentcore.amazonaws.com` via `lambda:AddPermission`.
4. THE Lambda's execution role SHALL include `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`, and `logs:CreateLogGroup/Stream/PutLogEvents` permissions.
5. THE Lambda SHALL be registered as an AgentCore evaluator via `create_evaluator(level="TRACE", evaluatorConfig={codeBased: {lambdaConfig: {...}}})` with evaluator name `multiplier_trajectory_eval`.
6. THE Lambda, its execution role, and the evaluator SHALL be tagged with `app=multiplier-hr-agent`, `project=eddpoc`, and `env=dev`.
7. THE system SHALL provide an idempotent deployment script (`evaluators/deploy_trajectory_evaluator.py`) that creates or updates the role, Lambda, permission, and evaluator in a single run.

### Requirement 2: Trajectory Scoring Rubric

**User Story:** As a developer, I want a deterministic, metadata-only scoring rubric that produces directly comparable scores for any agent, because the AgentCore service strips event bodies before invoking code-based evaluators.

#### Acceptance Criteria

1. THE Trajectory_Evaluator SHALL score each trajectory on six criteria with fixed weights totaling a maximum of 100 points. The total score SHALL be a variable integer in the range 0–100, computed as the sum of the six criterion scores:
   - Agent presence (20 points): at least one `strands.telemetry.tracer` span with `gen_ai.operation.name = invoke_agent` exists.
   - Tool success rate (30 points): fraction of `execute_tool` spans whose `gen_ai.tool.status` is `success` or `ok`.
   - Tool variety (10 points): 10 points if two or more distinct tool names, 7 points if exactly one, 0 if none.
   - Error-free execution (15 points): deducts 5 points per `strands.telemetry.tracer` span with `status.code = ERROR`, floor at 0.
   - Latency (15 points): 15 points if `agent_duration_s` under 50% of `MAX_LATENCY_SECONDS` (default 30 s), scaling linearly down to a floor of 5 points at the threshold, 0 if over.
   - Efficiency (10 points): 10 points if tool-call count and LLM-call count are both within bounds (`MAX_TOOL_CALLS=10`, `MAX_LLM_CALLS=8`) and at least one of either exists, else 0.
2. THE Trajectory_Evaluator SHALL return `value` as the total score divided by 100 (normalized to 0.0–1.0).
3. THE Trajectory_Evaluator SHALL return `label` using the mapping: `≥0.90 → Excellent`, `≥0.75 → Very Good`, `≥0.60 → Good`, `≥0.40 → Poor`, `<0.40 → Unacceptable`. THE `label` SHALL be derived solely from the normalized `value`, independent of any individual criterion's score or any Lambda-side computation errors.
4. THE Trajectory_Evaluator SHALL return `explanation` as a single text block containing: trajectory summary (span count, tool count, LLM call count, latency, token usage), tools available, tool sequence with success/failure markers, model identifier, and the six-criterion scoring breakdown.
5. THE Trajectory_Evaluator SHALL only count strands-scoped spans when detecting errors, so that non-agent infrastructure noise (e.g., `Bedrock Runtime.CountTokens` ADOT spans) does not reduce the error-free score.
6. THE Trajectory_Evaluator SHALL NOT attempt to access user query content, assistant response content, tool parameters, or tool results, because the AgentCore service removes event bodies before the Lambda is invoked.

### Requirement 3: Single Comparator Orchestration

**User Story:** As a developer, I want one script that runs the full comparison across all agents so results are always produced under identical conditions.

#### Acceptance Criteria

1. THE Comparator_Script SHALL invoke every Managed_Agent via the `agentcore invoke` CLI with a per-prompt timeout of 180 seconds.
2. THE Comparator_Script SHALL invoke every BYO_Agent via `opentelemetry-instrument` wrapped around `agents/byo_runner.py` with a per-prompt timeout of 300 seconds.
3. THE Comparator_Script SHALL set the following OTEL environment variables for BYO invocations:
   - `AGENT_OBSERVABILITY_ENABLED=true`
   - `OTEL_PYTHON_DISTRO=aws_distro`
   - `OTEL_PYTHON_CONFIGURATOR=aws_configurator`
   - `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`
   - `OTEL_TRACES_EXPORTER=otlp`
   - `OTEL_RESOURCE_ATTRIBUTES` including `service.name`, `aws.log.group.names`, `aws.service.type=gen_ai_agent`
   - `OTEL_EXPORTER_OTLP_LOGS_HEADERS` with `x-aws-log-group`, `x-aws-log-stream`, `x-aws-metric-namespace`
   - `BYO_SESSION_ID` set to a unique per-prompt session identifier
4. THE Comparator_Script SHALL run all agents in parallel using a ThreadPoolExecutor with up to 6 workers, but SHALL invoke the five prompts for any single agent sequentially (with a 3 s spacing between prompts) to avoid per-model throttling.
5. THE Comparator_Script SHALL write `results/trajectory_invocations.json` after invocations complete, with each agent's five `Invocation` records including session_id (BYO), trace_id (discovered later), success flag, duration, and any error text.

### Requirement 4: Unified Evaluation via the Single Evaluator

**User Story:** As a developer, I want every evaluation to go through the same AgentCore `evaluate()` API with the same evaluator ID so scores are directly comparable.

#### Acceptance Criteria

1. THE Comparator_Script SHALL wait for a configurable `Trace_Propagation_Delay` (default 120 s) between invocations and evaluation, printing a countdown every 30 s.
2. THE Comparator_Script SHALL discover trace IDs from the `aws/spans` log group by filtering messages on the agent's service name AND the `invoke_agent` marker, sorted ascending by timestamp, and using the most recent N unique trace IDs where N equals the prompt count.
3. THE Comparator_Script SHALL fetch both aws/spans and the agent log group for the target trace via CloudWatch Logs Insights and pass the combined list as `sessionSpans` to `evaluate()`.
4. THE Comparator_Script SHALL call `agentcore.evaluate(evaluatorId=<Trajectory_Evaluator ID>, evaluationInput={sessionSpans: ...}, evaluationTarget={traceIds: [...]})` for each (agent, prompt) pair. WHEN a log or span fetch returns an empty list or an exception, THE Comparator_Script SHALL record the failure on that (agent, prompt) Evaluation with a descriptive error AND SHALL NOT mark the evaluation as successful unless the `evaluate()` API call has actually been invoked AND has returned a non-error result AND the sessionSpans passed to that call were non-empty.
5. THE Comparator_Script SHALL use the same `evaluatorId` for both Managed_Agent and BYO_Agent evaluations.
6. IF `evaluate()` returns an `errorCode` result, THEN THE Comparator_Script SHALL record the failure on that (agent, prompt) with the error message preserved for the report.
7. THE Comparator_Script SHALL write `results/trajectory_evaluations.json` containing every `Evaluation` record (successful or failed) after evaluation completes.

### Requirement 5: Single Comparison Report

**User Story:** As a developer, I want a single Markdown report that lets me compare managed vs BYO agents directly for every model and prompt.

#### Acceptance Criteria

1. THE Comparator_Script SHALL write `results/trajectory_comparison.md` when evaluation completes.
2. THE Comparison_Report SHALL include a Metadata section listing generation timestamp (UTC), evaluator name, evaluator ID, evaluator type (`Deterministic metadata scoring (same Lambda for managed + BYO)`), successful/total evaluation counts, and total wall-clock time.
3. THE Comparison_Report SHALL include a Summary table with the following columns per agent: Agent, Deployment, Model, Average Score, and one column per prompt (P1–P5) showing either a 2-decimal score or `FAIL`.
4. THE Comparison_Report SHALL include a Per-Model Parity section with one subsection per model key, each containing a table of `{Agent, Deployment, Avg Score, Label distribution}` rows so managed and BYO variants of the same model appear side by side.
5. THE Comparison_Report SHALL include a Per-Prompt Detail section listing trace ID, score, label, and the Trajectory_Evaluator's `explanation` text for every (agent, prompt) pair.
6. IF any evaluations failed, THEN THE Comparison_Report SHALL include a Failures table with Agent, Prompt, and Reason columns. If no failures occurred, THE Comparison_Report SHALL state so explicitly.
7. THE Comparison_Report SHALL include a Notes section that states both managed and BYO agents were scored by the same code-based evaluator through the same `evaluate()` API, lists the six scoring criteria, and references `BYO_AgentCore_Observability_issue.md` for the content-level evaluation gap that remains open.

### Requirement 6: Final Summary Output

**User Story:** As a developer, I want the script to print a concise summary I can read in the terminal without opening the report file.

#### Acceptance Criteria

1. WHEN the Comparator_Script completes, THE script SHALL print `Successful: <succeeded>/<total>` and `Wall-clock: <seconds>`.
2. WHEN at least one managed evaluation succeeded (i.e., the count of successful managed evaluations is greater than zero), THE Comparator_Script SHALL print `Avg managed score: <3-decimal value> (n=<count>)`. IF no managed evaluations succeeded, THE Comparator_Script SHALL NOT print a managed average line.
3. WHEN at least one BYO evaluation succeeded (i.e., the count of successful BYO evaluations is greater than zero), THE Comparator_Script SHALL print `Avg BYO score: <3-decimal value> (n=<count>)`. IF no BYO evaluations succeeded, THE Comparator_Script SHALL NOT print a BYO average line.
4. THE Comparator_Script SHALL print the absolute path to the generated Comparison_Report.

### Requirement 7: Configuration and CLI

**User Story:** As a developer, I want the script configurable without code changes so I can adjust wait time and credentials per run.

#### Acceptance Criteria

1. THE Comparator_Script SHALL read `AWS_PROFILE` and `AWS_REGION` from environment variables, defaulting to `ml-sandbox` and `us-east-1` respectively.
2. THE Comparator_Script SHALL read `WAIT_SECONDS` from the environment, defaulting to 120.
3. THE Comparator_Script SHALL use the evaluator ID constant `EVALUATOR_ID = "multiplier_trajectory_eval-Evy2MEDqBq"` in the deployed eddpoc account; a future revision MAY promote this to a config file but Phase 1 accepts the hard-coded ID.
4. THE deployment script (`evaluators/deploy_trajectory_evaluator.py`) SHALL be idempotent: on second and subsequent runs it SHALL update the Lambda code, refresh the role's inline policy, refresh the invoke permission, and update the evaluator configuration without re-creating unchanged resources. IF AWS requires resource recreation to apply certain updates (for example, immutable Lambda configuration fields or a stale invoke-permission statement), THEN THE deployment script MAY perform the recreation while preserving the external identifiers (Lambda name, evaluator name) so downstream references remain valid.

### Requirement 8: Error Handling

**User Story:** As a developer, I want failures isolated to the specific (agent, prompt) pair so the rest of the comparison completes.

#### Acceptance Criteria

1. IF an invocation fails (non-zero exit or subprocess timeout), THEN THE Comparator_Script SHALL record the failure on that Invocation record and continue the other invocations.
2. IF trace ID discovery returns fewer trace IDs than prompts for a given agent, THEN THE Comparator_Script SHALL abort the run with a non-zero exit code after saving `results/trajectory_invocations.json`, logging which agent and how many trace IDs were missing. This fail-fast behavior prevents silent scoring gaps in the final comparison report. An optional `--allow-partial` flag MAY be added in a future revision to restore the per-prompt-skip behavior.
3. IF `fetch_session_spans` returns an empty list, THEN THE Comparator_Script SHALL record an `error="no spans found"` Evaluation for that (agent, prompt).
4. IF `evaluate()` raises an exception, THEN THE Comparator_Script SHALL catch it, record the exception type and message on the Evaluation record, and continue with remaining evaluations.
5. THE Comparator_Script SHALL save `results/trajectory_invocations.json` and `results/trajectory_evaluations.json` unconditionally at the end of their phases so partial progress is always inspectable.

## Out of Scope

The following items are intentionally excluded from this feature and remain tracked elsewhere:

- **Content-level evaluation for BYO agents** — judging factual accuracy, completeness, or compliance safety requires the LLM-as-a-Judge path, which is blocked by the `invoke_agent` log event gap for BYO agents (see `BYO_AgentCore_Observability_issue.md`).
- **Updating local `registry/registry.json` or the AWS Agent Registry** with trajectory scores — this was in scope for the earlier revision; if needed, it can be added as a follow-up feature.
- **Online Eval Config wiring for the Trajectory_Evaluator** — continuous evaluation via `CreateOnlineEvaluationConfig` is a follow-up and not part of this feature.
