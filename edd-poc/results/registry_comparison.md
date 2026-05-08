# Agent Registry Comparison — All 6 Agents

**Generated:** 2026-05-08 15:38:29
**Agents:** 3 managed (AgentCore Runtime) + 3 BYO (ADOT → CloudWatch)
**Prompts:** 5 per agent = 30 total invocations
**Execution:** ThreadPoolExecutor(max_workers=6) — all agents run concurrently

---

## Registry Overview

| Agent | Model | Type | Last Eval Score | Last Eval Time |
|-------|-------|------|----------------|----------------|
| multiplier_hr_sonnet | us.anthropic.claude-sonnet-4-6 | managed | {"correctness": 0.2, "helpfulness": 0.667} | 2026-05-08T07:38:27 |
| multiplier_hr_nova_2_pro | us.amazon.nova-2-pro-v1:0 | managed | {"correctness": 0.4, "helpfulness": 0.8} | 2026-05-08T07:38:27 |
| multiplier_hr_glm_5 | zai.glm-5 | managed | {"correctness": 0.4, "helpfulness": 0.766} | 2026-05-08T07:38:27 |
| multiplier_byo_sonnet | us.anthropic.claude-sonnet-4-6 | byo | {"correctness": 0.4, "helpfulness": 0.8} | 2026-05-08T07:38:27 |
| multiplier_byo_nova_2_pro | us.amazon.nova-2-pro-v1:0 | byo | {"correctness": 0.4, "helpfulness": 0.9} | 2026-05-08T07:38:27 |
| multiplier_byo_glm_5 | zai.glm-5 | byo | {"correctness": 0.2, "helpfulness": 0.866} | 2026-05-08T07:38:27 |

---

## Phase 1: Invocation Results (30 invocations)

### multiplier_hr_sonnet (managed)

- Success: 5/5
- Avg latency: 16930ms

| # | Prompt | Status | Latency |
|---|--------|--------|---------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | 13834ms |
| 2 | What are the notice periods and regulatory requirements... | ✓ | 16297ms |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | 18594ms |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✓ | 16067ms |
| 5 | For employee EMP-11111 in India, look up their details,... | ✓ | 19859ms |

### multiplier_hr_nova_2_pro (managed)

- Success: 5/5
- Avg latency: 17677ms

| # | Prompt | Status | Latency |
|---|--------|--------|---------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | 15398ms |
| 2 | What are the notice periods and regulatory requirements... | ✓ | 17146ms |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | 17982ms |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✓ | 15135ms |
| 5 | For employee EMP-11111 in India, look up their details,... | ✓ | 22726ms |

### multiplier_hr_glm_5 (managed)

- Success: 5/5
- Avg latency: 18090ms

| # | Prompt | Status | Latency |
|---|--------|--------|---------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | 15271ms |
| 2 | What are the notice periods and regulatory requirements... | ✓ | 19250ms |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | 17392ms |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✓ | 14536ms |
| 5 | For employee EMP-11111 in India, look up their details,... | ✓ | 24000ms |

### multiplier_byo_sonnet (byo)

- Success: 5/5
- Avg latency: 12178ms

| # | Prompt | Status | Latency |
|---|--------|--------|---------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | 9189ms |
| 2 | What are the notice periods and regulatory requirements... | ✓ | 13277ms |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | 11780ms |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✓ | 9048ms |
| 5 | For employee EMP-11111 in India, look up their details,... | ✓ | 17595ms |

### multiplier_byo_nova_2_pro (byo)

- Success: 5/5
- Avg latency: 9129ms

| # | Prompt | Status | Latency |
|---|--------|--------|---------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | 17618ms |
| 2 | What are the notice periods and regulatory requirements... | ✓ | 6261ms |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | 6304ms |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✓ | 6045ms |
| 5 | For employee EMP-11111 in India, look up their details,... | ✓ | 9419ms |

### multiplier_byo_glm_5 (byo)

- Success: 5/5
- Avg latency: 25331ms

| # | Prompt | Status | Latency |
|---|--------|--------|---------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | 17500ms |
| 2 | What are the notice periods and regulatory requirements... | ✓ | 15492ms |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | 19796ms |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✓ | 23483ms |
| 5 | For employee EMP-11111 in India, look up their details,... | ✓ | 50385ms |

---

## Phase 2: Evaluation Results (Ground-Truth Comparison)

**Baseline:** Sonnet (in-memory invocation, HelpfulnessEvaluator)
**Contenders:** All 6 agents evaluated with CorrectnessEvaluator vs Sonnet baseline

### Summary

| Agent | Type | Avg Correctness | Avg Helpfulness |
|-------|------|----------------|-----------------|
| sonnet (baseline) | — | — | 1.000 |
| multiplier_hr_sonnet | managed | 0.200 | 0.667 |
| multiplier_hr_nova_2_pro | managed | 0.400 | 0.800 |
| multiplier_hr_glm_5 | managed | 0.400 | 0.766 |
| multiplier_byo_sonnet | byo | 0.400 | 0.800 |
| multiplier_byo_nova_2_pro | byo | 0.400 | 0.900 |
| multiplier_byo_glm_5 | byo | 0.200 | 0.866 |

### Per-Prompt Correctness (vs Sonnet Baseline)

| # | Prompt | multiplier_hr_sonnet | multiplier_hr_nova_2 | multiplier_hr_glm_5 | multiplier_byo_sonne | multiplier_byo_nova_ | multiplier_byo_glm_5 |
|---|--------| --- | --- | --- | --- | --- | --- |
| 1 | What is the employment status of employee EMP... | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| 2 | What are the notice periods and regulatory re... | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| 3 | Calculate the monthly payroll breakdown for a... | ✗ | ✗ | ✗ | ✗ | ✓ | ✓ |
| 4 | Check the leave balance for employee EMP-6789... | ✗ | ✓ | ✓ | ✓ | ✗ | ✗ |
| 5 | For employee EMP-11111 in India, look up thei... | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

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