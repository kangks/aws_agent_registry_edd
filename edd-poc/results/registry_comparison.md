# Agent Registry Comparison — All 6 Agents

**Generated:** 2026-05-08 09:41:34
**Agents:** 3 managed (AgentCore Runtime) + 3 BYO (ADOT → CloudWatch)
**Prompts:** 5 per agent = 30 total invocations
**Execution:** ThreadPoolExecutor(max_workers=6) — all agents run concurrently

---

## Registry Overview

| Agent | Model | Type | Last Eval Score | Last Eval Time |
|-------|-------|------|----------------|----------------|
| multiplier_hr_sonnet | us.anthropic.claude-sonnet-4-6 | managed | {"correctness": 1.0, "helpfulness": 1.0} | 2026-05-08T01:41:29 |
| multiplier_hr_haiku | us.anthropic.claude-haiku-4-5- | managed | {"correctness": 1.0, "helpfulness": 0.933} | 2026-05-08T01:41:29 |
| multiplier_hr_nova_pro | us.amazon.nova-pro-v1:0 | managed | {"correctness": 0.8, "helpfulness": 0.867} | 2026-05-08T01:41:29 |
| multiplier_byo_sonnet | us.anthropic.claude-sonnet-4-6 | byo | {"correctness": 1.0, "helpfulness": 1.0} | 2026-05-08T01:41:29 |
| multiplier_byo_haiku | us.anthropic.claude-haiku-4-5- | byo | {"correctness": 1.0, "helpfulness": 1.0} | 2026-05-08T01:41:29 |
| multiplier_byo_nova_pro | us.amazon.nova-pro-v1:0 | byo | {"correctness": 1.0, "helpfulness": 0.833} | 2026-05-08T01:41:29 |

---

## Phase 1: Invocation Results (30 invocations)

### multiplier_hr_sonnet (managed)

- Success: 5/5
- Avg latency: 17096ms

| # | Prompt | Status | Latency |
|---|--------|--------|---------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | 18047ms |
| 2 | What are the notice periods and regulatory requirements... | ✓ | 17439ms |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | 16868ms |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✓ | 13487ms |
| 5 | For employee EMP-11111 in India, look up their details,... | ✓ | 19637ms |

### multiplier_hr_haiku (managed)

- Success: 5/5
- Avg latency: 16774ms

| # | Prompt | Status | Latency |
|---|--------|--------|---------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | 13881ms |
| 2 | What are the notice periods and regulatory requirements... | ✓ | 17864ms |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | 15060ms |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✓ | 13548ms |
| 5 | For employee EMP-11111 in India, look up their details,... | ✓ | 23516ms |

### multiplier_hr_nova_pro (managed)

- Success: 5/5
- Avg latency: 15937ms

| # | Prompt | Status | Latency |
|---|--------|--------|---------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | 14928ms |
| 2 | What are the notice periods and regulatory requirements... | ✓ | 15604ms |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | 15572ms |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✓ | 12635ms |
| 5 | For employee EMP-11111 in India, look up their details,... | ✓ | 20945ms |

### multiplier_byo_sonnet (byo)

- Success: 5/5
- Avg latency: 11882ms

| # | Prompt | Status | Latency |
|---|--------|--------|---------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | 9708ms |
| 2 | What are the notice periods and regulatory requirements... | ✓ | 12153ms |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | 11646ms |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✓ | 8328ms |
| 5 | For employee EMP-11111 in India, look up their details,... | ✓ | 17577ms |

### multiplier_byo_haiku (byo)

- Success: 5/5
- Avg latency: 7517ms

| # | Prompt | Status | Latency |
|---|--------|--------|---------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | 6631ms |
| 2 | What are the notice periods and regulatory requirements... | ✓ | 7222ms |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | 7145ms |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✓ | 6414ms |
| 5 | For employee EMP-11111 in India, look up their details,... | ✓ | 10172ms |

### multiplier_byo_nova_pro (byo)

- Success: 5/5
- Avg latency: 7320ms

| # | Prompt | Status | Latency |
|---|--------|--------|---------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | 6631ms |
| 2 | What are the notice periods and regulatory requirements... | ✓ | 6492ms |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | 6559ms |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✓ | 6043ms |
| 5 | For employee EMP-11111 in India, look up their details,... | ✓ | 10875ms |

---

## Phase 2: Evaluation Results (Ground-Truth Comparison)

**Baseline:** Sonnet (in-memory invocation, HelpfulnessEvaluator)
**Contenders:** All 6 agents evaluated with CorrectnessEvaluator vs Sonnet baseline

### Summary

| Agent | Type | Avg Correctness | Avg Helpfulness |
|-------|------|----------------|-----------------|
| sonnet (baseline) | — | — | 1.000 |
| multiplier_hr_sonnet | managed | 1.000 | 1.000 |
| multiplier_hr_haiku | managed | 1.000 | 0.933 |
| multiplier_hr_nova_pro | managed | 0.800 | 0.867 |
| multiplier_byo_sonnet | byo | 1.000 | 1.000 |
| multiplier_byo_haiku | byo | 1.000 | 1.000 |
| multiplier_byo_nova_pro | byo | 1.000 | 0.833 |

### Per-Prompt Correctness (vs Sonnet Baseline)

| # | Prompt | multiplier_hr_sonnet | multiplier_hr_haiku | multiplier_hr_nova_p | multiplier_byo_sonne | multiplier_byo_haiku | multiplier_byo_nova_ |
|---|--------| --- | --- | --- | --- | --- | --- |
| 1 | What is the employment status of employee EMP... | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 2 | What are the notice periods and regulatory re... | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 3 | Calculate the monthly payroll breakdown for a... | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 4 | Check the leave balance for employee EMP-6789... | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 5 | For employee EMP-11111 in India, look up thei... | ✓ | ✓ | ✗ | ✓ | ✓ | ✓ |

---

## Stale Agent Detection

All agents have been evaluated within the last 7 days. ✅

---

## How to Run

```bash
AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \
  .venv/bin/python scripts/run_registry_comparison.py
```

This script:
1. Loads all 6 agents from registry.json
2. Runs all 6 concurrently (ThreadPoolExecutor, max_workers=6)
3. Each agent runs 5 prompts sequentially (avoids per-model throttling)
4. Phase 2: In-memory evaluation (ground-truth vs contender)
5. Updates registry.json with latest scores
6. Generates this report