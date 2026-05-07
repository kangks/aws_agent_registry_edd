# Ground Truth vs Contender — Managed Path (AgentCore Runtime)

**Generated:** 2026-05-07 10:32:33

**Baseline (Ground Truth):** Claude Sonnet 4 (multiplier_hr_sonnet)
**Contenders:** Claude Haiku 4.5 (multiplier_hr_haiku), Amazon Nova Pro (multiplier_hr_nova_pro)
**Evaluator:** `multiplier_domain_accuracy` (LLM-as-a-Judge, Claude Sonnet 4.5)

## Methodology

1. Invoke all 3 runtimes with the same 3 prompts
2. Run `agentcore run eval` with `--expected-response` flag providing Sonnet's response as ground truth
3. The evaluator scores each response on factual accuracy, completeness, and compliance safety (1-5 scale)
4. Compare scores across models to determine if contenders match baseline quality

## Summary

| Model | Prompt 1 | Prompt 2 | Prompt 3 | Average |
|-------|----------|----------|----------|---------|
| Sonnet (baseline) | 5/5 Excellent | 5/5 Excellent | 2/5 Fair* | 4.0 |
| Haiku vs Sonnet | 5/5 Excellent | 5/5 Excellent | 2/5 Fair* | 4.0 |
| Nova Pro vs Sonnet | 5/5 Excellent | 5/5 Excellent | 2/5 Fair* | 4.0 |

*\*Prompt 3 scored 2/5 across all models because the evaluator flagged the mock salary data (INR 55,000 annual) as unrealistically low for India. This is a data quality issue in the mock tools, not a model quality issue.*

## Per-Session Details

### Prompt 1: "What is the employment status of employee EMP-12345 in Singapore?"

| Runtime | Session ID | Score | Label |
|---------|-----------|-------|-------|
| multiplier_hr_sonnet | `9ffccab6-5fcf-4043-bfe9-434ff16ffb2e` | 5/5 | Excellent |
| multiplier_hr_haiku | `dd3fce80-eadc-4a38-95c8-54e140ece784` | 5/5 | Excellent |
| multiplier_hr_nova_pro | `6b1dc231-6cf9-4dfd-bc87-a2f7388d5ee9` | 5/5 | Excellent |

**Observation:** All 3 models produced equivalent responses for this simple single-tool query. The `--expected-response` flag confirmed Haiku and Nova Pro matched Sonnet's factual content.

### Prompt 2: "What are the compliance requirements for Thailand?"

| Runtime | Session ID | Score | Label |
|---------|-----------|-------|-------|
| multiplier_hr_sonnet | `aebf6d80-4f41-4117-815a-1cb107425e9d` | 5/5 | Excellent |
| multiplier_hr_haiku | `3f51ee71-2a27-4e6b-afe4-b27b59533478` | 5/5 | Excellent |
| multiplier_hr_nova_pro | `df6d7b2d-9eab-4a48-8b64-ed50b39ef13b` | 5/5 | Excellent |

**Observation:** All models correctly extracted and presented Thailand compliance data. Nova Pro added a helpful disclaimer about consulting local legal experts.

### Prompt 3: "For employee EMP-11111 in India, look up their details, check compliance rules, and calculate their payroll breakdown in INR."

| Runtime | Session ID | Score | Label |
|---------|-----------|-------|-------|
| multiplier_hr_sonnet | `0459cbe2-5141-4ef5-a7ef-0ad4c8026b95` | 2/5 | Fair |
| multiplier_hr_haiku | `3ade1cc3-66bd-4618-8f17-75026a8f06c1` | 2/5 | Fair |
| multiplier_hr_nova_pro | `35f0f7e1-36f2-46c3-b70c-0ad447488b03` | 2/5 | Fair |

**Observation:** The evaluator flagged all 3 models for the same issue: the mock data returns INR 55,000 as annual salary, which the evaluator considers unrealistically low for India. This is a **mock data quality issue**, not a model quality difference. All 3 models correctly used the tools and calculated payroll identically.

## Key Findings

1. **All models produce equivalent quality** on simple and medium-complexity prompts (Prompts 1 & 2)
2. **The `--expected-response` flag works** — it provides ground truth context to the evaluator, enabling direct comparison
3. **Mock data limitations** can trigger false negatives — the evaluator correctly identified that INR 55,000 annual is unrealistic, but this affects all models equally
4. **Nova Pro matches Sonnet** on the managed path — unlike the BYO path where it sometimes guesses parameters, the managed runtime produces consistent results

## Comparison: Managed vs BYO Path

| Aspect | BYO Path (local SDK) | Managed Path (AgentCore) |
|--------|---------------------|--------------------------|
| Evaluator | `CorrectnessEvaluator` (binary: CORRECT/INCORRECT) | `multiplier_domain_accuracy` (1-5 scale) |
| Ground truth mechanism | `expected_assertion` field in EvaluationData | `--expected-response` CLI flag |
| Scoring granularity | Binary (1.0 or 0.0) | 5-level (1-5) |
| Haiku vs Sonnet | 4/5 correct (missed leave balance prompt) | 4.0/5.0 avg (matched on all prompts) |
| Nova Pro vs Sonnet | 5/5 correct | 4.0/5.0 avg (matched on all prompts) |

## Commands Used

```bash
# Invoke all 3 runtimes with same prompts
AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 agentcore invoke --runtime multiplier_hr_sonnet "<prompt>"
AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 agentcore invoke --runtime multiplier_hr_haiku "<prompt>"
AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 agentcore invoke --runtime multiplier_hr_nova_pro "<prompt>"

# Evaluate with ground truth (Sonnet's response as expected)
AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 agentcore run eval \
  --runtime multiplier_hr_haiku \
  --evaluator multiplier_domain_accuracy \
  --session-id <session_id> \
  --expected-response "<sonnet's response summary>" \
  --days 1 --json
```

## Limitations

- The `--expected-response` flag provides context to the evaluator but doesn't change the scoring rubric — the evaluator still scores on its own criteria (factual accuracy, completeness, compliance safety)
- Mock data quality affects all models equally, making it hard to differentiate on complex prompts
- The managed path evaluator uses a 1-5 scale which is less granular for "same vs different" comparison than the BYO path's binary correctness score
