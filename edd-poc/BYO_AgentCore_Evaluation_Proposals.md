# Proposals: Evaluating BYO Agents (Outside AgentCore Runtime) with AgentCore Evaluations

## Context

The current unified evaluation pipeline works end-to-end for managed agents (AgentCore Runtime-hosted) but fails for BYO agents with `LogEventMissingException`. The root cause: the `invoke_agent` Strands span requires a corresponding log event (with `body.input`/`body.output`) that is created by the AgentCore Runtime sidecar but not emitted by the Strands SDK or ADOT instrumentation when running outside AgentCore Runtime.

See [`BYO_AgentCore_Observability_issue.md`](./BYO_AgentCore_Observability_issue.md) for full evidence.

This document explores alternative paths to achieve the goal: **use AgentCore Evaluations as a single unified evaluator for both managed and BYO agents**, producing directly comparable scores.

---

## Proposal Summary

| # | Proposal | Effort | Risk | Blocks on AWS? | Unifies Evaluator? |
|---|---|---|---|---|---|
| 2 | **Lambda (code-based) evaluator — TOP PICK** | Medium | Low | No | ✅ Yes (single Lambda for both managed and BYO) |
| 1 | TOOL_CALL level custom evaluator | Medium | Low | No | ✅ Yes (fallback if #2 has issues) |
| 5 | Run AWS Diagnostic Skill first | Low | None | No | Diagnosis only — do this in parallel |
| 3 | Batch Evaluation with BYO log group | Low | Medium | Maybe | ⚠️ Same API, same gap — quick smoke test only |
| 7 | Session-level LLM-as-a-judge evaluator | Low | Medium | No | ✅ Yes if session events work |
| 4 | Synthesize `invoke_agent` log event via custom OTEL processor | High | Medium | No | ✅ Yes |
| 6 | Host BYO in AgentCore Runtime (hybrid) | Medium | Low | No | ✅ Yes, but defeats "BYO outside AgentCore" |

---

## Proposal 1: TOOL_CALL-Level Custom Evaluator

### Idea

Instead of evaluating the `invoke_agent` trace (which needs the missing log event), evaluate at the **TOOL_CALL level**. AgentCore Evaluators support three levels — `SESSION`, `TRACE`, `TOOL_CALL` — each with different span/event requirements.

For BYO agents, the **tool spans (e.g., `execute_tool employee_lookup`)** have corresponding events emitted by the Strands SDK (we verified this in our environment — tool spans have matching log events in `/aws/bedrock-agentcore/runtimes/multiplier-byo-sonnet`).

### How It Works

1. Create a new custom evaluator with `level: "TOOL_CALL"`
2. The evaluator is invoked per tool call, with `{tool_turn}` and `{context}` placeholders
3. Since tool events ARE present in the BYO log group, evaluation succeeds
4. Aggregate tool-level scores per session → compare to managed agents (same tools, same level)

### Docs Source

- [Create evaluator](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-evaluator.html) — "Tool-level evaluators: `available_tools`, `context`, `tool_turn`"

### Evaluator Config (example)

```json
{
  "llmAsAJudge": {
    "instructions": "You are evaluating an HR agent's tool call. The tool being called: {tool_turn}. The agent context: {context}. Available tools: {available_tools}. Evaluate whether the tool selection is correct and the parameters are appropriate. Score 1-5.",
    "ratingScale": { "numerical": [
      {"value": 1, "label": "Wrong tool", "definition": "Tool choice is incorrect for the user's request"},
      {"value": 3, "label": "Acceptable", "definition": "Tool choice is reasonable but parameters could be better"},
      {"value": 5, "label": "Excellent", "definition": "Correct tool with optimal parameters"}
    ]},
    "modelConfig": { "bedrockEvaluatorModelConfig": {
      "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
      "inferenceConfig": {"maxTokens": 500, "temperature": 0.0}
    }}
  }
}
```

### Validation Steps

1. Create evaluator with `--level TOOL_CALL`
2. Point on-demand eval at BYO trace → verify tool spans resolve without `LogEventMissingException`
3. Run same evaluator on managed agent traces → compare per-tool scores
4. Aggregate per-session: mean tool score across the 5 prompts

### Pros
- Directly addresses our platform gap (tool events DO exist in BYO log group)
- Uses the same evaluator for both managed and BYO
- Still LLM-as-a-Judge with the same model

### Cons
- Not a direct replacement for trace-level "was the overall response good?" evaluation
- Requires aggregation logic in the script to roll tool-level scores into session averages
- Doesn't evaluate the final assistant response quality — only tool decisions

---

## Proposal 2: Lambda (Code-Based) Evaluator — **STRONG CANDIDATE**

### Why This Bypasses the Platform Gap

The `LogEventMissingException` / `AgentSpanMappingException` errors come from the **service's built-in span parser** that tries to extract `user_query` and `agent_response` from the `invoke_agent` span event before handing off to an LLM-as-a-Judge.

With a **code-based evaluator**, the service does NOT parse the spans itself — it just fetches the raw `sessionSpans` and hands them to your Lambda. Your Lambda does the parsing, so whatever the Strands SDK actually emits (chat span events, tool events) is what you work with. No `invoke_agent` event required.

From the [docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html), the Lambda receives:
```json
{
  "evaluationInput": { "sessionSpans": [...] },
  "evaluationTarget": { "traceIds": [...], "spanIds": [...] }
}
```
No validation step. The service just ships the spans.

### Confidence by Mode

| Mode | Confidence | Why |
|---|---|---|
| **On-demand** (`evaluate()` API with `sessionSpans`) | ✅ High | Caller builds `sessionSpans` — we control what's in there. Service just forwards to Lambda. |
| **Online eval** (CloudWatch log group data source) | ⚠️ Medium | Service fetches spans from the log group. Docs don't explicitly say whether it applies LogEventMissingException validation before invoking Lambda. Needs a smoke test. |

### Smoke Test Lambda (1–2 hours to validate both modes)

```python
def handler(event, context):
    """Inspect what the service sends — no real evaluation."""
    spans = event["evaluationInput"]["sessionSpans"]
    target = event["evaluationTarget"]
    scopes = set()
    has_invoke_agent_event = False
    for item in spans:
        scope = item.get("scope", {}).get("name", "")
        scopes.add(scope)
        if "body" in item and scope == "strands.telemetry.tracer":
            if "invoke_agent" in str(item.get("name", "")):
                has_invoke_agent_event = True
    return {
        "label": "INSPECT",
        "value": 1.0,
        "explanation": f"Received {len(spans)} spans. Scopes: {sorted(scopes)}. Has invoke_agent event: {has_invoke_agent_event}. Target: {target}"
    }
```

Wire it as:
1. An on-demand evaluator — call with our own curated spans+events (should definitely work)
2. An online eval config pointing at the BYO log group (`/aws/bedrock-agentcore/runtimes/multiplier-byo-sonnet`) — verify the service actually invokes Lambda without pre-validating

The `explanation` field tells us exactly what the service is passing to the Lambda — answering both confidence questions definitively.

### Production Implementation

Once the smoke test confirms the approach, replace the test Lambda with a real evaluator:

### How It Works

1. Write a Lambda function that:
   - Receives `sessionSpans` input (both spans from `aws/spans` and events from the BYO log group)
   - For BYO: extracts the final assistant response from the `chat` span events (which DO exist)
   - For managed: extracts the final response from the `invoke_agent` event (which exists there)
   - Calls Bedrock directly (e.g., Claude Sonnet) with a custom prompt
   - Returns `{label, value, explanation}` in the standard format
2. Create a `codeBased` evaluator pointing to the Lambda ARN
3. Use the same Lambda evaluator for BOTH managed and BYO online eval configs

### Lambda Function Skeleton

```python
import json
import boto3

bedrock = boto3.client("bedrock-runtime")

def handler(event, context):
    """
    event = {
      "schemaVersion": "1.0",
      "evaluatorId": "...",
      "evaluationLevel": "TRACE",
      "evaluationInput": {"sessionSpans": [...]},
      "evaluationTarget": {"traceIds": ["..."]}
    }
    """
    spans = event["evaluationInput"]["sessionSpans"]
    target_trace_id = event["evaluationTarget"]["traceIds"][0]

    # Extract user query: look for the first user message in any span event
    user_query = None
    final_response = None
    for item in spans:
        if item.get("traceId") != target_trace_id:
            continue

        # BYO: chat span events have body.input/output with user/assistant messages
        body = item.get("body", {})
        if isinstance(body, dict):
            for msg in body.get("input", {}).get("messages", []):
                if msg.get("role") == "user" and not user_query:
                    user_query = extract_text(msg)
            for msg in body.get("output", {}).get("messages", []):
                if msg.get("role") == "assistant":
                    final_response = extract_text(msg)

    if not user_query or not final_response:
        return {
            "errorCode": "INPUT_MISSING",
            "errorMessage": f"Could not extract user query or final response from trace {target_trace_id}"
        }

    # Call Bedrock with the same rubric as our LLM-as-a-Judge evaluator
    prompt = f"""Evaluate this HR agent response.
User query: {user_query}
Agent response: {final_response}
Score 1-5 based on factual accuracy, completeness, compliance safety. Return JSON: {{"value": N, "label": "...", "explanation": "..."}}"""

    response = bedrock.invoke_model(
        modelId="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        body=json.dumps({"messages": [{"role": "user", "content": prompt}], ...})
    )

    # Parse Bedrock response into the required format
    return parse_bedrock_score(response)


def extract_text(msg):
    """Handle both plain string and nested {content: {content: "[{text: ...}]"}} formats."""
    content = msg.get("content", "")
    if isinstance(content, dict):
        raw = content.get("content", "")
        try:
            blocks = json.loads(raw)
            return " ".join(b.get("text", "") for b in blocks if "text" in b)
        except Exception:
            return str(raw)
    return str(content)
```

### Validation Steps

1. Deploy Lambda with IAM role allowing `bedrock:InvokeModel`
2. Grant eval service role permission to invoke Lambda (see docs)
3. Create evaluator: `create_evaluator(level="TRACE", evaluatorConfig={"codeBased": {"lambdaConfig": {...}}})`
4. Test with on-demand eval for ONE BYO trace — verify Lambda receives spans + events
5. Replace both managed and BYO online eval configs to use the Lambda evaluator
6. Run unified comparison

### Pros
- **Best unification candidate** — same Lambda evaluator for BOTH managed and BYO
- Full control over input extraction — handles the missing `invoke_agent` event gracefully
- Deterministic or LLM-based — your choice
- Can still use Bedrock Claude for scoring, keeping comparable methodology

### Cons
- Requires building and deploying a Lambda function
- Requires tagging resources (`app=multiplier-hr-agent`, `project=eddpoc`, `env=dev` per workspace rules)
- Per-invocation cost for Lambda + Bedrock
- Must maintain 2 code paths inside the Lambda (BYO event format vs. managed event format)

### Recommended: **This is the most promising proposal**

---

## Proposal 3: Batch Evaluation with BYO Log Group

### Idea

Use the **Batch Evaluation** API (`StartBatchEvaluation`) instead of Online Eval. Batch evaluation operates on a CloudWatch log group + service name, discovering sessions automatically.

### How It Works

```python
client.start_batch_evaluation(
    batchEvaluationName="byo_sonnet_batch",
    evaluators=[{"evaluatorId": "eddpoc_multiplier_domain_accuracy-DCjD5FFsrw"}],
    dataSourceConfig={
        "cloudWatchLogs": {
            "serviceNames": ["multiplier-byo-sonnet"],
            "logGroupNames": ["/aws/bedrock-agentcore/runtimes/multiplier-byo-sonnet"],
        }
    },
)
```

### Docs Source

- [Batch evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/batch-evaluations.html)
- [Start batch evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/batch-evaluations-start.html)

### Validation Steps

1. Invoke BYO agents
2. Call `start_batch_evaluation` pointing to BYO log group
3. Poll `get_batch_evaluation` until `COMPLETED`
4. Read per-session results from output log group

### Pros
- Simpler than Online Eval (no sampling config, no idle timeout)
- Can target specific sessions via `filterConfig.sessionIds`
- Same API shape as managed agents → easy to unify

### Cons
- **Likely hits the same `LogEventMissingException`** — batch uses the same evaluator engine as online/on-demand
- Must confirm with a test before building on this

### Validation First
Run this smoke test before investing effort:
```python
# Quick test: point batch eval at an already-invoked BYO session
client.start_batch_evaluation(...)
# Wait, check if per-session results have actual scores or LogEventMissing errors
```

---

## Proposal 4: Synthesize the `invoke_agent` Log Event

### Idea

Create the missing `invoke_agent` log event ourselves via a custom OTEL log processor or a post-processing Lambda that runs on CloudWatch log events.

### How It Works

**Option 4a: OTEL SpanProcessor wrapper around Strands**
- Subclass `BatchLogRecordProcessor` from OTEL
- On every span end, if the span is `invoke_agent Strands Agents`, manually emit a log record with:
  - Same `spanId` as the invoke_agent span
  - `scope.name: "strands.telemetry.tracer"`
  - `body.input.messages = [user_message]` (captured from first chat span's input)
  - `body.output.messages = [assistant_final_message]` (captured from last chat span's output)
- The log record is exported via the standard OTEL log exporter to the BYO log group

**Option 4b: CloudWatch Logs subscription → Lambda**
- Subscribe a Lambda to the BYO log group
- On each new log event with `scope.name: "strands.telemetry.tracer"`, aggregate
- When the session is idle for N seconds, emit a synthetic `invoke_agent` event to the same log group

### Validation Steps

1. Implement Option 4a as a Python package or in-process hook in `byo_runner.py`
2. Invoke one BYO agent → verify the new log event appears in the BYO log group with the correct `spanId`
3. Trigger on-demand eval → should resolve without `LogEventMissingException`

### Pros
- Directly fills the platform gap without waiting for AWS
- Reusable for any Strands-based BYO agent
- Works with both online eval and on-demand eval

### Cons
- Fragile — depends on Strands internal span semantics which could change
- Option 4a requires deep OTEL/Strands integration
- Option 4b adds latency (waits for session idle) and Lambda cost
- May not produce the exact body structure AWS expects, causing `AgentSpanMappingException` instead

---

## Proposal 5: Run the Official AWS Diagnostic Skill First

### Idea

AWS publishes an **official diagnostic skill** specifically for `LogEventMissingException` for agents hosted outside AgentCore Runtime (`3p-managed` deployment type).

### Docs Source

- [Diagnose AgentCore Evaluation issues with an AI coding assistant](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/diagnose-evaluation-issues.html)

> "The skill works with agents deployed on AgentCore Runtime **and agents hosted on third-party infrastructure (Amazon ECS, Amazon EKS, AWS Lambda, or any other environment)**. The assistant queries your own Amazon CloudWatch log groups to identify the root cause and recommend a fix."

### How It Works

1. Copy the skill from [Diagnostic skill source](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/diagnose-evaluation-skill-source.html)
2. Save as `.kiro/skills/agentcore-eval-diagnostic/SKILL.md`
3. Load in Kiro and run:
   > "My Region is us-east-1, deployment type is `3p-managed`, evaluation type is `online`, session ID is `byo-multiplier-byo-sonnet-6d252dff`. My evaluation isn't producing results. Can you diagnose it?"
4. The skill queries our CloudWatch log groups and produces a structured diagnostic report with a recommended fix

### Validation Steps

1. Copy SKILL.md into `.kiro/skills/agentcore-eval-diagnostic/`
2. Run the diagnosis against one failing BYO session
3. Review the recommended fix — may reveal an undocumented config we missed (e.g., a specific env var, span attribute, or service name format)

### Pros
- **Zero risk** — uses official AWS tooling
- Might reveal the actual fix (e.g., a missing `OTEL_PYTHON_LOG_CORRELATION=true` or similar)
- If the skill says "this is expected for 3p-managed," we get written confirmation

### Cons
- Only diagnoses — doesn't fix automatically
- May confirm what we already know (no quick win)

### Recommended: **Do this first** — cheap, fast, definitive

---

## Proposal 6: Host BYO in AgentCore Runtime (Hybrid)

### Idea

Drop the "BYO outside AgentCore" constraint and deploy the BYO agents as AgentCore Runtimes too. The existing managed evaluation path then works unchanged.

### How It Works

- Use the same agent code (`agents/main.py` or a variant)
- Deploy 3 additional runtimes: `multiplier_byo_sonnet_managed`, etc.
- The "BYO" distinction becomes internal (same code, same deployment model)

### Pros
- Unified evaluation works today — no platform gap
- Zero new engineering
- Consistent observability

### Cons
- **Defeats the purpose of BYO** — the whole point is to demonstrate agents running outside AgentCore Runtime
- Doesn't answer "can I evaluate my ECS/EKS/Lambda agent?"
- Still useful as a fallback for the demo

### When to Use
Only if Proposals 1–5 all fail and the demo deadline is tight.

---

## Proposal 7: Session-Level LLM-as-a-Judge Evaluator

### Idea

Switch from TRACE-level to SESSION-level evaluation. Session-level evaluators use `{context}` (all turns) and `{available_tools}` placeholders — which may not require the specific `invoke_agent` event.

### Docs Source

- [Create evaluator](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-evaluator.html) — "Session-level evaluators: `context`, `available_tools`"

### How It Works

1. Create a new evaluator with `level: "SESSION"`
2. `context` placeholder includes the full session transcript — the service must assemble this from spans + events
3. If SESSION-level assembly uses only tool span events (which BYO HAS), it may work without the invoke_agent event

### Validation Steps

1. Create session-level evaluator
2. Run on-demand eval against a BYO session
3. Check if `LogEventMissingException` still occurs

### Pros
- Minimal code change — just a new evaluator level
- Reuses the same LLM-as-a-Judge pattern

### Cons
- **Unknown if it avoids the `invoke_agent` event requirement** — docs don't specify
- Same risk as batch eval — same evaluator engine, may hit same error

---

## Recommended Path Forward

### Phase 1: Validate with a smoke-test Lambda (1 day)
1. **Proposal 2 smoke test**: Deploy the inspection Lambda, wire it as both on-demand and online eval. The Lambda's `explanation` field will tell us exactly what the service passes — confirming whether the code-based path bypasses `LogEventMissingException` in both modes.
2. **Proposal 5**: Also run the official AWS diagnostic skill on the original failing session — it may surface an undocumented env var or config we missed.
3. **Proposals 3 and 7** (batch eval and SESSION-level) — only if the smoke test reveals the code-based path has unexpected issues.

### Phase 2: Build the real evaluator (1–2 days)
4. **Proposal 2 production Lambda** — calls Bedrock with the same rubric as `multiplier_domain_accuracy`, extracts user query + final response from whatever spans/events are actually present. Use as the **single evaluator for both managed and BYO**.
5. Delete the existing LLM-as-a-Judge evaluator configs and replace with the Lambda-based one.

### Phase 3: Validate parity (1 day)
- Run the unified comparison with the Lambda evaluator against both managed and BYO agents
- Confirm scores are directionally aligned (same prompts, same model, same rubric → similar scores)
- If there's unexpected divergence, the Lambda's `explanation` field tells us what's different in the input data

### Phase 4: Production
- Update spec in `.kiro/specs/unified-eval-path/` with the code-based evaluator approach
- Update `EDD_architecture.md` to document the unified Lambda evaluator
- Re-run end-to-end comparison → produce final report with BOTH managed and BYO scores

### Fallbacks (in order)
- If Proposal 2 online-eval mode fails but on-demand works → use **on-demand only** (still unified, just orchestrated from the script instead of continuous)
- If Proposal 2 fails entirely → **Proposal 1** (TOOL_CALL-level) since tool events DO exist in BYO log group
- Last resort → **Proposal 6** (host BYO in AgentCore Runtime)

---

## Open Questions for AWS

1. Is there a supported way for a Strands SDK agent running outside AgentCore Runtime to emit the `invoke_agent` log event? A flag, env var, SDK hook, or OTEL processor?
2. Does the **SESSION-level** evaluator tolerate a missing `invoke_agent` event if tool events are present?
3. Does the **TOOL_CALL-level** evaluator require the `invoke_agent` event, or only the tool span event?
4. Is there a roadmap item for "BYO agent evaluation parity" in AgentCore Evaluations?
5. The diagnostic skill covers `3p-managed` deployment. What's the official workaround for this specific error, for our Strands + ADOT stack?

---

## References

- [How it works: Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/how-it-works-evaluations.html)
- [Evaluation types (Online, On-demand, Batch)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations-types.html)
- [Evaluators (built-in + custom)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluators.html)
- [Create evaluator (LLM + code-based)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-evaluator.html)
- [Custom code-based (Lambda) evaluator](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html)
- [Understanding input spans (scopes, events, body)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/understanding-input-spans.html)
- [Create online evaluation (CloudWatch log group data source)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-online-evaluations.html)
- [Start batch evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/batch-evaluations-start.html)
- [Observability for agents outside AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html) — `OTEL_EXPORTER_OTLP_LOGS_HEADERS` setup
- [Diagnose evaluation issues (official skill)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/diagnose-evaluation-issues.html)
