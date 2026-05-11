# Design Document: Unified Eval Path (Trajectory Evaluator)

## Overview

The Unified Eval Path uses a **single AgentCore code-based (Lambda) evaluator** to score both managed (AgentCore Runtime) and BYO (Strands + ADOT outside AgentCore) agents through the same `evaluate()` API. The design has two artifacts:

1. **`multiplier_trajectory_eval`** — an AWS Lambda registered as an AgentCore code-based evaluator with `level=TRACE`. It implements the [AgentCore code-based evaluator contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html).
2. **`scripts/run_trajectory_comparison.py`** — the single comparator that invokes all agents, calls the evaluator once per session, and writes a single Markdown report.

An earlier iteration tried to unify via the LLM-as-a-Judge evaluator (`multiplier_domain_accuracy`). That path failed for BYO agents because the AgentCore Evaluate service requires an `invoke_agent` log event that the Strands SDK does not emit when running outside AgentCore Runtime — documented in `BYO_AgentCore_Observability_issue.md`. The POC in `BYO_AgentCore_POC_Results.md` proved the code-based path works because the service strips event bodies before invoking the Lambda, so the evaluator operates on span metadata only, which both deployment types emit identically.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                 scripts/run_trajectory_comparison.py                     │
│                         (Single Comparator)                              │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Phase 1 — Invocation (ThreadPoolExecutor max_workers=6)                 │
│  ┌──────────────────────────┐   ┌──────────────────────────┐             │
│  │  Managed (3 agents)       │   │  BYO (3 agents)           │             │
│  │  agentcore invoke          │   │  opentelemetry-instrument │             │
│  │  → spans in aws/spans      │   │  → spans in aws/spans     │             │
│  │  → events in runtime LG    │   │  → events in BYO LG       │             │
│  └──────────────┬───────────┘   └──────────────┬───────────┘             │
│                 │                              │                         │
│  Phase 2 — Trace Propagation Wait (120 s default)                        │
│                 │                              │                         │
│  Phase 3 — Trace Discovery + Evaluation                                  │
│  ┌──────────────▼──────────────────────────────▼───────────┐             │
│  │  discover_trace_ids()                                    │             │
│  │    Logs Insights on aws/spans filtered by service.name   │             │
│  │  fetch_session_spans()                                   │             │
│  │    Logs Insights on aws/spans + agent log group          │             │
│  │  agentcore.evaluate(evaluatorId=MULTIPLIER_TRAJECTORY,   │             │
│  │      evaluationInput={sessionSpans: ...},                │             │
│  │      evaluationTarget={traceIds: [...]})                 │             │
│  │    ┌─────────────────────────────────────────┐           │             │
│  │    │ AgentCore normalizes spans, strips      │           │             │
│  │    │ event bodies, invokes the Lambda:       │           │             │
│  │    │                                          │           │             │
│  │    │  trajectory_evaluator_lambda.py         │           │             │
│  │    │    analyze_trajectory() → scoring()     │           │             │
│  │    │    → {label, value, explanation}        │           │             │
│  │    └─────────────────────────────────────────┘           │             │
│  └──────────────────────────┬───────────────────────────────┘             │
│                             │                                            │
│  Phase 4 — Report Generation                                             │
│  ┌──────────────────────────▼───────────────────────────────┐             │
│  │  generate_report() → results/trajectory_comparison.md    │             │
│  │  Also saves raw JSON:                                    │             │
│  │    results/trajectory_invocations.json                   │             │
│  │    results/trajectory_evaluations.json                   │             │
│  └──────────────────────────────────────────────────────────┘             │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Components

### 1. The Single Evaluator: `multiplier_trajectory_eval`

Registered as an AgentCore code-based evaluator with `level=TRACE`, backed by the Lambda function `eddpoc-trajectory-evaluator`.

**Deployed resources (after running `evaluators/deploy_trajectory_evaluator.py`):**

| Resource | Identifier |
|---|---|
| IAM role | `eddpoc-trajectory-evaluator-role` |
| Lambda function | `eddpoc-trajectory-evaluator` |
| Lambda ARN | `arn:aws:lambda:us-east-1:654654616949:function:eddpoc-trajectory-evaluator` |
| Evaluator name | `multiplier_trajectory_eval` |
| Evaluator ID | `multiplier_trajectory_eval-Evy2MEDqBq` |
| Evaluator ARN | `arn:aws:bedrock-agentcore:us-east-1:654654616949:evaluator/multiplier_trajectory_eval-Evy2MEDqBq` |

All resources tagged `app=multiplier-hr-agent, project=eddpoc, env=dev`.

### 2. Lambda Handler Contract

```python
def lambda_handler(event: dict, context) -> dict:
    """
    Input:  {
      "schemaVersion": "1.0",
      "evaluatorId":   "multiplier_trajectory_eval-Evy2MEDqBq",
      "evaluationLevel": "TRACE",
      "evaluationInput": {"sessionSpans": [<normalized span>, ...]},
      "evaluationTarget": {"traceIds": [...], "spanIds": [...]}
    }

    Output (success): {"label": "Excellent", "value": 0.95, "explanation": "..."}
    Output (failure): {"errorCode": "TRAJECTORY_EMPTY", "errorMessage": "..."}
    """
```

### 3. Span Normalization (Service Behavior)

**Critical platform behavior, verified empirically in the POC:** the AgentCore service mutates the `sessionSpans` list before invoking the Lambda. Key facts:

- **Event bodies are stripped.** Lambda receives zero log events even when dozens are passed in.
- **Span `events` and `span_events` inline fields are empty.**
- **Each span is normalized** — receives both camelCase (`parentSpanId`, `endTimeUnixNano`) and snake_case (`parent_span_id`, `end_time`) keys, plus a `source` field.
- **What survives:** `attributes` (all `gen_ai.*`, `session.id`), `scope`, `status`, `kind`, span IDs, trace IDs, timestamps.

Because the service normalizes both managed and BYO input to the same shape, the Lambda's extraction logic is identical for both deployment types.

### 4. Trajectory Analysis (`analyze_trajectory`)

```python
def analyze_trajectory(session_spans: list[dict],
                       target_trace_id: str | None = None) -> dict:
    """Extract trajectory structure from span metadata.

    Filters to target_trace_id if given, then walks spans to collect:
      - invoke_agent spans (for agent_duration_s, total tokens, model, available tools)
      - execute_tool spans (name, status, duration, description)
      - chat spans (per-LLM-call tokens, model, duration)
      - any error-status strands spans (for no_errors scoring)
      - total_latency_s from min/max timestamps

    Returns a dict with trajectory structure and counters only — no content.
    """
```

Key details:
- **Operation detection** uses `attributes["gen_ai.operation.name"]` (values `invoke_agent`, `execute_tool`, `chat`) with a span-name fallback.
- **Error detection** counts only `strands.telemetry.tracer`-scoped spans so ADOT infrastructure noise (e.g., `Bedrock Runtime.CountTokens` from auto-instrumentation) does not count against the agent's score.
- **Token totals** are taken from the `invoke_agent` span's `gen_ai.usage.*_tokens` attributes (already aggregated across turns).
- **Available tools** come from `gen_ai.agent.tools` on the invoke_agent span.

### 5. Scoring Rubric (`score_trajectory`)

| Criterion | Weight | Rule |
|---|---|---|
| Agent presence | 20 | 20 if ≥1 `strands.telemetry.tracer` invoke_agent span exists, else 0 |
| Tool success | 30 | `30 × (successful_tool_calls / total_tool_calls)`, or 15 if no tool calls |
| Tool variety | 10 | 10 if distinct tool names ≥ 2, 7 if exactly 1, 0 if none |
| Error-free | 15 | `15 − min(15, error_span_count × 5)` |
| Latency | 15 | Linearly decay `15 → 5` as `agent_duration_s` approaches `MAX_LATENCY_SECONDS = 30.0`; 0 above threshold; 10 if duration unknown |
| Efficiency | 10 | 10 if tool calls ≤ 10 and LLM calls ≤ 8 and ≥1 of either exists, else 0 |
| **Total** | **100** | Normalized to 0.0–1.0 for return value |

Label mapping (applied to the normalized value):

| Score range | Label |
|---|---|
| ≥ 0.90 | Excellent |
| ≥ 0.75 | Very Good |
| ≥ 0.60 | Good |
| ≥ 0.40 | Poor |
| < 0.40 | Unacceptable |

### 6. Explanation Format

The `explanation` field is a single plain-text block that includes every input the reader needs to verify the score:

```
Trajectory: {N} spans, {T} tool calls, {L} LLM calls, latency {X}s, {K} tokens.
Tools available: tool_a, tool_b, ....
Tool sequence: ✓tool_a → ✓tool_b → ✗tool_c.
Model: {model id}.
[ERRORS: N span(s) failed: name1, name2, ...  (only if errors > 0)]
Scoring breakdown: agent=20/20, tool_success=30/30, tool_variety=10/10,
no_errors=15/15, latency=11/15, efficiency=10/10 = 96/100 (Excellent).
```

### 7. Comparator Data Model

```python
@dataclass
class Invocation:
    agent_name: str
    agent_type: str          # "managed" | "byo"
    model_key: str           # "sonnet" | "nova_2_pro" | "glm_5"
    prompt_index: int        # 0–4
    prompt_text: str
    trace_id: str | None = None
    session_id: str | None = None
    stdout_snippet: str = ""
    success: bool = False
    duration_s: float = 0.0
    error: str = ""

@dataclass
class Evaluation:
    agent_name: str
    agent_type: str
    model_key: str
    prompt_index: int
    prompt_text: str
    trace_id: str
    value: float | None = None
    label: str = ""
    explanation: str = ""
    success: bool = False
    error: str = ""
```

### 8. Comparator Execution Flow

```
main():
  wall_start = time.time()

  # --- Phase 1: Invocation ---
  all_invocations = {}
  with ThreadPoolExecutor(max_workers=6) as pool:
      for agent in MANAGED_AGENTS:
          pool.submit(run_invocations_for_agent, agent, "managed")
      for agent in BYO_AGENTS:
          pool.submit(run_invocations_for_agent, agent, "byo")
      # Each task runs the 5 prompts sequentially (3 s gap between)
  save(results/trajectory_invocations.json)

  # --- Phase 2: Wait ---
  for 120 s (default), print countdown every 30 s

  # --- Phase 3: Trace discovery + Evaluate ---
  for each agent:
      trace_ids = discover_trace_ids(agent)  # via aws/spans CW Logs Insights
      for prompt_index, trace_id:
          session_spans = fetch_session_spans(agent.log_group, trace_id)
          evaluation  = agentcore.evaluate(
              evaluatorId=EVALUATOR_ID,
              evaluationInput={"sessionSpans": session_spans},
              evaluationTarget={"traceIds": [trace_id]},
          )
  save(results/trajectory_evaluations.json)

  # --- Phase 4: Report ---
  generate_report(all_evaluations, metadata)
  print summary
```

### 9. Trace Discovery

```python
def discover_trace_ids(agent: dict, ...) -> list[str]:
    """Find the trace IDs for the 5 recent prompt invocations of this agent.

    Queries aws/spans with:
      fields @timestamp, @message
      | filter @message like /"{service_name}"/ and @message like /"invoke_agent"/
      | sort @timestamp asc
      | limit 50

    Returns the LAST 5 distinct trace IDs (the most recent run), preserving
    chronological order so index i corresponds to prompt i.
    """
```

### 10. Session Spans Fetch

```python
def fetch_session_spans(log_group: str, trace_id: str) -> list[dict]:
    """Fetch spans (aws/spans) and events (agent log group) for one trace.

    Both queries use:
      fields @message
      | filter @message like /"{trace_id}"/
      | sort @timestamp asc
      | limit 500

    Events are included for completeness even though the AgentCore service
    will strip event bodies before invoking the Lambda.
    """
```

### 11. Evaluator Invocation

```python
def evaluate_one(agent, agent_type, prompt_index, trace_id) -> Evaluation:
    session_spans = fetch_session_spans(agent["log_group"], trace_id)
    if not session_spans:
        return Evaluation(..., error="no spans found")

    resp = agentcore.evaluate(
        evaluatorId=EVALUATOR_ID,
        evaluationInput={"sessionSpans": session_spans},
        evaluationTarget={"traceIds": [trace_id]},
    )

    for r in resp.get("evaluationResults", []):
        if "errorCode" in r:
            return Evaluation(..., error=f"{r['errorCode']}: {r['errorMessage']}")
        return Evaluation(
            value=float(r["value"]),
            label=r["label"],
            explanation=r["explanation"],
            success=True,
            ...
        )
```

### 12. Report Generator

Produces `results/trajectory_comparison.md` with five sections:

1. **Metadata** — timestamp, evaluator name/ID, evaluator type, success/total counts, wall-clock.
2. **Summary** — `| Agent | Deployment | Model | Avg | P1..P5 |` table for all 6 agents.
3. **Per-Model Parity** — one subsection per model key (sonnet, nova_2_pro, glm_5) with Managed + BYO side-by-side including label distribution.
4. **Per-Prompt Detail** — trace ID, score, label, explanation for every (agent, prompt) pair.
5. **Failures** — Agent/Prompt/Reason table, or an explicit "No failures" statement.

The report ends with a Notes section documenting the metadata-only nature of the evaluator, the six criteria, and a reference to `BYO_AgentCore_Observability_issue.md` for the open content-level gap.

## Interfaces

### AWS dependencies

| Dependency | API | Purpose |
|---|---|---|
| `boto3 bedrock-agentcore` | `evaluate(evaluatorId, evaluationInput.sessionSpans, evaluationTarget.traceIds)` | Score sessions |
| `boto3 bedrock-agentcore-control` | `create_evaluator`, `update_evaluator`, `list_evaluators` | Register the code-based evaluator |
| `boto3 lambda` | `create_function`, `update_function_code`, `update_function_configuration`, `add_permission` | Deploy the evaluator Lambda |
| `boto3 iam` | `create_role`, `get_role`, `put_role_policy` | Manage the Lambda execution role |
| `boto3 logs` | `start_query`, `get_query_results` | Discover trace IDs and fetch sessionSpans |
| `agentcore` CLI | `agentcore invoke --runtime <name> <prompt>` | Invoke managed agents |
| `opentelemetry-instrument` | Wraps `python agents/byo_runner.py ...` | Invoke BYO agents with ADOT |
| Bedrock Runtime | Not needed by this evaluator (deterministic metadata-only scoring; Bedrock permissions are pre-provisioned in case a future evaluator variant wants them) | — |

### `evaluate()` call shape for BOTH deployment types

```python
agentcore.evaluate(
    evaluatorId="multiplier_trajectory_eval-Evy2MEDqBq",
    evaluationInput={"sessionSpans": [<spans + events from CloudWatch>]},
    evaluationTarget={"traceIds": [trace_id]},
)
```

There is no branching on `agent_type` in the evaluate call. Managed and BYO are indistinguishable at the API level.

### BYO OTEL environment

```bash
AGENT_OBSERVABILITY_ENABLED=true
OTEL_PYTHON_DISTRO=aws_distro
OTEL_PYTHON_CONFIGURATOR=aws_configurator
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_TRACES_EXPORTER=otlp
OTEL_RESOURCE_ATTRIBUTES="service.name=<svc>,aws.log.group.names=<byo log group>,aws.service.type=gen_ai_agent"
OTEL_EXPORTER_OTLP_LOGS_HEADERS="x-aws-log-group=<byo log group>,x-aws-log-stream=runtime-logs,x-aws-metric-namespace=bedrock-agentcore"
BYO_SESSION_ID=<unique per prompt>
BYPASS_TOOL_CONSENT=true
```

## Error Handling

| Failure | Behavior |
|---|---|
| Invocation subprocess timeout / non-zero exit | Record on the `Invocation` with error text; continue other invocations. |
| Fewer trace IDs than prompts for ANY agent | Abort the run with a non-zero exit code after saving `results/trajectory_invocations.json`; log which agent(s) and counts. Prevents silent scoring gaps. (An `--allow-partial` flag is reserved for future revision.) |
| Empty sessionSpans for a trace | Emit `Evaluation(error="no spans found")`; continue with remaining (agent, prompt) pairs. |
| `evaluate()` returns `errorCode` | Emit `Evaluation(error="<code>: <message>")`; continue. |
| `evaluate()` raises | Catch, emit `Evaluation(error="<exception>")`; continue. |
| Report write failure | Surface exception; intermediate JSON files remain available for inspection. |

## Agent Configuration

```python
MANAGED_AGENTS = [
    {
        "name": "multiplier_hr_sonnet",
        "runtime_name": "multiplier_hr_sonnet",
        "model_key": "sonnet",
        "service_name": "eddpoc_multiplier_hr_sonnet.DEFAULT",
        "log_group": "/aws/bedrock-agentcore/runtimes/eddpoc_multiplier_hr_sonnet-5YhsT625tI-DEFAULT",
    },
    {
        "name": "multiplier_hr_nova_2_pro",
        "runtime_name": "multiplier_hr_nova_2_pro",
        "model_key": "nova_2_pro",
        "service_name": "eddpoc_multiplier_hr_nova_2_pro.DEFAULT",
        "log_group": "/aws/bedrock-agentcore/runtimes/eddpoc_multiplier_hr_nova_2_pro-w5d1Pu9Uv4-DEFAULT",
    },
    {
        "name": "multiplier_hr_glm_5",
        "runtime_name": "multiplier_hr_glm_5",
        "model_key": "glm_5",
        "service_name": "eddpoc_multiplier_hr_glm_5.DEFAULT",
        "log_group": "/aws/bedrock-agentcore/runtimes/eddpoc_multiplier_hr_glm_5-SbMYgjFojx-DEFAULT",
    },
]

BYO_AGENTS = [
    {"name": "multiplier_byo_sonnet",     "model_key": "sonnet",     "service_name": "multiplier-byo-sonnet",     "log_group": "/aws/bedrock-agentcore/runtimes/multiplier-byo-sonnet"},
    {"name": "multiplier_byo_nova_2_pro", "model_key": "nova_2_pro", "service_name": "multiplier-byo-nova-2-pro", "log_group": "/aws/bedrock-agentcore/runtimes/multiplier-byo-nova-2-pro"},
    {"name": "multiplier_byo_glm_5",      "model_key": "glm_5",      "service_name": "multiplier-byo-glm-5",      "log_group": "/aws/bedrock-agentcore/runtimes/multiplier-byo-glm-5"},
]

PROMPTS = [
    "What is the employment status of employee EMP-12345 in Singapore?",
    "What are the notice periods and regulatory requirements for employees in Thailand?",
    "Calculate the monthly payroll breakdown for an employee earning 90000 SGD annually in Singapore.",
    "Check the leave balance for employee EMP-67890. How many days do they have remaining?",
    "For employee EMP-11111 in India, look up their details, check compliance rules, and calculate their payroll breakdown in INR.",
]
```

## Design Rationale

### Why a code-based (Lambda) evaluator rather than LLM-as-a-Judge?

The LLM-as-a-Judge evaluator uses the service's internal span parser, which REQUIRES an `invoke_agent` log event with `body.input`/`body.output`. For managed agents this event is emitted by the AgentCore Runtime sidecar; for BYO agents the Strands SDK does not emit it. We confirmed empirically in the POC that forcing the BYO path through LLM-as-a-Judge fails with `LogEventMissingException` in both on-demand and online evaluation modes, with or without `OTEL_EXPORTER_OTLP_LOGS_HEADERS` set.

Code-based evaluators use a different service code path that does not run that parser. Instead it normalizes spans, strips event bodies, and invokes our Lambda with the metadata. This works identically for managed and BYO because both emit the same span metadata shape.

### Why metadata-only scoring?

It is a forced choice: the service does not give the Lambda access to event bodies. Attempting to pass spans plus events through `sessionSpans` does not help because the service filters them out. The design turns this constraint into a feature — the rubric scores trajectory STRUCTURE (tool selection, success rate, latency, error-freeness), which is rigorous and deterministic across deployments.

### Why a deterministic rubric rather than calling Bedrock from inside the Lambda?

Two reasons:
1. **Determinism for comparison.** Two identical trajectories must produce identical scores. A Bedrock call inside the Lambda introduces non-determinism.
2. **No content to judge.** Without access to the user query and assistant response, an LLM call inside the Lambda would only be judging metadata descriptors — which a hand-written rubric does better and cheaper.

The Lambda has `bedrock:InvokeModel` permission pre-provisioned so a future variant can make a judgment call if new AWS features ever expose content — no redeploy of the role is needed.

### Why run invocations sequentially per agent?

Per-model throttling limits are the constraint. Running the five prompts for one agent sequentially (with a 3 s gap) keeps us under service limits. Across agents we run in parallel because the limits apply per-model.

## Correctness Properties

### Property 1: Evaluator ID is the same for managed and BYO

For every (agent, prompt) pair evaluated by the Comparator, the `evaluatorId` parameter in the `agentcore.evaluate` call is the constant `EVALUATOR_ID` regardless of the agent's deployment type. The Comparator SHALL NOT branch on agent type when building the evaluate call.

**Validates: Requirements 4.4, 4.5**

### Property 2: Scoring is deterministic in its inputs

For any two calls to `lambda_handler` with byte-identical `evaluationInput.sessionSpans` and `evaluationTarget`, the returned `(label, value, explanation)` SHALL be identical. The handler SHALL NOT use any non-deterministic inputs (time, random, network).

**Validates: Requirement 2**

### Property 3: Total score equals sum of the six criterion scores

For any invocation that returns a success response, `value * 100` SHALL equal the sum of the six criterion scores listed in the explanation, and each criterion's score SHALL be within its documented 0..weight bound.

**Validates: Requirement 2.1, 2.2**

### Property 4: Label bucketing is monotone

For any two success responses with values `v1 < v2`, the label of `v1` SHALL be equal to or worse than the label of `v2` under the ordering {Unacceptable < Poor < Good < Very Good < Excellent}.

**Validates: Requirement 2.3**

### Property 5: Error-free score ignores non-strands spans

Adding any number of ERROR-status spans whose scope is NOT `strands.telemetry.tracer` to `sessionSpans` SHALL NOT change the `no_errors` criterion score.

**Validates: Requirement 2.5**

### Property 6: Lambda never crashes on malformed input

For any `event` that does not match the contract (missing keys, wrong types, empty sessionSpans), the handler SHALL return `{errorCode, errorMessage}` rather than raising. This property ensures the service can always record a failed evaluation rather than a Lambda exception.

**Validates: Requirement 8.4**

### Property 7: Comparator persists intermediate state unconditionally

After the invocation phase completes (successfully or not), `results/trajectory_invocations.json` SHALL exist. After the evaluation phase completes (successfully or not), `results/trajectory_evaluations.json` SHALL exist. Both files SHALL be valid JSON.

**Validates: Requirement 8.5**

### Property 8: Report contains one row per (agent, prompt) in the Per-Prompt Detail section

For N agents and M prompts, the Per-Prompt Detail section of the report SHALL contain exactly N×M entries, each tagged with the agent name, prompt index, and prompt text. Successful entries include trace ID, value, label, explanation; failed entries include the error message.

**Validates: Requirement 5.5**

### Property 9: Per-model parity section groups agents by model_key

For every model key appearing in the agent configuration, the report's Per-Model Parity section SHALL contain exactly one subsection with that model key as its heading, containing one row per agent of that model regardless of deployment type.

**Validates: Requirement 5.4**

### Property 10: Deployment script is idempotent

Running `deploy_trajectory_evaluator.py` twice in succession SHALL result in identical resource state (same Lambda ARN, same evaluator ID, same permission statement ID), with the second run performing update operations rather than create operations.

**Validates: Requirement 7.4**
