---
name: "byo-agentcore-evaluation"
displayName: "BYO Agent + AgentCore Online Evaluation"
description: "Enable continuous LLM-as-a-Judge evaluation for AI agents running OUTSIDE AgentCore Runtime. Covers OTEL setup, the InvokeAgentLogEmitter SpanProcessor, Online Eval Config creation, and the critical log event format."
keywords: ["agentcore", "evaluation", "byo", "observability", "otel", "strands", "online-eval", "llm-judge"]
author: "Richard Kang"
---

# BYO Agent + AgentCore Online Evaluation

## Overview

This power guides you through setting up **continuous LLM-as-a-Judge evaluation** for AI agents that run **outside** Amazon Bedrock AgentCore Runtime (BYO agents). It enables the same AgentCore Online Evaluation that managed agents get automatically — scoring every invocation on factual accuracy, completeness, and compliance safety.

**The problem:** AgentCore Online Evaluation requires an `invoke_agent` log event that the AgentCore Runtime sidecar creates automatically. BYO agents (running on EC2, ECS, Lambda, or local compute) don't have this sidecar, so the evaluator fails with `LogEventMissingException`.

**The solution:** A custom OpenTelemetry SpanProcessor (`InvokeAgentLogEmitter`) that emits the missing log event in the exact format the evaluator expects. Combined with proper OTEL configuration and an Online Eval Config, BYO agents participate in continuous evaluation alongside managed agents.

**What you'll achieve:**
- Same LLM-as-a-Judge evaluator scoring both managed and BYO agents
- Continuous, automatic evaluation (no manual orchestration)
- Scores visible in CloudWatch GenAI Observability dashboard
- Directly comparable quality metrics across deployment types

## What Works (Proven)

✅ BYO agents emit OTEL spans to `aws/spans` with `strands.telemetry.tracer` scope
✅ BYO agents emit log events to their own CloudWatch log group via `OTEL_EXPORTER_OTLP_LOGS_HEADERS`
✅ The `InvokeAgentLogEmitter` SpanProcessor emits the `invoke_agent` log event
✅ AgentCore LLM-as-a-Judge evaluator scores BYO traces (validated: 5.0/5 Excellent)
✅ Online Eval Configs work with BYO log groups as data source
✅ Same evaluator ID for both managed and BYO — no branching

## What NOT To Do (Pitfalls We Discovered)

❌ **Don't use `content.content` format for the OUTPUT message** — the evaluator expects `content.message` for the assistant response
❌ **Don't use plain string for the INPUT message** — the evaluator expects `content.content` with a JSON array of text blocks
❌ **Don't pass events through the `evaluate()` API's `sessionSpans` parameter** — the service strips event bodies before invoking code-based evaluators (this only affects on-demand evaluation, not Online Eval)
❌ **Don't expect `get_tracer()` alone to emit the `invoke_agent` event** — it emits events for `chat` spans but NOT for the parent `invoke_agent` span
❌ **Don't use the `agentcore run eval` CLI for BYO traces** — it looks in the runtime-specific log group, not `aws/spans`
❌ **Don't skip `OTEL_EXPORTER_OTLP_LOGS_HEADERS`** — without it, log events don't reach the BYO log group

## Prerequisites

- Python 3.10+ with `aws-opentelemetry-distro` installed
- An agentic framework that uses OpenTelemetry (Strands Agents, LangGraph with OTEL instrumentation, etc.)
- AWS account with:
  - Amazon Bedrock model access (for the LLM-as-a-Judge evaluator model)
  - CloudWatch Transaction Search enabled
  - An AgentCore custom evaluator deployed (LLM-as-a-Judge type)
- IAM role for Online Evaluation with permissions to read agent log groups and invoke Bedrock models

## Step 1: Configure OTEL for Your BYO Agent

Set these environment variables before running your agent with `opentelemetry-instrument`:

```bash
# Core ADOT configuration
AGENT_OBSERVABILITY_ENABLED=true
OTEL_PYTHON_DISTRO=aws_distro
OTEL_PYTHON_CONFIGURATOR=aws_configurator
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_TRACES_EXPORTER=otlp

# Service identification + log group routing
# Replace <your-agent-name> with your agent's service name (e.g., "my-hr-agent-sonnet")
# Replace <your-log-group> with your agent's log group (e.g., "/aws/bedrock-agentcore/runtimes/my-hr-agent-sonnet")
OTEL_RESOURCE_ATTRIBUTES="service.name=<your-agent-name>,aws.log.group.names=<your-log-group>,aws.service.type=gen_ai_agent"

# CRITICAL: This routes log events to your agent's CloudWatch log group
OTEL_EXPORTER_OTLP_LOGS_HEADERS="x-aws-log-group=<your-log-group>,x-aws-log-stream=runtime-logs,x-aws-metric-namespace=bedrock-agentcore"

# Session tracking (unique per invocation for evaluation grouping)
BYO_SESSION_ID="<unique-session-id>"
```

**Run your agent:**
```bash
opentelemetry-instrument python your_agent_runner.py --prompt "user query"
```

## Step 2: Install the InvokeAgentLogEmitter SpanProcessor

This is the critical component that bridges the gap. Add this to your agent runner AFTER ADOT initializes:

```python
# In your agent runner (after opentelemetry-instrument sets up the TracerProvider):

# 1. Initialize the Strands tracer (emits spans with strands.telemetry.tracer scope)
from strands.telemetry.tracer import get_tracer
get_tracer()

# 2. Install the InvokeAgentLogEmitter
from invoke_agent_log_emitter import install as install_log_emitter
install_log_emitter()

# 3. Set the user query so the emitter can include it in the log record
import os
os.environ["_BYO_USER_QUERY"] = user_prompt

# 4. Run your agent
agent = create_agent()
result = agent(user_prompt)
```

## Step 3: The InvokeAgentLogEmitter Implementation

Here is the complete SpanProcessor implementation. Save this as `invoke_agent_log_emitter.py` alongside your agent code:

```python
"""
Custom SpanProcessor that emits an invoke_agent log record when the agent span ends.
Bridges the gap between BYO agents and AgentCore Online Evaluation.
"""
import json
import logging
import time
from typing import Optional

from opentelemetry import trace
from opentelemetry._logs import get_logger_provider, SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

logger = logging.getLogger(__name__)
STRANDS_SCOPE = "strands.telemetry.tracer"


class InvokeAgentLogEmitter(SpanProcessor):
    def __init__(self):
        self._last_assistant_message: Optional[str] = None
        self._session_id: Optional[str] = None

    def on_start(self, span, parent_context=None):
        pass

    def on_end(self, span: ReadableSpan) -> None:
        if not span.instrumentation_scope or span.instrumentation_scope.name != STRANDS_SCOPE:
            return

        name = span.name or ""
        attributes = dict(span.attributes) if span.attributes else {}

        if "session.id" in attributes and not self._session_id:
            self._session_id = str(attributes["session.id"])

        # Capture assistant response from span events
        for event in span.events or []:
            event_attrs = dict(event.attributes) if event.attributes else {}
            if event.name == "gen_ai.choice":
                msg = event_attrs.get("message", "")
                if msg:
                    self._last_assistant_message = str(msg)
            elif event.name == "gen_ai.client.inference.operation.details":
                output_msgs = event_attrs.get("gen_ai.output.messages", "")
                if output_msgs:
                    self._last_assistant_message = str(output_msgs)

        if "invoke_agent" not in name:
            return

        import os
        user_query = os.environ.get("_BYO_USER_QUERY", "")

        if not self._last_assistant_message:
            return

        assistant_msg = str(self._last_assistant_message)[:10000]

        # CRITICAL FORMAT (discovered empirically):
        # Input: content.content = JSON array of text blocks
        # Output: content.message = plain string
        body = {
            "input": {
                "messages": [
                    {"content": {"content": json.dumps([{"text": user_query}])}, "role": "user"}
                ]
            },
            "output": {
                "messages": [
                    {"content": {"message": assistant_msg, "finish_reason": "end_turn"}, "role": "assistant"}
                ]
            },
        }

        try:
            logger_provider = get_logger_provider()
            if not isinstance(logger_provider, LoggerProvider):
                return

            otel_logger = logger_provider.get_logger(STRANDS_SCOPE, version="")
            otel_logger.emit(
                timestamp=span.end_time,
                observed_timestamp=int(time.time_ns()),
                severity_number=SeverityNumber.INFO,
                severity_text="",
                body=body,
                attributes={
                    "event.name": STRANDS_SCOPE,
                    "session.id": self._session_id or attributes.get("session.id", "default"),
                },
            )
            logger.info(f"Emitted invoke_agent log record: trace={format(span.context.trace_id, '032x')}")
        except Exception as e:
            logger.warning(f"Failed to emit invoke_agent log record: {e}", exc_info=True)

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


def install():
    """Install the InvokeAgentLogEmitter into the current TracerProvider."""
    provider = trace.get_tracer_provider()
    if hasattr(provider, "_real_provider"):
        provider = provider._real_provider
    if hasattr(provider, "add_span_processor"):
        provider.add_span_processor(InvokeAgentLogEmitter())
        logger.info("InvokeAgentLogEmitter installed")
    else:
        logger.warning(f"Cannot install: provider type={type(provider)}")
```

## Step 4: Create the Online Eval Config

Create an Online Evaluation configuration pointing to your BYO agent's log group:

```python
import boto3

client = boto3.client("bedrock-agentcore-control", region_name="us-east-1")

response = client.create_online_evaluation_config(
    onlineEvaluationConfigName="eval_my_byo_agent",
    description="Continuous evaluation for my BYO agent",
    rule={"samplingConfig": {"samplingPercentage": 100.0}},
    dataSourceConfig={
        "cloudWatchLogs": {
            "logGroupNames": ["<your-log-group>"],
            "serviceNames": ["<your-agent-name>"],
        }
    },
    evaluators=[{"evaluatorId": "<your-evaluator-id>"}],
    evaluationExecutionRoleArn="<your-evaluation-role-arn>",
    enableOnCreate=True,
)
print(f"Config ID: {response['onlineEvaluationConfigId']}")
```

**Required IAM permissions for the evaluation role:**
- `logs:DescribeLogGroups`, `logs:DescribeLogStreams`, `logs:FilterLogEvents`, `logs:GetLogEvents`, `logs:StartQuery`, `logs:GetQueryResults` on `aws/spans` and your agent log group
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` on the eval output log group
- `bedrock:InvokeModel` on the evaluator's judge model

## Step 5: Verify It Works

After invoking your agent, wait ~20 minutes (15 min session idle timeout + processing), then check:

```python
import boto3, json, time

logs = boto3.client("logs", region_name="us-east-1")
config_id = "<your-online-eval-config-id>"
log_group = f"/aws/bedrock-agentcore/evaluations/results/{config_id}"

query_id = logs.start_query(
    logGroupName=log_group,
    startTime=int(time.time()) - 3600,
    endTime=int(time.time()),
    queryString="fields @message | sort @timestamp desc | limit 5",
)["queryId"]
time.sleep(10)
response = logs.get_query_results(queryId=query_id)

for row in response.get("results", []):
    for f in row:
        if f["field"] == "@message":
            msg = json.loads(f["value"])
            attrs = msg.get("attributes", {})
            score = attrs.get("gen_ai.evaluation.score.value")
            error = attrs.get("error.message", "")
            if score:
                print(f"✅ Score: {score}")
            elif error:
                print(f"❌ Error: {error}")
```

## The Critical Log Event Format

This is the exact format the AgentCore Online Evaluation expects. Getting this wrong causes `AgentSpanMappingException`:

```json
{
  "scope": {"name": "strands.telemetry.tracer"},
  "spanId": "<MUST match the invoke_agent span's spanId>",
  "traceId": "<trace ID>",
  "timeUnixNano": 1778310572000000000,
  "observedTimeUnixNano": 1778310572000000000,
  "severityNumber": 9,
  "severityText": "",
  "body": {
    "input": {
      "messages": [
        {
          "content": {"content": "[{\"text\": \"the user's query\"}]"},
          "role": "user"
        }
      ]
    },
    "output": {
      "messages": [
        {
          "content": {"message": "the assistant's response text", "finish_reason": "end_turn"},
          "role": "assistant"
        }
      ]
    }
  },
  "attributes": {
    "event.name": "strands.telemetry.tracer",
    "session.id": "unique-session-id"
  }
}
```

**⚠️ CRITICAL FORMAT RULES:**
- **Input `content`:** `{"content": "[{\"text\": \"...\"}]"}` — JSON-serialized array of text blocks (NOT a plain string, NOT `{"message": "..."}`)
- **Output `content`:** `{"message": "...", "finish_reason": "end_turn"}` — plain string message (NOT a JSON array, NOT `{"content": "..."}`)
- **`spanId`:** MUST match the `invoke_agent` span's spanId (the evaluator correlates by spanId)
- **`scope.name`:** MUST be `strands.telemetry.tracer` (the evaluator only processes this scope)

## Troubleshooting

### Error: `LogEventMissingException`
**Cause:** The `invoke_agent` log event is not present in the agent's log group.
**Fix:** Ensure `InvokeAgentLogEmitter` is installed and `OTEL_EXPORTER_OTLP_LOGS_HEADERS` is set correctly.

### Error: `AgentSpanMappingException: Failed to parse user_query`
**Cause:** Input message format is wrong.
**Fix:** Use `{"content": {"content": "[{\"text\": \"...\"}]"}}` (JSON array), NOT `{"content": {"message": "..."}}`.

### Error: `AgentSpanMappingException: Failed to parse agent_response`
**Cause:** Output message format is wrong.
**Fix:** Use `{"content": {"message": "...", "finish_reason": "end_turn"}}` (plain string), NOT `{"content": {"content": "[...]"}}`.

### No results after 20+ minutes
**Causes:**
1. Session idle timeout hasn't elapsed (default 15 min)
2. Log group name in Online Eval Config doesn't match where events are exported
3. Service name in Online Eval Config doesn't match `OTEL_RESOURCE_ATTRIBUTES`

### SpanProcessor doesn't capture the response
**Cause:** The `gen_ai.choice` span event isn't emitted before the `invoke_agent` span ends.
**Fix:** Ensure `get_tracer()` is called before agent invocation. For non-Strands frameworks, you may need to capture the response differently (e.g., from the agent's return value).

## Adapting for Non-Strands Frameworks

The `InvokeAgentLogEmitter` is designed for Strands Agents SDK. For other frameworks:

1. **LangGraph + opentelemetry-instrumentation-langchain:** The scope name should be `opentelemetry.instrumentation.langchain` and the span name pattern is `LangGraph.workflow`. Adapt the `on_end` method to detect this span and extract input/output from the framework's event format.

2. **Custom frameworks:** Any framework that produces OTEL spans with a root "agent invocation" span can use this pattern. You need to:
   - Identify the root span name/scope
   - Capture the user input (from env var, span attributes, or framework hooks)
   - Capture the final output (from span events, framework hooks, or the agent's return value)
   - Emit the log record with the correct format

## The EDD Loop

Once Online Evaluation is running, you have the complete Evaluation-Driven Development loop:

```
Agent Registry → Deploy Agent → OTEL to CloudWatch → Online Evaluation (continuous) → Scores in Dashboard → Feed back to Registry
```

Every invocation is automatically scored. Quality regressions are detected immediately. Model comparisons are data-driven.

## Reference

- [AgentCore Evaluations Documentation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/how-it-works-evaluations.html)
- [Understanding Input Spans](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/understanding-input-spans.html)
- [Create Online Evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-online-evaluations.html)
- [Observability for Agents Outside AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)
- [AgentCore Samples: Agent Not Hosted on Runtime](https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/06-AgentCore-observability/02-Agent-not-hosted-on-runtime)
