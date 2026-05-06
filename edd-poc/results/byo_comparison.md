# BYO Agent — End-to-End Model Comparison with Local SDK Evaluation

**Generated:** 2026-05-07 07:12:55

## Overview

This report demonstrates the **BYO (Bring Your Own)** evaluation path:
the same agent code runs locally and is evaluated using `strands-agents-evals`
SDK evaluators — no AgentCore Runtime needed.

The `agentcore run eval` command only works with managed runtime traces.
This script provides an alternative: capture traces in-memory with
`StrandsEvalsTelemetry`, then evaluate locally with `HelpfulnessEvaluator`.

## Summary

| Model | Avg Helpfulness | Prompts Evaluated |
| --- | --- | --- |
| sonnet (us.anthropic.claude-sonnet-4-6) | 1.000 | 5/5 |
| haiku (us.anthropic.claude-haiku-4-5-20251001-v1:0) | 1.000 | 5/5 |
| nova_pro (us.amazon.nova-pro-v1:0) | 0.867 | 5/5 |

## Per-Prompt Scores

| Prompt | sonnet | haiku | nova_pro |
| --- | --- | --- | --- |
| What is the employment status of employee EMP-12345 in Singa... | 1.000 | 1.000 | 1.000 |
| What are the notice periods and regulatory requirements for ... | 1.000 | 1.000 | 0.833 |
| Calculate the monthly payroll breakdown for an employee earn... | 1.000 | 1.000 | 0.833 |
| Check the leave balance for employee EMP-67890. How many day... | 1.000 | 1.000 | 1.000 |
| For employee EMP-11111 in India, look up their details, chec... | 1.000 | 1.000 | 0.667 |

## Response Times (ms)

| Prompt | sonnet | haiku | nova_pro |
| --- | --- | --- | --- |
| Prompt 1 | 5963 | 4129 | 3254 |
| Prompt 2 | 7589 | 4111 | 3230 |
| Prompt 3 | 7586 | 4751 | 3909 |
| Prompt 4 | 5529 | 3794 | 3013 |
| Prompt 5 | 12584 | 7825 | 6931 |

## How to Run

```bash
AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \
  .venv/bin/python scripts/run_byo_comparison.py --models sonnet,haiku,nova_pro
```

## Key Observations

- **Best performer:** sonnet with avg helpfulness 1.000
- **Evaluation method:** Local SDK-based (`strands-agents-evals` HelpfulnessEvaluator)
- **No AgentCore Runtime required** — proves BYO agents can be fully evaluated locally
- **Same agent code** used across all models (only the model ID changes)
