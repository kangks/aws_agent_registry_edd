# BYO Agent Observability — CloudWatch Traces via ADOT

## Summary

**BYO (Bring Your Own)** means the agent runs locally (or on any compute you control) rather than on AgentCore Runtime. Traces are exported to CloudWatch/X-Ray via AWS Distro for OpenTelemetry (ADOT), making the agent visible in CloudWatch GenAI Observability dashboards alongside managed runtime agents.

## How It Works

1. Install `aws-opentelemetry-distro` (ADOT Python package)
2. Set environment variables for ADOT configuration
3. Run agent with `opentelemetry-instrument` wrapper
4. ADOT auto-instruments botocore calls and exports traces to X-Ray/CloudWatch

## How to Run

### Prerequisites
```bash
pip install aws-opentelemetry-distro
```

### Invocation Command
```bash
AGENT_OBSERVABILITY_ENABLED=true \
OTEL_PYTHON_DISTRO=aws_distro \
OTEL_PYTHON_CONFIGURATOR=aws_configurator \
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
OTEL_RESOURCE_ATTRIBUTES="service.name=multiplier-byo-sonnet" \
AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \
.venv/bin/opentelemetry-instrument .venv/bin/python agents/byo_runner.py \
  --model sonnet \
  --prompt "What is the employment status of employee EMP-12345 in Singapore?"
```

### Service Names per Model
| Model | Service Name |
|-------|-------------|
| sonnet | `multiplier-byo-sonnet` |
| haiku | `multiplier-byo-haiku` |
| nova_pro | `multiplier-byo-nova-pro` |

## Trace Verification Results

### Traces Appeared in CloudWatch/X-Ray: ✅ YES

All 9 invocations (3 models × 3 prompts) produced traces visible in AWS X-Ray:

| Trace ID | Service | Model | Tool Used | Duration |
|----------|---------|-------|-----------|----------|
| 1-69fbc51c-93d44a92f349922c4e04b3fe | multiplier-byo-sonnet | claude-sonnet-4-6 | employee_lookup | 5.5s |
| 1-69fbc5f0-4635c8c5ce148a54cb87a411 | multiplier-byo-sonnet | claude-sonnet-4-6 | compliance_checker | 8.6s |
| 1-69fbc606-60c917f85313ae01ae49bbe8 | multiplier-byo-sonnet | claude-sonnet-4-6 | payroll_calculator | 10.6s |
| 1-69fbc637-18c78527696fa18b933aa17f | multiplier-byo-haiku | claude-haiku-4-5 | employee_lookup | 4.6s |
| 1-69fbc648-3f37a93d2a94470d4a909073 | multiplier-byo-haiku | claude-haiku-4-5 | compliance_checker | 5.0s |
| 1-69fbc72b-4f6c84eefb3ff6e1886cc1fd | multiplier-byo-haiku | claude-haiku-4-5 | payroll_calculator | 5.0s |
| 1-69fbc763-046ccb45be4c5367c5e6a9f3 | multiplier-byo-nova-pro | nova-pro-v1:0 | employee_lookup | 3.5s |
| 1-69fbc779-81053e899eccdecc8a4be380 | multiplier-byo-nova-pro | nova-pro-v1:0 | compliance_checker | 3.9s |
| 1-69fbc78b-3bcf00a71f0f988ff5c2d0c2 | multiplier-byo-nova-pro | nova-pro-v1:0 | payroll_calculator | 4.2s |

### Trace Content

Each trace includes:
- `invoke_agent Strands Agents` — top-level agent span
- `chat {model_id}` — Bedrock ConverseStream calls
- `execute_tool {tool_name}` — tool execution spans
- `Bedrock Runtime.CountTokens` — token counting calls
- `execute_event_loop_cycle` — agent loop iterations

### Annotations Present
- `aws.local.service`: Service name (e.g., `multiplier-byo-sonnet`)
- `aws.local.resource.identifier`: Model ID
- `aws.local.resource.type`: `AWS::Bedrock::Model`
- `aws.remote.service`: `AWS::BedrockRuntime`
- `span.kind`: `INTERNAL`, `CLIENT`

## Evaluation Results

### Status: ⚠️ Not Available for BYO Traces

The `agentcore run eval` command currently requires traces to be associated with a **managed AgentCore runtime**. BYO traces appear in X-Ray with their own service name but are not linked to a runtime ARN, so the evaluator cannot discover them.

**Error:** `"No session spans found for agent ... in the last 7 day(s). Has the agent been invoked?"`

### Workaround Options

1. **Use local SDK evaluation** — Run `strands-agents-evals` HelpfulnessEvaluator locally against captured sessions (as done in `scripts/run_and_compare.py`)
2. **Wait for BYO evaluator support** — AWS may add support for evaluating non-runtime traces in a future release
3. **Use managed runtime for evaluation** — Deploy the same agent to AgentCore Runtime for evaluation, use BYO for development/testing

## Issues Encountered

### 1. Log Export 403 Forbidden
```
Failed to export logs batch code: 403, reason: Forbidden
```
**Cause:** ADOT tries to export logs via OTLP but requires `OTEL_EXPORTER_OTLP_LOGS_HEADERS` with `x-aws-log-group` and `x-aws-log-stream` values. This is expected for BYO agents without a pre-configured CloudWatch log group.

**Impact:** None — traces still export successfully. Only log forwarding fails.

### 2. Evaluator Cannot Score BYO Traces
The `agentcore run eval` command only works with managed runtime traces. BYO traces are visible in X-Ray but not discoverable by the evaluator.

**Workaround:** Use local SDK-based evaluation or managed runtime for scoring.

## Key Differences: BYO vs Managed Runtime

| Aspect | BYO (ADOT) | Managed Runtime |
|--------|-----------|-----------------|
| Agent runs on | Your machine / any compute | AgentCore Runtime |
| Trace export | ADOT → X-Ray/CloudWatch | Automatic via sidecar |
| Service name | Custom (e.g., `multiplier-byo-sonnet`) | Runtime name |
| Evaluator support | ❌ Not yet | ✅ Full support |
| CloudWatch visibility | ✅ X-Ray traces | ✅ GenAI Observability |
| Setup complexity | Env vars + opentelemetry-instrument | agentcore deploy |
| Cost | No runtime cost | Runtime compute cost |

## Environment Variables Reference

| Variable | Value | Purpose |
|----------|-------|---------|
| `AGENT_OBSERVABILITY_ENABLED` | `true` | Enable ADOT agent observability |
| `OTEL_PYTHON_DISTRO` | `aws_distro` | Use AWS OTEL distribution |
| `OTEL_PYTHON_CONFIGURATOR` | `aws_configurator` | Use AWS OTEL configurator |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | Export protocol |
| `OTEL_RESOURCE_ATTRIBUTES` | `service.name=<name>` | Service identity in traces |
| `AWS_PROFILE` | `ml-sandbox` | AWS credentials |
| `AWS_REGION` | `us-east-1` | Target region |
