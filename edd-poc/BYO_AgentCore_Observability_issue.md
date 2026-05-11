# BYO Agent Evaluation Gap: Missing `invoke_agent` Log Event

## Summary

BYO agents running outside AgentCore Runtime cannot be evaluated by the AgentCore Evaluator (Online Eval, On-Demand Eval, or `agentcore run eval` CLI) because the `invoke_agent` span is missing its corresponding log event. This event is created by the AgentCore Runtime sidecar for managed agents but is NOT emitted by the Strands SDK or ADOT instrumentation for BYO agents.

## Environment

- **Account:** 654654616949
- **Region:** us-east-1
- **Project:** eddpoc
- **Strands SDK:** latest (via `.venv/bin/python`)
- **ADOT:** `opentelemetry-instrument` with `aws_distro` / `aws_configurator`
- **Evaluator:** `eddpoc_multiplier_domain_accuracy-DCjD5FFsrw` (custom LLM-as-a-Judge)

## What We're Trying to Achieve

Use a **single evaluator** (`multiplier_domain_accuracy`) to score both managed and BYO agents through the same AgentCore Evaluate path, producing directly comparable scores.

Architecture:
```
Managed Agent → AgentCore Runtime → CloudWatch (spans + events) → Online Eval → Score ✅
BYO Agent     → ADOT/OTEL         → CloudWatch (spans + events) → Online Eval → Score ❌
```

## Evidence

### 1. Documentation Requirement: Both spans AND events are required

**Source:** https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/understanding-input-spans.html

> "To evaluate a session, both spans and the corresponding events are required. Not all spans will have events, but the ones with the supported scopes should include corresponding events, else the service will throw a `ValidationException`."

### 2. Documentation: Events for external agents go to configured log group

**Source:** https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/understanding-input-spans.html

> "For agents hosted outside AgentCore Runtime, events are stored in the log group configured by `OTEL_EXPORTER_OTLP_LOGS_HEADERS` environment variable."

### 3. Error from Online Eval (actual output from our environment)

**CloudWatch Log Group:** `/aws/bedrock-agentcore/evaluations/results/eddpoc_eval_byo_sonnet-AVImd57apu`

```json
{
  "attributes": {
    "error": 1,
    "error.message": "Session span data is incomplete. Span with ID: 6b0871e1bd616cc1 and name: invoke_agent Strands Agents is missing a corresponding log event. Please retry the request with the complete span data",
    "error.type": "LogEventMissingException",
    "gen_ai.evaluation.name": "eddpoc_multiplier_domain_accuracy",
    "session.id": "byo-multiplier-byo-sonnet-6d252dff"
  }
}
```

### 4. BYO log group HAS strands events — but NOT for the `invoke_agent` span

**CloudWatch Log Group:** `/aws/bedrock-agentcore/runtimes/multiplier-byo-sonnet`

Query showing strands events exist:
```
fields @message | filter @message like /"strands.telemetry.tracer"/ | sort @timestamp desc | limit 10
```
→ Returns events with `spanId` values matching `chat` spans (e.g., `acf9b95c89fefde9`)

Query showing NO `invoke_agent` event:
```
fields @message | filter @message like /"strands.telemetry.tracer"/ and @message like /"invoke_agent"/ | limit 5
```
→ **Returns 0 results**

### 5. Managed agent's runtime log group DOES have the `invoke_agent` event

**CloudWatch Log Group:** `/aws/bedrock-agentcore/runtimes/eddpoc_multiplier_hr_sonnet-5YhsT625tI-DEFAULT`

Query:
```
fields @message | filter @message like /"strands.telemetry.tracer"/ and @message like /"body"/ | sort @timestamp desc | limit 5
```
→ Returns events with:
- `scope.name: "strands.telemetry.tracer"`
- `spanId` matching the `invoke_agent` span
- `body.input.messages` (user query)
- `body.output.messages` (final assistant response)
- `attributes.event.name: "strands.telemetry.tracer"`

**This event is created by the AgentCore Runtime sidecar, not by the Strands SDK.**

### 6. Adding `get_tracer()` to BYO runner did NOT produce the `invoke_agent` event

**File:** `agents/byo_runner.py`

Added:
```python
from strands.telemetry.tracer import get_tracer
get_tracer()
```

Result: BYO log group received 3 strands events for `chat` spans but still **0 events for the `invoke_agent` span**.

## Comparison Table

| Component | Managed (AgentCore Runtime) | BYO (ADOT + Strands SDK) |
|---|---|---|
| Spans in `aws/spans` | ✅ scope: `strands.telemetry.tracer` | ✅ scope: `strands.telemetry.tracer` |
| `chat` span events in log group | ✅ present | ✅ present |
| `invoke_agent` span event in log group | ✅ created by Runtime sidecar | ❌ NOT emitted |
| Online Eval Config | ✅ scores produced | ❌ `LogEventMissingException` |
| `agentcore run eval --trace-id` | ✅ works | ❌ "No session spans found" |
| On-demand `evaluate()` API | ✅ works (via CLI) | ❌ `LogEventMissingException` |

## OTEL Configuration Used for BYO Agents

```bash
AGENT_OBSERVABILITY_ENABLED=true
OTEL_PYTHON_DISTRO=aws_distro
OTEL_PYTHON_CONFIGURATOR=aws_configurator
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_TRACES_EXPORTER=otlp
OTEL_RESOURCE_ATTRIBUTES="service.name=multiplier-byo-sonnet,aws.log.group.names=/aws/bedrock-agentcore/runtimes/multiplier-byo-sonnet,aws.service.type=gen_ai_agent"
OTEL_EXPORTER_OTLP_LOGS_HEADERS="x-aws-log-group=/aws/bedrock-agentcore/runtimes/multiplier-byo-sonnet,x-aws-log-stream=runtime-logs,x-aws-metric-namespace=bedrock-agentcore"
```

## Online Eval Configs Created for BYO Agents

| Config ID | Log Group | Service Name | Status |
|---|---|---|---|
| `eddpoc_eval_byo_sonnet-AVImd57apu` | `/aws/bedrock-agentcore/runtimes/multiplier-byo-sonnet` | `multiplier-byo-sonnet` | ACTIVE/ENABLED |
| `eddpoc_eval_byo_nova_2_pro-UGf4Dw79AU` | `/aws/bedrock-agentcore/runtimes/multiplier-byo-nova-2-pro` | `multiplier-byo-nova-2-pro` | ACTIVE/ENABLED |
| `eddpoc_eval_byo_glm_5-ujF6o573Ll` | `/aws/bedrock-agentcore/runtimes/multiplier-byo-glm-5` | `multiplier-byo-glm-5` | ACTIVE/ENABLED |

Using the same evaluator as managed agents: `eddpoc_multiplier_domain_accuracy-DCjD5FFsrw`

## Project File Locations

| File | Purpose |
|------|---------|
| `agents/byo_runner.py` | BYO agent entrypoint (Strands + ADOT) |
| `agents/main.py` | Managed agent entrypoint (AgentCore Runtime) |
| `scripts/run_unified_comparison.py` | Unified evaluation script |
| `scripts/unified_models.py` | ADOT env vars configuration |
| `agentcore/agentcore.json` | Project config (runtimes, evaluators, online eval configs) |
| `agentcore/.cli/deployed-state.json` | Deployed resource IDs |
| `tmp_test_byo_invoke.py` | Demo script: invokes BYO agent, checks for invoke_agent event |
| `tmp_create_byo_eval_configs.py` | Script that created the BYO online eval configs |

## Questions for AWS

1. Is there a Strands SDK configuration, ADOT plugin, or environment variable that causes the `invoke_agent` log event to be emitted for BYO agents running outside AgentCore Runtime?
2. Is there a planned Strands SDK update to emit the `invoke_agent` log event (with `body.input`/`body.output`) for agents not hosted in AgentCore Runtime?
3. Is there an alternative evaluation path for BYO agents that doesn't require the `invoke_agent` event? (e.g., evaluating at the `chat` span level instead of trace level)
4. Can the Online Eval Config be configured to evaluate at a different span level (e.g., `chat` spans which DO have corresponding events)?

## Reproduction Steps

1. Run: `.venv/bin/python tmp_test_byo_invoke.py`
2. Wait 20 minutes for online eval to process
3. Query: `/aws/bedrock-agentcore/evaluations/results/eddpoc_eval_byo_sonnet-AVImd57apu`
4. Observe `LogEventMissingException` error in results
