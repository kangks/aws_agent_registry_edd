# BYO Full End-to-End — Ground Truth vs Contender

**Generated:** 2026-05-07 18:26:14
**Baseline:** Claude Sonnet 4 (`us.anthropic.claude-sonnet-4-6`)
**Contenders:** Claude Haiku 4.5, Amazon Nova Pro
**Prompts:** 5 (same as managed path)
**CloudWatch traces:** 15 (5 per model × 3 models)

## Parity with Managed Path

| Aspect | Managed Path | BYO Path |
|--------|-------------|----------|
| Prompts per model | 5 | 5 ✅ |
| Traces to CloudWatch | ✅ (runtime sidecar) | ✅ (ADOT) |
| Service names | `eddpoc_multiplier_hr_*.DEFAULT` | `multiplier-byo-*` |
| Evaluator | `multiplier_domain_accuracy` (1-5) | `CorrectnessEvaluator` (binary) |
| Ground truth | `--expected-response` CLI flag | `expected_assertion` field |

## Summary

| Comparison | Avg Correctness vs Baseline | Avg Helpfulness |
|------------|---------------------------|-----------------|
| Sonnet (baseline) | — | 1.000 |
| Haiku vs Sonnet | 0.600 (3/5 correct) | 0.700 |
| Nova Pro vs Sonnet | 1.000 (5/5 correct) | 0.900 |

## Per-Prompt Correctness (vs Sonnet Baseline)

| # | Prompt | Haiku | Nova Pro |
|---|--------|-------|----------|
| 1 | What is the employment status of employee EMP-12345 in ... | ✓ | ✓ |
| 2 | What are the notice periods and regulatory requirements... | ✓ | ✓ |
| 3 | Calculate the monthly payroll breakdown for an employee... | ✓ | ✓ |
| 4 | Check the leave balance for employee EMP-67890. How man... | ✗ | ✓ |
| 5 | For employee EMP-11111 in India, look up their details,... | ✗ | ✓ |

## Per-Prompt Helpfulness (Absolute Quality)

| # | Prompt | Sonnet | Haiku | Nova Pro |
|---|--------|--------|-------|----------|
| 1 | What is the employment status of employee EMP-12345 in ... | 1.000 | 0.833 | 0.833 |
| 2 | What are the notice periods and regulatory requirements... | 1.000 | 0.833 | 0.833 |
| 3 | Calculate the monthly payroll breakdown for an employee... | 1.000 | 0.833 | 0.833 |
| 4 | Check the leave balance for employee EMP-67890. How man... | 1.000 | 0.667 | 1.000 |
| 5 | For employee EMP-11111 in India, look up their details,... | 1.000 | 0.333 | 1.000 |

## Response Times (ms) — ADOT Invocations (CloudWatch traces)

| # | Sonnet | Haiku | Nova Pro |
|---|--------|-------|----------|
| 1 | 52597 | 7275 | 6270 |
| 2 | 125005 | 7110 | 6368 |
| 3 | 62743 | 6554 | 6322 |
| 4 | 185343 | 6392 | 6019 |
| 5 | 186985 | 10160 | 12096 |

## Correctness Reasoning

### Haiku vs Sonnet

**Prompt 1:** What is the employment status of employee EMP-12345 in Singa...
- Verdict: CORRECT ✓
- Reason: The agent's response contains all the same core factual information as the expected response: Employee EMP-12345 in Singapore is Active, Department is Engineering, Annual Salary is SGD 95,000, and Start Date is March 15, 2022. The only differences ar

**Prompt 2:** What are the notice periods and regulatory requirements for ...
- Verdict: CORRECT ✓
- Reason: The agent's response contains all the core factual information from the expected response:
- Notice periods: Probation = None, Confirmed = 1 pay cycle ✓
- Regulatory requirements: Labour Protection Act, Social Security contributions, Work permit for 

**Prompt 3:** Calculate the monthly payroll breakdown for an employee earn...
- Verdict: CORRECT ✓
- Reason: The agent's response contains all the core factual values present in the expected response: Gross Monthly Salary (SGD 7,500), Tax Deduction (SGD 1,125), Social Security (SGD 1,500), Net Monthly Salary (SGD 4,875), and Total Employer Cost (SGD 8,775).

**Prompt 4:** Check the leave balance for employee EMP-67890. How many day...
- Verdict: INCORRECT ✗
- Reason: The agent failed to provide any leave balance information for EMP-67890. Instead, it asked for an additional parameter (country) that wasn't required by the user query. The expected response clearly shows the employee has 21 days entitlement, used 8 

**Prompt 5:** For employee EMP-11111 in India, look up their details, chec...
- Verdict: INCORRECT ✗
- Reason: The agent's response correctly captures the core payroll breakdown figures: Gross Monthly Salary (INR 4,583.33), Tax Deduction (INR 916.67), Social Security/PF/ESI (INR 550.00), Net Monthly Salary (INR 3,116.67), and Total Employer Cost (INR 5,133.33

### Nova Pro vs Sonnet

**Prompt 1:** What is the employment status of employee EMP-12345 in Singa...
- Verdict: CORRECT ✓
- Reason: The agent's response contains the same core factual information as the expected response: employee EMP-12345 in Singapore is active, in the Engineering department, with a salary of $95,000 (SGD 95,000), and a start date of March 15, 2022. The formatt

**Prompt 2:** What are the notice periods and regulatory requirements for ...
- Verdict: CORRECT ✓
- Reason: The agent's response covers all the core factual content from the expected response:
- Notice periods: Probation = None, Confirmed = 1 pay cycle ✓
- Benefits: Social Security Fund, Annual leave (6 days), Sick leave (30 days) ✓
- Tax rates: Resident 0

**Prompt 3:** Calculate the monthly payroll breakdown for an employee earn...
- Verdict: CORRECT ✓
- Reason: The agent's response contains all the same core numerical values as the expected response:
- Gross Monthly: SGD 7,500 ✓
- Tax: SGD 1,125 ✓
- Social Security: SGD 1,500 ✓
- Net Monthly: SGD 4,875 ✓
- Employer Cost: SGD 8,775 ✓

All critical factual fi

**Prompt 4:** Check the leave balance for employee EMP-67890. How many day...
- Verdict: CORRECT ✓
- Reason: The agent's response contains all the core factual information: Annual Entitlement of 21 days, Used Days of 8 days, and Remaining Balance of 13 days for employee EMP-67890. The expected response contains the same figures. The agent's response lacks s

**Prompt 5:** For employee EMP-11111 in India, look up their details, chec...
- Verdict: CORRECT ✓
- Reason: The agent's response contains all the same core factual information as the expected response:
- Employee details (EMP-11111, Active, Product dept, 55,000 INR, Jan 10, 2023)
- Identical payroll breakdown figures (Gross 4,583.33, Tax 916.67, Social Sec

## Key Observations

- **Haiku:** 3/5 prompts CORRECT vs Sonnet baseline
- **Nova Pro:** 5/5 prompts CORRECT vs Sonnet baseline

## How to Run

```bash
AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \
  .venv/bin/python scripts/run_byo_full.py
```

Phase 1 sends 15 traces to CloudWatch via ADOT.
Phase 2 runs 15 in-memory invocations for evaluation (no CloudWatch).