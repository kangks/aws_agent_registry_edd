# Implementation Plan: Unified Eval Path (Trajectory Evaluator)

## Overview

Deploy a single AgentCore code-based evaluator (Lambda) and a single comparator script that uses it to score both managed (AgentCore Runtime) and BYO (Strands + ADOT outside AgentCore) agents through the same `evaluate()` API. This replaces the earlier attempt to unify via the LLM-as-a-Judge evaluator, which is blocked for BYO agents by the `invoke_agent` log event gap documented in `BYO_AgentCore_Observability_issue.md`.

Deployed artifacts after Phase 2:
- IAM role: `eddpoc-trajectory-evaluator-role`
- Lambda: `eddpoc-trajectory-evaluator`
- AgentCore evaluator: `multiplier_trajectory_eval-Evy2MEDqBq`
- Comparator script: `scripts/run_trajectory_comparison.py`
- Report: `results/trajectory_comparison.md`

All AWS resources tagged `app=multiplier-hr-agent`, `project=eddpoc`, `env=dev`.

## Status legend

- [x] Done — delivered in the POC, verified by the end-to-end run.
- [ ] Open — not yet done (follow-up work).
- [ ]* Optional — can be skipped for MVP; useful for regression testing.

## Tasks

- [x] 1. Retire the previous LLM-as-a-Judge implementation
  - [x] 1.1 Keep `scripts/run_unified_comparison.py` on disk as-is
    - Do not delete; it documents the attempt and is referenced by `BYO_AgentCore_Observability_issue.md`.
    - _Requirements: N/A (historical)_
  - [x] 1.2 Keep `evaluators/lambda_evaluator.py` (old deterministic evaluator) on disk
    - Do not delete; it targets a different schema and could be useful for future experiments.
    - _Requirements: N/A_

- [x] 2. Build the Trajectory_Evaluator Lambda
  - [x] 2.1 Implement `evaluators/trajectory_evaluator_lambda.py` with AgentCore code-based evaluator contract
    - `lambda_handler(event, context)` accepting `{schemaVersion, evaluatorId, evaluationLevel, evaluationInput.sessionSpans, evaluationTarget}`
    - Returning `{label, value, explanation}` on success or `{errorCode, errorMessage}` on error
    - Never raise out of the handler
    - _Requirements: 1.2, 8.4_
  - [x] 2.2 Implement `analyze_trajectory(session_spans, target_trace_id)` metadata extractor
    - Operation detection via `gen_ai.operation.name` attribute with span-name fallback
    - Collect invoke_agent spans, execute_tool spans (with status), chat spans (with token counts), error spans
    - Compute `total_latency_s`, `agent_duration_s`, token totals, models used
    - Filter errors to strands-scoped spans only
    - _Requirements: 2.5, 2.6_
  - [x] 2.3 Implement `score_trajectory(analysis)` with the six-criterion rubric totalling 100 points
    - Agent presence (20) / Tool success (30) / Tool variety (10) / Error-free (15) / Latency (15) / Efficiency (10)
    - Normalize to 0.0–1.0 and map to the five-label scale
    - _Requirements: 2.1, 2.2, 2.3_
  - [x] 2.4 Implement `render_explanation(analysis, scoring)` producing a single deterministic text block
    - Include span/tool/LLM counts, latency, tokens, tool sequence with success markers, model, scoring breakdown
    - _Requirements: 2.4_
  - [ ]* 2.5 Unit test the rubric with synthetic span metadata
    - Sanity tests that known-good inputs return Excellent and known-bad inputs return lower labels
    - Property tests for rubric determinism and additivity (Design Properties 2, 3, 4, 5)
    - _Requirements: 2.1–2.6_

- [x] 3. Deployment automation
  - [x] 3.1 Create `evaluators/deploy_trajectory_evaluator.py`
    - Constants: REGION `us-east-1`, ACCOUNT_ID `654654616949`, PROFILE `ml-sandbox`
    - `ensure_iam_role()` — create or fetch role; always refresh inline policy with the full permission list; wait 10 s after creation for propagation
    - `build_zip()` — package `trajectory_evaluator_lambda.py` as `lambda_function.py` into a zip
    - `ensure_lambda(role_arn)` — create-or-update the Lambda with runtime `python3.11`, timeout 300 s, memory 512 MB, and env vars for `BEDROCK_MODEL_ID` and `BEDROCK_REGION`
    - `grant_invoke_permission(lambda_arn)` — remove-then-add a statement allowing `bedrock-agentcore.amazonaws.com` to invoke the Lambda (idempotent)
    - `ensure_evaluator(lambda_arn)` — use `list_evaluators` to find the existing evaluator by name and `update_evaluator` if present, else `create_evaluator(level="TRACE", evaluatorConfig={codeBased: {lambdaConfig: {...}}})`
    - Tag every resource with `app=multiplier-hr-agent`, `project=eddpoc`, `env=dev`
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6, 1.7_
  - [x] 3.2 Execute the deployment script end-to-end in the `ml-sandbox` account
    - Confirm Lambda ARN, evaluator ID, and evaluator ARN are recorded in the POC doc
    - _Requirements: 1.1, 7.4_
  - [ ]* 3.3 Idempotency regression test
    - Run the script twice, confirm resource IDs unchanged and second run reports `(update skipped: ...)` or the `✓ Updated evaluator config` path
    - Validates Design Property 10
    - _Requirements: 7.4_

- [x] 4. Build the single Comparator script
  - [x] 4.1 Create `scripts/run_trajectory_comparison.py` with invocation phase
    - Read `AWS_PROFILE`, `AWS_REGION`, `WAIT_SECONDS` from environment with defaults
    - Define `Invocation`, `Evaluation` dataclasses plus agent configuration lists matching the Design doc
    - Implement `invoke_managed(agent, prompt_index, prompt)` using `agentcore invoke` with 180 s timeout
    - Implement `invoke_byo(agent, prompt_index, prompt)` with the full OTEL env bundle (including `OTEL_EXPORTER_OTLP_LOGS_HEADERS`) and unique `BYO_SESSION_ID`
    - Implement `run_invocations_for_agent(agent, agent_type)` — run the 5 prompts sequentially with a 3 s gap
    - Implement the Phase 1 orchestrator: `ThreadPoolExecutor(max_workers=6)`, 3 managed + 3 BYO agents in parallel
    - Write `results/trajectory_invocations.json` at the end of Phase 1
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_
  - [x] 4.2 Implement Phase 2 wait with 30-second countdown
    - Loop sleeping up to 30 s at a time, printing remaining seconds
    - _Requirements: 4.1_
  - [x] 4.3 Implement trace ID discovery `discover_trace_ids(agent, agent_type, invocations)`
    - Query `aws/spans` filtering on `service_name` AND `invoke_agent`
    - Sort ascending, take last N unique trace IDs where N is the prompt count
    - _Requirements: 4.2_
  - [x] 4.4 Implement `fetch_session_spans(log_group, trace_id)` and `query_logs(...)` helpers
    - Query both `aws/spans` (spans) and the agent's log group (events) with the same trace_id filter
    - Return the concatenated list
    - _Requirements: 4.3_
  - [x] 4.5 Implement `evaluate_one(agent, agent_type, prompt_index, trace_id)`
    - Call `agentcore.evaluate(evaluatorId=EVALUATOR_ID, evaluationInput={sessionSpans: ...}, evaluationTarget={traceIds: [trace_id]})`
    - Same evaluator ID for managed and BYO (no branching on agent_type)
    - Catch exceptions; catch `errorCode` results; both paths produce an `Evaluation(error=...)`
    - _Requirements: 4.4, 4.5, 4.6, 8.4_
  - [x] 4.6 Write `results/trajectory_evaluations.json` at the end of Phase 3
    - _Requirements: 4.7, 8.5_

- [x] 5. Report generator
  - [x] 5.1 Implement `generate_report(all_evaluations, metadata)` returning the Markdown string
    - Metadata section, Summary table, Per-Model Parity, Per-Prompt Detail, Failures, Notes
    - Include the evaluator ID, evaluator type, evaluator name, success count, total count, wall-clock
    - Show per-prompt scores in the summary with `FAIL` for failed cells
    - Group Per-Model Parity by `model_key`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_
  - [x] 5.2 Write the report to `results/trajectory_comparison.md`
    - _Requirements: 5.1_
  - [ ]* 5.3 Unit test `generate_report` with synthetic Evaluation inputs
    - Validates Design Properties 8, 9
    - _Requirements: 5.3, 5.4_

- [x] 6. Final summary output
  - [x] 6.1 Print `Successful: <succeeded>/<total>` and `Wall-clock: <seconds>`
    - _Requirements: 6.1_
  - [x] 6.2 Print `Avg managed score: ... (n=...)` and `Avg BYO score: ... (n=...)` when applicable
    - _Requirements: 6.2, 6.3_
  - [x] 6.3 Print the absolute path to the generated report
    - _Requirements: 6.4_

- [x] 7. End-to-end validation run
  - [x] 7.1 Execute `scripts/run_trajectory_comparison.py` against all 6 deployed agents
    - Managed: `multiplier_hr_sonnet`, `multiplier_hr_nova_2_pro`, `multiplier_hr_glm_5`
    - BYO: `multiplier_byo_sonnet`, `multiplier_byo_nova_2_pro`, `multiplier_byo_glm_5`
    - _Requirements: 3.1–3.5, 4.1–4.7, 5.1–5.7, 6.1–6.4_
  - [x] 7.2 Confirm 30/30 evaluations succeed and managed/BYO averages are within ~0.05 of each other
    - POC achieved managed=0.952, BYO=0.943
    - _Requirements: 4.5 (parity)_

- [x] 8. Documentation
  - [x] 8.1 Produce `BYO_AgentCore_POC_Results.md` summarizing the POC
    - Headline numbers, architecture, findings, deployed resource IDs, reproduction steps
    - _Requirements: 5.7_
  - [x] 8.2 Keep `BYO_AgentCore_Observability_issue.md` and `BYO_AgentCore_Evaluation_Proposals.md` in the repo
    - They document the constraint and the option space that led to this design
    - _Requirements: 5.7_

- [ ]* 9. Property tests
  - [ ]* 9.1 Property 1: evaluator ID is identical for managed and BYO evaluate calls
    - _Validates: Requirements 4.4, 4.5_
  - [ ]* 9.2 Property 2: `lambda_handler` is deterministic in its inputs
    - _Validates: Requirement 2_
  - [ ]* 9.3 Property 3: total score equals the sum of the six criterion scores
    - _Validates: Requirements 2.1, 2.2_
  - [ ]* 9.4 Property 4: label bucketing is monotone
    - _Validates: Requirement 2.3_
  - [ ]* 9.5 Property 5: error-free score ignores non-strands spans
    - _Validates: Requirement 2.5_
  - [ ]* 9.6 Property 6: Lambda never raises on malformed input
    - _Validates: Requirement 8.4_
  - [ ]* 9.7 Property 7: intermediate JSON files exist after each phase
    - _Validates: Requirement 8.5_
  - [ ]* 9.8 Property 8: Per-Prompt Detail contains N×M entries
    - _Validates: Requirement 5.5_
  - [ ]* 9.9 Property 9: Per-Model Parity groups agents by `model_key`
    - _Validates: Requirement 5.4_
  - [ ]* 9.10 Property 10: deploy script is idempotent
    - _Validates: Requirement 7.4_

- [ ] 10. Follow-ups (not required for this feature)
  - [ ] 10.0 Enforce fail-fast abort on missing trace IDs (Requirement 8.2, new)
    - Current Comparator logs `error="no trace_id found"` and continues. The updated requirement says: abort the whole run with non-zero exit, log which agent had how many missing trace IDs, after saving `trajectory_invocations.json`.
    - Acceptance: artificially starve an agent of traces (e.g., block its log group query), run the Comparator, verify exit code != 0 and the error message identifies the starved agent.
    - _Requirements: 8.2_
  - [ ] 10.1 Promote `EVALUATOR_ID` from a hard-coded constant to a config file read by the Comparator
    - _Requirements: 7.3 (deferred)_
  - [ ] 10.2 Wire the Trajectory_Evaluator into an Online Eval Config with `cloudWatchLogs` data source for each agent
    - Would produce continuous scoring without running the Comparator
    - _Requirements: Out-of-scope per current spec_
  - [ ] 10.3 Pair the Trajectory_Evaluator with an external content judge for BYO agents
    - Calls Bedrock directly from the Comparator (bypassing AgentCore evaluator path) to score factual accuracy, completeness, compliance safety
    - Combines both scores in the report (trajectory + content)
    - _Requirements: Out-of-scope per current spec_
  - [ ] 10.4 Update `registry/registry.json` and the AWS Agent Registry with trajectory scores
    - _Requirements: Out-of-scope per current spec_
  - [x] 10.5 Update `EDD_architecture.md` to replace references to the previous unified-eval diagram with the trajectory evaluator diagram
    - _Requirements: 5.7 (documentation)_

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2.1", "2.2", "2.3", "2.4"] },
    { "id": 2, "tasks": ["2.5", "3.1"] },
    { "id": 3, "tasks": ["3.2"] },
    { "id": 4, "tasks": ["3.3", "4.1", "4.2", "4.3", "4.4", "4.5", "4.6"] },
    { "id": 5, "tasks": ["5.1", "5.2", "5.3", "6.1", "6.2", "6.3"] },
    { "id": 6, "tasks": ["7.1"] },
    { "id": 7, "tasks": ["7.2", "8.1", "8.2"] },
    { "id": 8, "tasks": ["9.1", "9.2", "9.3", "9.4", "9.5", "9.6", "9.7", "9.8", "9.9", "9.10"] },
    { "id": 9, "tasks": ["10.0", "10.1", "10.2", "10.3", "10.4", "10.5"] }
  ]
}
```

## Notes

- The AgentCore service normalizes `sessionSpans` before invoking the code-based Lambda: event bodies are stripped, span fields get both camelCase and snake_case keys, and a `source` attribute is added. The Lambda therefore evaluates metadata only. This is not a bug in our evaluator — it is the service contract and is the reason the design works uniformly for managed and BYO.
- Managed and BYO agents are indistinguishable at the `evaluate()` API level: same evaluator ID, same parameter shape, same response schema.
- The POC empirical results (30/30 successful evaluations, managed avg 0.952 vs BYO avg 0.943) are captured in `results/trajectory_comparison.md` and `BYO_AgentCore_POC_Results.md`.
- Content-level evaluation for BYO agents remains blocked by the `invoke_agent` log event gap; the design explicitly calls this out as out-of-scope and references the existing issue document.
