# Ground Truth vs Contender — Model Comparison

**Generated:** 2026-05-07 10:23:16

**Baseline (Ground Truth):** Claude Sonnet 4 (us.anthropic.claude-sonnet-4-6)
**Contenders:** Claude Haiku 4.5, Amazon Nova Pro

## Methodology

Instead of scoring each model independently, this comparison:
1. Runs Sonnet (the "ground truth" / baseline) against all prompts
2. Runs each contender (Haiku, Nova Pro) against the same prompts
3. Evaluates each contender's response AGAINST Sonnet's response using `CorrectnessEvaluator`
4. This answers: "Does the contender produce responses as good as the baseline?"

The `CorrectnessEvaluator` with `expected_assertion` compares the contender's response
against the baseline's response and returns CORRECT (1.0) or INCORRECT (0.0).

`HelpfulnessEvaluator` provides an absolute quality score (0.0–1.0) for each model independently.

## Summary

| Comparison | Avg Correctness vs Baseline | Avg Helpfulness |
|------------|---------------------------|-----------------|
| Sonnet (baseline) | — | 1.000 |
| Haiku vs Sonnet | 0.800 | 0.800 |
| Nova Pro vs Sonnet | 1.000 | 0.833 |

## Per-Prompt Correctness (vs Sonnet Baseline)

| Prompt | Haiku | Nova Pro |
| --- | --- | --- |
| What is the employment status of employee EMP-12345 in Singa... | 1.000 | 1.000 |
| What are the notice periods and regulatory requirements for ... | 1.000 | 1.000 |
| Calculate the monthly payroll breakdown for an employee earn... | 1.000 | 1.000 |
| Check the leave balance for employee EMP-67890. How many day... | 0.000 | 1.000 |
| For employee EMP-11111 in India, look up their details, chec... | 1.000 | 1.000 |

## Per-Prompt Helpfulness (Absolute Quality)

| Prompt | Sonnet | Haiku | Nova Pro |
| --- | --- | --- | --- |
| What is the employment status of employee EMP-12345 in Singa... | 1.000 | 1.000 | 1.000 |
| What are the notice periods and regulatory requirements for ... | 1.000 | 1.000 | 0.833 |
| Calculate the monthly payroll breakdown for an employee earn... | 1.000 | 0.833 | 0.667 |
| Check the leave balance for employee EMP-67890. How many day... | 1.000 | 0.333 | 0.833 |
| For employee EMP-11111 in India, look up their details, chec... | 1.000 | 0.833 | 0.833 |

## Correctness Evaluation Reasoning

Detailed reasoning from the CorrectnessEvaluator for each contender vs baseline:

### Haiku

**Prompt 1:** What is the employment status of employee EMP-12345 in Singa...
- Verdict: CORRECT ✓
- Reasoning: The agent's response contains all the same core factual information as the expected response: employee EMP-12345 in Singapore is active, works in Engineering, earns SGD 95,000 annually, and started on March 15, 2022. The format differs (bullet list vs. table), and the date is written slightly differ

**Prompt 2:** What are the notice periods and regulatory requirements for ...
- Verdict: CORRECT ✓
- Reasoning: The agent's response covers all the same core factual content as the expected response: notice periods (none for probation, 1 pay cycle for confirmed), regulatory requirements (Labour Protection Act, Social Security, work permits), mandatory benefits (Social Security Fund, 6 days annual leave, 30 da

**Prompt 3:** Calculate the monthly payroll breakdown for an employee earn...
- Verdict: CORRECT ✓
- Reasoning: The agent's response contains all the core factual information matching the expected response:
- Gross Monthly Salary: SGD 7,500.00 ✓
- Tax Deduction: SGD 1,125.00 ✓
- Social Security (CPF): SGD 1,500.00 ✓
- Net Monthly Salary: SGD 4,875.00 ✓
- Total Monthly Employer Cost: SGD 8,775.00 ✓

The agent'

**Prompt 4:** Check the leave balance for employee EMP-67890. How many day...
- Verdict: INCORRECT ✗
- Reasoning: The agent did not provide the leave balance information at all. Instead, it asked for the employee's country before proceeding. The expected response clearly provides specific data: 21 days entitlement, 8 days used, and 13 days remaining. The agent's response is fundamentally incomplete and fails to

**Prompt 5:** For employee EMP-11111 in India, look up their details, chec...
- Verdict: CORRECT ✓
- Reasoning: The agent's response contains the same core factual information as the expected response:
- Employee details match (EMP-11111, India, Active, Product department, INR 55,000, January 10, 2023)
- Monthly payroll breakdown is identical (Gross: INR 4,583.33, Tax: INR 916.67, PF/ESI: INR 550.00, Net: INR

### Nova Pro

**Prompt 1:** What is the employment status of employee EMP-12345 in Singa...
- Verdict: CORRECT ✓
- Reasoning: The agent's response contains the same core factual information as the expected response: employee EMP-12345 in Singapore is active, works in Engineering, has a salary of SGD 95,000, and started on 15 March 2022. The differences are purely stylistic (no table format, slightly different wording), but

**Prompt 2:** What are the notice periods and regulatory requirements for ...
- Verdict: CORRECT ✓
- Reasoning: The agent's response covers all the same core factual content as the expected response:
- Notice periods: Probation = None, Confirmed = 1 pay cycle ✓
- Benefits: Social Security Fund, Annual leave (6 days minimum), Sick leave (30 days) ✓
- Tax rates: Resident 0-35%, Non-resident 15% flat ✓
- Regulat

**Prompt 3:** Calculate the monthly payroll breakdown for an employee earn...
- Verdict: CORRECT ✓
- Reasoning: The agent's response contains all the core numerical values that match the expected response exactly: Gross Monthly (SGD 7,500.00), Tax (SGD 1,125.00), Social Security (SGD 1,500.00), Net Monthly (SGD 4,875.00), and Employer Cost (SGD 8,775.00). The agent's response is less detailed — it omits the "

**Prompt 4:** Check the leave balance for employee EMP-67890. How many day...
- Verdict: CORRECT ✓
- Reasoning: The user query is empty, but both the agent response and the expected response address leave balance information for employee EMP-67890. The core factual content in both responses is identical: Annual Entitlement = 21 days, Used Days = 8 days, Remaining Balance = 13 days. The agent response correctl

**Prompt 5:** For employee EMP-11111 in India, look up their details, chec...
- Verdict: CORRECT ✓
- Reasoning: The agent's response contains all the core factual information present in the expected response: employee EMP-11111 details (Active status, Product department, INR 55,000 annual salary, start date 10 January 2023), the complete payroll breakdown (Gross Monthly INR 4,583.33, Tax INR 916.67, Social Se

## Response Times (ms)

| Prompt | Sonnet (baseline) | Haiku | Nova Pro |
| --- | --- | --- | --- |
| Prompt 1 | 5608 | 3626 | 3448 |
| Prompt 2 | 7375 | 3904 | 3437 |
| Prompt 3 | 8329 | 4185 | 3367 |
| Prompt 4 | 6167 | 1874 | 2962 |
| Prompt 5 | 14075 | 6915 | 8678 |

## Key Observations

- **Haiku:** 4/5 prompts rated CORRECT vs Sonnet baseline
- **Nova Pro:** 5/5 prompts rated CORRECT vs Sonnet baseline

- **Evaluation method:** `CorrectnessEvaluator` with `expected_assertion` set to Sonnet's response
- **Scoring:** Binary — CORRECT (1.0) or INCORRECT (0.0) relative to baseline
- **Complementary metric:** `HelpfulnessEvaluator` provides absolute quality (0.0–1.0)
- **Use case:** Determine if a cheaper/faster model can replace the baseline without quality loss

## How to Run

```bash
AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \
  .venv/bin/python scripts/run_groundtruth_comparison.py
```
