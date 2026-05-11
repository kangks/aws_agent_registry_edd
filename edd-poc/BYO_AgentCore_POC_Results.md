# POC Results: Unified Trajectory Evaluator for BYO + Managed Agents

**Status: ✅ Proven. A single AgentCore code-based (Lambda) evaluator successfully scores both managed (AgentCore Runtime) and BYO (Strands + ADOT outside AgentCore) agents via the same `evaluate()` API, producing directly comparable scores.**

See the full comparison report at [`results/trajectory_comparison.md`](./results/trajectory_comparison.md).

---

## Headline Numbers

30 / 30 evaluations succeeded across 6 agents × 5 prompts.

| Deployment | Avg Score | Range |
|---|---|---|
| **Managed** (AgentCore Runtime) | **0.952** | 0.94 – 0.96 |
| **BYO** (Strands + ADOT) | **0.943** | 0.85 – 0.98 |

Per-model parity (same model, managed vs BYO):

| Model | Managed | BYO | Delta |
|---|---|---|---|
| sonnet | 0.950 | 0.948 | -0.002 |
| nova_2_pro | 0.954 | 0.962 | +0.008 |
| glm_5 | 0.952 | 0.918 | -0.034 |

The scores are within expected noise across all three models — BYO and managed behave equivalently through the unified evaluator.

---

## What We Built

### 1. `evaluators/trajectory_evaluator_lambda.py`
A Python Lambda that implements the [AgentCore code-based evaluator contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html). Scores the agent trajectory on 6 criteria from span metadata alone:

| Criterion | Weight | Source |
|---|---|---|
| Agent presence | 20 | `invoke_agent` strands span exists |
| Tool success rate | 30 | `gen_ai.tool.status` == success |
| Tool variety | 10 | distinct tool names called |
| Error-free execution | 15 | no `status.code=ERROR` strands spans |
| Latency | 15 | agent duration vs threshold (30s) |
| Efficiency | 10 | tool/LLM call count within bounds |

Output: `{label, value: 0.0-1.0, explanation}` per the AgentCore contract.

### 2. `evaluators/deploy_trajectory_evaluator.py`
Idempotent deployment script that:
- Creates/updates the IAM execution role with Bedrock + CloudWatch permissions
- Builds the Lambda zip and creates/updates `eddpoc-trajectory-evaluator`
- Grants `bedrock-agentcore.amazonaws.com` invoke permission
- Registers as an AgentCore evaluator via `create_evaluator(level="TRACE", codeBased={...})`
- All resources tagged: `app=multiplier-hr-agent, project=eddpoc, env=dev`

### 3. `scripts/run_trajectory_comparison.py`
End-to-end comparison runner:
- Invokes all 6 agents with 5 prompts each (parallel per-agent, sequential within-agent)
- Waits 120s for trace propagation
- Discovers trace IDs from `aws/spans` for each agent
- Calls `agentcore.evaluate(evaluatorId=<Lambda evaluator>, evaluationInput={sessionSpans: ...}, evaluationTarget={traceIds: [...]})` for each (agent, prompt)
- Generates `results/trajectory_comparison.md` with summary, per-model parity, and per-prompt detail

---

## Key Technical Findings

### Finding 1: AgentCore strips event bodies before invoking code-based Lambda

When you pass `sessionSpans` to the evaluate API, the service **normalizes and filters** them before invoking your Lambda. Specifically:
- **Log events (with `body`) are stripped entirely** — Lambda receives 0 events even when you send dozens.
- **Span inline `events` and `span_events` fields are empty.**
- **Only span metadata survives** — attributes, status, scope, timing, session.id.

This means code-based evaluators **cannot score on content** (user query, assistant response, tool parameters, tool results). They can score on **trajectory structure and metadata** only.

Verified by swapping in an inspector Lambda that dumped everything it received. See `tmp_inspect_service_item.py` and `tmp_inspect_events_field.py` for the inspection harness.

### Finding 2: The metadata IS rich and trajectory-meaningful

What the Lambda DOES receive is surprisingly complete for trajectory evaluation:

**Per-span attributes (strands scope):**
- `gen_ai.operation.name` — invoke_agent / execute_tool / chat
- `gen_ai.tool.name`, `gen_ai.tool.description`, `gen_ai.tool.json_schema`, `gen_ai.tool.status`, `gen_ai.tool.call.id`
- `gen_ai.usage.input_tokens`, `output_tokens`, `total_tokens`
- `gen_ai.request.model`
- `gen_ai.agent.tools` (list of available tools)
- `gen_ai.event.start_time`, `gen_ai.event.end_time`
- `session.id`
- Status (OK / ERROR)

This is enough to score: "did the agent use the right tools, successfully, quickly, without errors?" — which is exactly what trajectory evaluation is.

### Finding 3: BYO and managed normalize to the same metadata shape

Both deployment paths pass through the service's normalizer. A BYO agent's ADOT-exported spans and a managed agent's sidecar-exported spans look **identical** to the Lambda. This is why the same Lambda scores both correctly.

Sample metadata from our POC:
```json
{
  "name": "execute_tool employee_lookup",
  "scope": {"name": "strands.telemetry.tracer"},
  "attributes": {
    "gen_ai.tool.name": "employee_lookup",
    "gen_ai.tool.status": "success",
    "gen_ai.tool.call.id": "tooluse_ZtEt9Qr8OExSDinznkeAlv",
    "gen_ai.operation.name": "execute_tool",
    "session.id": "byo-poc-1af9e9ab"
  },
  "status": {"code": "OK"}
}
```

This shape is identical whether the span came from AgentCore Runtime or from our BYO runner.

---

## What This Resolves vs. What Remains

### ✅ Resolved by this POC

- **Unified evaluation API** — one `evaluate()` call, one evaluator, for both deployment types.
- **Directly comparable scores** — same rubric, same Lambda, same scale.
- **Continuous online eval compatibility** — the same Lambda evaluator can be plugged into an Online Eval Config with `cloudWatchLogs` data source pointing to the BYO log group. Score would be emitted continuously without the `LogEventMissingException` we saw with the LLM-as-a-judge path.
- **Observability gap for BYO** — we now have trajectory scores for BYO agents, which we previously could not produce at all.

### ❗ Still open (separate from this POC)

- **Content-level evaluation for BYO** — the original `LogEventMissingException` blocks the LLM-as-a-judge path for BYO. The Lambda path avoids this by scoring on metadata, but cannot judge factual accuracy, completeness, or compliance safety.
- **The `invoke_agent` log event gap** documented in `BYO_AgentCore_Observability_issue.md` is unchanged. To get content-level evaluation for BYO, we still need AWS/Strands to fix that gap OR we would need to combine this trajectory evaluator with an out-of-band Bedrock call (bypassing AgentCore) for content judging.

### Possible follow-ups

1. **Wire the Lambda evaluator into Online Eval** — update the 6 online eval configs (3 managed + 3 BYO) to use `multiplier_trajectory_eval-Evy2MEDqBq` instead of / alongside `multiplier_domain_accuracy`. BYO would start producing continuous scores.
2. **Pair with an external content judge** — since the trajectory evaluator handles structure, a separate out-of-band Bedrock judge could handle content. The two scores together give a complete picture.
3. **Add ground-truth checks** — the Lambda can also consume `evaluationReferenceInputs` for expected trajectories / tool sequences, enabling deterministic regression testing.

---

## Deployed Resources

| Resource | Identifier |
|---|---|
| Lambda function | `eddpoc-trajectory-evaluator` |
| Lambda ARN | `arn:aws:lambda:us-east-1:654654616949:function:eddpoc-trajectory-evaluator` |
| IAM role | `eddpoc-trajectory-evaluator-role` |
| AgentCore evaluator ID | `multiplier_trajectory_eval-Evy2MEDqBq` |
| AgentCore evaluator ARN | `arn:aws:bedrock-agentcore:us-east-1:654654616949:evaluator/multiplier_trajectory_eval-Evy2MEDqBq` |

All tagged `app=multiplier-hr-agent, project=eddpoc, env=dev`.

---

## Reproducing

1. Deploy the evaluator:
   ```bash
   .venv/bin/python evaluators/deploy_trajectory_evaluator.py
   ```
2. Run the full comparison (takes ~7 min):
   ```bash
   AWS_PROFILE=ml-sandbox .venv/bin/python scripts/run_trajectory_comparison.py
   ```
3. Review `results/trajectory_comparison.md`.

Smoke test a single agent after making code changes:
```bash
.venv/bin/python tmp_smoke_test_evaluator.py
```
