# AgentCore Multi-Model EDD Comparison Results

**Date:** 2026-05-06
**Evaluator:** `multiplier_domain_accuracy` (LLM-as-a-Judge, Claude Sonnet 4.5)
**Region:** us-east-1
**Profile:** ml-sandbox

---

## Summary Table

| Model | Runtime | Avg Score | Sessions Evaluated |
|-------|---------|-----------|-------------------|
| Claude Sonnet 4 | `multiplier_hr_sonnet` | **4.0** | 5 |
| Claude Haiku 4.5 | `multiplier_hr_haiku` | **4.2** | 5 |
| Amazon Nova Pro | `multiplier_hr_nova_pro` | **4.0** | 5 |

---

## Per-Session Scores

### Claude Sonnet 4 (`us.anthropic.claude-sonnet-4-6`)

| # | Prompt | Score | Label |
|---|--------|-------|-------|
| 1 | Employment status EMP-12345 Singapore | 5 | Excellent |
| 2 | Notice periods & regulatory requirements Thailand | 5 | Excellent |
| 3 | Payroll breakdown 90000 SGD Singapore | 2 | Fair |
| 4 | Leave balance EMP-67890 | 5 | Excellent |
| 5 | Multi-tool: EMP-11111 India (lookup + compliance + payroll) | 3 | Good |

**Average: 4.0**

### Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`)

| # | Prompt | Score | Label |
|---|--------|-------|-------|
| 1 | Employment status EMP-12345 Singapore | 5 | Excellent |
| 2 | Notice periods & regulatory requirements Thailand | 5 | Excellent |
| 3 | Payroll breakdown 90000 SGD Singapore | 2 | Fair |
| 4 | Leave balance EMP-67890 | 5 | Excellent |
| 5 | Multi-tool: EMP-11111 India (lookup + compliance + payroll) | 4 | Very Good |

**Average: 4.2**

### Amazon Nova Pro (`us.amazon.nova-pro-v1:0`)

| # | Prompt | Score | Label |
|---|--------|-------|-------|
| 1 | Employment status EMP-12345 Singapore | 5 | Excellent |
| 2 | Notice periods & regulatory requirements Thailand | 5 | Excellent |
| 3 | Payroll breakdown 90000 SGD Singapore | 2 | Fair |
| 4 | Leave balance EMP-67890 | 5 | Excellent |
| 5 | Multi-tool: EMP-11111 India (lookup + compliance + payroll) | 3 | Good |

**Average: 4.0**

---

## Key Observations

### 1. All models perform identically on straightforward tool-use tasks
- Prompts 1, 2, and 4 (employee lookup, compliance check, leave balance) scored 5/5 across all models
- These tasks require simple tool invocation and accurate data presentation

### 2. Payroll calculation is universally penalized (Score: 2/5 for all models)
- The evaluator (Claude Sonnet 4.5 judge) flags the mock tax rates as unrealistic for Singapore
- The mock `payroll_calculator` uses a flat 15% tax rate, but Singapore's actual progressive system yields ~3-5% effective rate at SGD 90K
- This is a **data quality issue** in the mock tools, not a model capability issue
- All 3 models faithfully report the tool output — the judge penalizes the factual inaccuracy of the underlying data

### 3. Multi-tool orchestration shows slight model differentiation
- Haiku scored 4/5 (Very Good) on the multi-tool prompt vs. 3/5 (Good) for Sonnet and Nova Pro
- The judge penalized Sonnet and Nova Pro for not flagging the implausibly low ₹55,000 annual salary
- Haiku's response was rated more favorably despite the same underlying data

### 4. Response quality is remarkably consistent across models
- All 3 models produce well-formatted markdown tables with emojis
- All correctly invoke the appropriate tools for each query
- The formatting style is nearly identical (likely influenced by the shared system prompt)

### 5. Cost-performance implications
- Haiku (cheapest model) slightly outperforms on average (4.2 vs 4.0)
- For this HR domain with structured tool outputs, the cheaper model is sufficient
- Nova Pro performs on par with Sonnet at a different price point

---

## Session Details

### Sonnet Sessions
| Session ID | Trace ID |
|-----------|----------|
| `69e5d3fd-a026-4345-a6bf-4a8289f09d42` | `69fb548d30d77ce31a88ea7a026b0d54` |
| `01f6dde2-ecc6-450b-a0d8-1425059b0c85` | `69fb54a424cc757518db2f8176433944` |
| `856cb6a6-1d0e-4d4a-9a9d-2ba75ddb7bc8` | `69fb54bf049158382018edee60cca432` |
| `ce75e4c4-ea9e-411d-9ab5-c3b48f08d22d` | `69fb54d85fcbe1060c0c483023c5de2a` |
| `f797dbc2-f11e-46e1-ab41-57e7ddc448c9` | `69fb54ed2956d35d6f437a86785cca4d` |

### Haiku Sessions
| Session ID | Trace ID |
|-----------|----------|
| `e4d6eab8-1511-46cc-a41d-20e49135c09c` | `69fb551253bdd3334f85f56504165298` |
| `ac3d8485-38a1-4232-b89f-a61d8ffafd9a` | `69fb552913640cb21f0ac4f236d03484` |
| `fac1a458-16f1-4fea-9f14-9e12a006f130` | `69fb55411dcd9cfb145141da74194a32` |
| `2e0f2d6a-b39a-49d4-a812-f47b625cff24` | `69fb555956758731522ef0a13bcad27a` |
| `14825201-de84-4334-a4a1-8d95d196bd6f` | `69fb557002ba043576945a977fae1acd` |

### Nova Pro Sessions
| Session ID | Trace ID |
|-----------|----------|
| `58a0eb70-90ce-4b7f-b843-9fd2c794bcc3` | `69fb558f129c0f850d99d0a4607e0144` |
| `b95ad69d-1ab7-4111-ac6d-b59eb8b76917` | `69fb55a62fdc21fe798ad705011a55af` |
| `73cb0057-0ea7-48e1-92ca-3e9c366c098f` | `69fb55bf5eb67b385c97fb4268b516c7` |
| `c9b8d058-2092-4c55-be11-c15d4a670dbc` | `69fb55d77b5ada6e1f554c50081ecf56` |
| `7662a0c8-84e4-40d8-bd07-9ae8410f8be6` | `69fb55ea6aa1c4c2122196102591385b` |

---

## Raw Evaluation Results (JSON)

### Sonnet — Session 1 (Score: 5)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":5,"sessionScores":[{"sessionId":"69e5d3fd-a026-4345-a6bf-4a8289f09d42","traceId":"69fb548d30d77ce31a88ea7a026b0d54","value":5,"label":"Excellent"}]}
```

### Sonnet — Session 2 (Score: 5)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":5,"sessionScores":[{"sessionId":"01f6dde2-ecc6-450b-a0d8-1425059b0c85","traceId":"69fb54a424cc757518db2f8176433944","value":5,"label":"Excellent"}]}
```

### Sonnet — Session 3 (Score: 2)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":2,"sessionScores":[{"sessionId":"856cb6a6-1d0e-4d4a-9a9d-2ba75ddb7bc8","traceId":"69fb54bf049158382018edee60cca432","value":2,"label":"Fair"}]}
```

### Sonnet — Session 4 (Score: 5)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":5,"sessionScores":[{"sessionId":"ce75e4c4-ea9e-411d-9ab5-c3b48f08d22d","traceId":"69fb54d85fcbe1060c0c483023c5de2a","value":5,"label":"Excellent"}]}
```

### Sonnet — Session 5 (Score: 3)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":3,"sessionScores":[{"sessionId":"f797dbc2-f11e-46e1-ab41-57e7ddc448c9","traceId":"69fb54ed2956d35d6f437a86785cca4d","value":3,"label":"Good"}]}
```

### Haiku — Session 1 (Score: 5)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":5,"sessionScores":[{"sessionId":"e4d6eab8-1511-46cc-a41d-20e49135c09c","traceId":"69fb551253bdd3334f85f56504165298","value":5,"label":"Excellent"}]}
```

### Haiku — Session 2 (Score: 5)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":5,"sessionScores":[{"sessionId":"ac3d8485-38a1-4232-b89f-a61d8ffafd9a","traceId":"69fb552913640cb21f0ac4f236d03484","value":5,"label":"Excellent"}]}
```

### Haiku — Session 3 (Score: 2)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":2,"sessionScores":[{"sessionId":"fac1a458-16f1-4fea-9f14-9e12a006f130","traceId":"69fb55411dcd9cfb145141da74194a32","value":2,"label":"Fair"}]}
```

### Haiku — Session 4 (Score: 5)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":5,"sessionScores":[{"sessionId":"2e0f2d6a-b39a-49d4-a812-f47b625cff24","traceId":"69fb555956758731522ef0a13bcad27a","value":5,"label":"Excellent"}]}
```

### Haiku — Session 5 (Score: 4)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":4,"sessionScores":[{"sessionId":"14825201-de84-4334-a4a1-8d95d196bd6f","traceId":"69fb557002ba043576945a977fae1acd","value":4,"label":"Very Good"}]}
```

### Nova Pro — Session 1 (Score: 5)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":5,"sessionScores":[{"sessionId":"58a0eb70-90ce-4b7f-b843-9fd2c794bcc3","traceId":"69fb558f129c0f850d99d0a4607e0144","value":5,"label":"Excellent"}]}
```

### Nova Pro — Session 2 (Score: 5)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":5,"sessionScores":[{"sessionId":"b95ad69d-1ab7-4111-ac6d-b59eb8b76917","traceId":"69fb55a62fdc21fe798ad705011a55af","value":5,"label":"Excellent"}]}
```

### Nova Pro — Session 3 (Score: 2)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":2,"sessionScores":[{"sessionId":"73cb0057-0ea7-48e1-92ca-3e9c366c098f","traceId":"69fb55bf5eb67b385c97fb4268b516c7","value":2,"label":"Fair"}]}
```

### Nova Pro — Session 4 (Score: 5)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":5,"sessionScores":[{"sessionId":"c9b8d058-2092-4c55-be11-c15d4a670dbc","traceId":"69fb55d77b5ada6e1f554c50081ecf56","value":5,"label":"Excellent"}]}
```

### Nova Pro — Session 5 (Score: 3)
```json
{"evaluator":"multiplier_domain_accuracy","aggregateScore":3,"sessionScores":[{"sessionId":"7662a0c8-84e4-40d8-bd07-9ae8410f8be6","traceId":"69fb55ea6aa1c4c2122196102591385b","value":3,"label":"Good"}]}
```
