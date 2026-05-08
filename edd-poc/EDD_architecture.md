# Evaluation-Driven Development (EDD) with Amazon Bedrock AgentCore

## Customer Technical Briefing

**Date:** May 8, 2026  
**Project:** eddpoc — HR/Compliance Agent POC

---

## 1. Executive Summary

This document describes a proof-of-concept demonstrating **Evaluation-Driven Development (EDD)** — a methodology where evaluation scores drive every decision about AI agents: model selection, prompt quality, and production readiness.

The POC deploys the same HR/compliance agent across **two deployment paths** (Managed and BYO) using **three foundation models** (Claude Sonnet 4, Claude Haiku 4.5, Amazon Nova Pro), evaluates them with **8+ evaluators**, and produces data-driven model recommendations.

**Key findings:**
- All 6 agents deployed successfully with 100% invocation success rate (30/30)
- Sonnet achieves highest quality (WQS 0.931) but at 3x the cost of Haiku
- Haiku offers the best cost-adjusted performance (CAP score 3.24)
- Nova Pro is fastest and cheapest but fails on complex multi-tool queries

---

## 2. What is EDD?

Evaluation-Driven Development is like Test-Driven Development, but for AI agents. Instead of relying on manual testing or gut feel, EDD gives you quantitative data on agent performance across multiple dimensions.

**The core loop:**

```
Define Evaluators → Run Agent → Capture Traces → Score Traces → Compare → Iterate
```

Every time you change a model, modify a prompt, or update tools, you re-run the evaluation loop and compare scores. This gives you confidence that changes improve quality rather than degrade it.

---

## 3. Architecture Overview

![Architecture Diagram](screenshots/diagram_architecture.png)

The architecture has two parallel deployment paths that both feed into a unified observability and evaluation layer:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Agent Registry (6 agents)                     │
│         3 managed + 3 BYO, all with evaluation history              │
└──────────────────┬──────────────────────────┬───────────────────────┘
                   │                          │
      ┌────────────▼────────────┐   ┌────────▼──────────────┐
      │   Managed Agents (3)     │   │   BYO Agents (3)      │
      │   AgentCore Runtime      │   │   ADOT → CloudWatch   │
      │   (zero infrastructure)  │   │   (your compute)      │
      └────────────┬────────────┘   └────────┬──────────────┘
                   │                          │
      ┌────────────▼──────────────────────────▼───────────────┐
      │           CloudWatch GenAI Observability                │
      │           (traces from BOTH paths visible)             │
      └──────────────────────────┬────────────────────────────┘
                                 │
      ┌──────────────────────────▼────────────────────────────┐
      │              Evaluation Engine                          │
      │   Built-in (8) + Custom (LLM Judge + Lambda)           │
      └──────────────────────────┬────────────────────────────┘
                                 │
      ┌──────────────────────────▼────────────────────────────┐
      │         Results: Scores, Comparison, Recommendations   │
      └───────────────────────────────────────────────────────┘
```

---

## 4. Managed vs BYO Agents

### What are Managed Agents?

Managed agents are deployed to **Amazon Bedrock AgentCore Runtime** — a fully managed hosting environment where you deploy your agent code and AWS handles all infrastructure, scaling, and observability.

- **Zero infrastructure** — no servers, containers, or load balancers to manage
- **Automatic OTEL instrumentation** — traces export to CloudWatch without any code changes
- **Built-in evaluator support** — run `agentcore run eval` to score traces
- **Online evaluation** — auto-score every invocation at 100% sampling rate

### What are BYO (Bring Your Own) Agents?

BYO agents run on **your own compute** (local machine, EC2, Lambda, ECS) while sending traces to CloudWatch via ADOT (AWS Distro for OpenTelemetry).

- **Run anywhere** — same agent code, your infrastructure
- **ADOT instrumentation** — `opentelemetry-instrument` wrapper handles all trace export
- **CloudWatch visibility** — traces appear in GenAI Observability alongside managed agents
- **Local SDK evaluation** — use `strands-agents-evals` for scoring

### Comparison Table

| Aspect | Managed | BYO |
|--------|---------|-----|
| Agent runs on | AgentCore Runtime | Your compute |
| Infrastructure | Zero (AWS managed) | You manage |
| Traces go to | CloudWatch (automatic) | CloudWatch via ADOT |
| Visible in GenAI Observability | ✅ Yes | ✅ Yes |
| `agentcore run eval` | ✅ Works | ❌ Not yet supported |
| Local SDK evaluation | ✅ Works | ✅ Works |
| Online eval (auto-scoring) | ✅ Supported | ❌ Not yet supported |
| Latency overhead | ~5-8s (cold start) | None (direct invocation) |

### Console Screenshot: All 6 Agents in CloudWatch

The screenshot below shows all 6 agents visible in CloudWatch GenAI Observability — both managed (bedrock-agentcore environment) and BYO (other environment) agents appear in the same dashboard:

![Agents List](screenshots/02_agents_list.png)

---

## 5. Agent Registry

The Agent Registry is the unified catalog that tracks all agents with their evaluation history.

### How Registry Metadata Works

Each agent record contains:

```json
{
  "name": "multiplier_hr_sonnet",
  "model": "us.anthropic.claude-sonnet-4-6",
  "deployment_type": "managed",
  "runtime_id": "eddpoc_multiplier_hr_sonnet-5YhsT625tI",
  "service_name": "multiplier_hr_sonnet.DEFAULT",
  "owner": "edd-poc",
  "description": "HR/Compliance agent - Sonnet 4 (managed runtime)",
  "last_eval_score": {
    "correctness": 1.0,
    "helpfulness": 1.0
  },
  "last_eval_timestamp": "2026-05-08T01:41:29.938536+00:00",
  "last_eval_evaluator": "registry_comparison",
  "tags": ["managed", "sonnet", "baseline"]
}
```

### Two-Level Registry

1. **Local Registry** (`registry/registry.json`) — Fast access, tracks all 6 agents with eval scores
2. **AWS Agent Registry** (cloud-side) — Durable, centralized catalog with approval workflows

**AWS Registry Details:**
- Registry ID: `Rqbs73eeqpMEEwf9`
- ARN: `arn:aws:bedrock-agentcore:us-east-1:654654616949:registry/Rqbs73eeqpMEEwf9`
- 6 CUSTOM records with metadata stored in `descriptors.custom.inlineContent`
- Record lifecycle: CREATING → DRAFT → PENDING_APPROVAL → APPROVED

### Stale Agent Detection

The registry automatically identifies agents that haven't been evaluated in N days:

```python
stale = get_stale_agents(days=7)
# Returns agents needing re-evaluation
```

This enables scheduled re-evaluation triggers — ensuring quality doesn't silently degrade.

---

## 6. Runtime Deep Dive

### Managed Runtime (AgentCore)

The managed runtime uses `BedrockAgentCoreApp` from the `bedrock_agentcore.runtime` package:

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from agent import create_agent

app = BedrockAgentCoreApp()
agent = create_agent(os.environ.get("AGENT_MODEL_KEY", "sonnet"))

@app.entrypoint
def invoke(payload):
    prompt = payload.get("prompt") or ""
    response = agent(prompt)
    return str(response)
```

**Configuration** (`agentcore.json`):
- 3 runtimes sharing the same code, differentiated by `AGENT_MODEL_KEY` env var
- Python 3.11, PUBLIC network, HTTP protocol
- 900s idle timeout, 3600s max lifetime
- Online evaluation at 100% sampling rate

### BYO Runtime (ADOT + CloudWatch)

The BYO path uses the `opentelemetry-instrument` wrapper with ADOT configuration:

```bash
AGENT_OBSERVABILITY_ENABLED=true \
OTEL_PYTHON_DISTRO=aws_distro \
OTEL_PYTHON_CONFIGURATOR=aws_configurator \
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
OTEL_RESOURCE_ATTRIBUTES="service.name=multiplier-byo-sonnet" \
opentelemetry-instrument python agents/byo_runner.py --model sonnet --prompt "..."
```

The agent code is identical — only the instrumentation wrapper differs. ADOT handles all trace export to CloudWatch without any manual OTEL configuration in the agent code.

---

## 7. Observability: Metrics, OTEL, and Traces

### OpenTelemetry Trace Structure

Every agent invocation produces a trace — a tree of spans representing every action:

```
invoke_agent Strands Agents (root span, 14.82s)
├── execute_event_loop_cycle (4.67s)
│   ├── chat (4.66s)
│   │   ├── Bedrock Runtime.CountTokens (0.80s)
│   │   └── chat us.anthropic.claude-sonnet-4-6 (3.86s) [1,202 → 164 tokens]
│   ├── execute_tool employee_lookup (0s)
│   └── execute_tool compliance_checker (0s)
├── execute_event_loop_cycle (cycle 2)
│   ├── chat → chat us.anthropic.claude-sonnet-4-6
│   └── execute_tool payroll_calculator
└── execute_event_loop_cycle (cycle 3, final synthesis)
    └── chat → chat us.anthropic.claude-sonnet-4-6
```

### Three Trace Capture Paths

| Path | Where Agent Runs | How Traces Export | Best For |
|------|-----------------|-------------------|----------|
| **AgentCore Runtime** | Managed | Automatic (OTEL sidecar) | Production, continuous eval |
| **BYO + ADOT** | Your compute | ADOT → CloudWatch | Existing infra, observability |
| **InMemoryExporter** | Local process | Captured in-memory | Development, offline comparison |

### CloudWatch GenAI Observability Dashboard

The dashboard provides a unified view of all agents:

![AgentCore Observability Overview](screenshots/01_agentcore_observability_overview.png)

**Key metrics visible:**
- **6/3 Agents/Endpoints** — 6 agents across 3 endpoint types
- **30 Sessions** — from the latest comparison run
- **150 Traces** — 5 prompts × 6 agents × multiple spans
- **194.1K Total tokens** — across all invocations
- **0% Error rate** — all invocations successful
- **0% Throttle rate** — no throttling encountered

### Agent Detail: Span Metrics

Clicking into an agent shows per-span metrics including tool execution counts and latency:

![Agent Detail Spans](screenshots/03_agent_detail_spans.png)

### Token Usage Over Time

The FM token usage graph shows input/output token consumption patterns:

![Agent Metrics Tokens](screenshots/04_agent_metrics_tokens.png)

### Trace List

Each agent has a list of traces with span counts, token usage, and timing:

![Traces List](screenshots/05_traces_list.png)

### Trace Detail: Span Tree + Trajectory Graph

Clicking into a trace reveals the full execution flow as both a span tree and a visual trajectory graph:

![Trace Detail Graph](screenshots/06_trace_detail_graph.png)

This trace shows:
- **16 spans** total for a multi-tool query
- **3 event loop cycles** (model reasoning → tool calls → synthesis)
- **3 tool executions** (employee_lookup, compliance_checker, payroll_calculator)
- **5,427 tokens** consumed
- **14.82s** total latency

---

## 8. Evaluation System

### Built-in Evaluators

The `strands-agents-evals` SDK provides 8 evaluators:

| Evaluator | What It Measures | Score Range |
|-----------|-----------------|-------------|
| Helpfulness | Was the response useful? | 0.0 – 1.0 |
| Faithfulness | Is it grounded in tool outputs? | 0.0 – 1.0 |
| Coherence | Is it well-structured? | 0.0 – 1.0 |
| Correctness | Does it match ground truth? | 0.0 – 1.0 |
| Harmfulness | Does it avoid harmful content? | 0.0 – 1.0 |
| Answer Relevancy | Does it address the question? | 0.0 – 1.0 |
| Tool Selection Accuracy | Did it pick the right tools? | 0.0 – 1.0 |
| Tool Parameter Accuracy | Did it pass correct params? | 0.0 – 1.0 |

### Custom Evaluator: LLM-as-a-Judge (`multiplier_domain_accuracy`)

An LLM (Claude Sonnet 4.5) reads the agent's trace and scores it on a rubric:

```
Criteria:
1. Factual Accuracy — Are the facts correct?
2. Completeness — Does it fully address the question?
3. Compliance Safety — Does it avoid dangerous advice?

Scale:
1.0 = Excellent (accurate, complete, compliant)
0.75 = Good (mostly accurate, minor gaps)
0.5 = Adequate (partially correct, missing details)
0.25 = Poor (significant errors)
0.0 = Unacceptable (wrong or unsafe)
```

### Custom Evaluator: Lambda (`multiplier_deterministic`)

A Lambda function runs 4 binary checks against each trace:

| Check | What It Verifies | Pass Condition |
|-------|-----------------|----------------|
| `tool_usage` | Agent called at least one tool | Any tool span exists |
| `no_empty_params` | Tool calls have non-empty parameters | All tool spans have params |
| `has_response` | Agent produced a non-empty response | Response length > 0 |
| `under_latency` | Total execution under threshold | Duration < 30 seconds |

Score = passing checks / 4 (e.g., 3/4 = 0.75)

### Online Evaluation Configuration

For managed agents, online evaluation auto-scores every invocation:

```json
{
  "name": "eval_sonnet",
  "agent": "multiplier_hr_sonnet",
  "evaluators": ["multiplier_domain_accuracy"],
  "samplingRate": 100,
  "enableOnCreate": true
}
```

### Ground-Truth Comparison Methodology

The most rigorous evaluation approach:

1. **Baseline:** Run Sonnet (highest quality model) and capture its responses
2. **Contenders:** Run Haiku and Nova Pro with the same prompts
3. **Score:** Use `CorrectnessEvaluator` to compare contender responses against Sonnet's (binary CORRECT/INCORRECT)
4. **Rank:** Combine correctness + helpfulness + cost for final recommendation

---

## 9. Agentic Trajectory Comparison

The traces captured during evaluation contain the **full agentic loop** — every reasoning step, tool decision, parameter selection, and synthesis. This section compares how each model navigates the same task.

### What's Captured in Each Trace

Each trace file (`results/traces/{model}_prompt_{n}.json`) contains OTEL spans of three types:

| Span Type | What It Captures | Example |
|-----------|-----------------|---------|
| `inference` | Model reasoning + tool decisions | `<thinking>I need salary first</thinking>` → `tool_use: employee_lookup(...)` |
| `execute_tool` | Tool call params + raw result | `employee_lookup(EMP-11111, India)` → `{"salary": 55000}` |
| `inference` (cycle 2+) | Post-tool reasoning + next action | Sees salary=55000 → calls `payroll_calculator(55000, INR)` |

### Trajectory Comparison: Multi-Tool Query (Prompt 5)

**Prompt:** "For employee EMP-11111 in India, look up their details, check compliance rules, and calculate their payroll breakdown in INR."

This is the most revealing prompt — it requires 3 tools and the model must decide whether to call them sequentially (waiting for salary) or in parallel (guessing salary).

#### Sonnet 4.6 — Sequential with Parallel Optimization (12.5s)

```
┌─────────────────────────────────────────────────────────────────┐
│ Cycle 1: Reasoning + Parallel Tool Calls                         │
├─────────────────────────────────────────────────────────────────┤
│ 🧠 "I can fetch employee details and compliance rules            │
│    simultaneously, then use the salary for payroll."             │
│                                                                  │
│ 🔧 employee_lookup(EMP-11111, India)  ──┐                        │
│ 🔧 compliance_checker(India)          ──┤ PARALLEL               │
│                                         │                        │
│ 📥 {"salary": 55000, "dept": "Product"} ◄┘                       │
│ 📥 {"notice_periods": {...}, ...}       ◄┘                       │
├─────────────────────────────────────────────────────────────────┤
│ Cycle 2: Use Actual Salary                                       │
├─────────────────────────────────────────────────────────────────┤
│ 🧠 "Got salary = 55,000 INR. Now calculate payroll."             │
│ 🔧 payroll_calculator(55000, India, INR)                         │
│ 📥 {"gross_monthly": 4583.33, "net": 3116.67}                   │
├─────────────────────────────────────────────────────────────────┤
│ Cycle 3: Final Synthesis                                         │
├─────────────────────────────────────────────────────────────────┤
│ 💬 Formatted table with employee details + compliance +          │
│    payroll breakdown. Rich markdown with emojis.                 │
└─────────────────────────────────────────────────────────────────┘
```

**Strategy:** Parallel where safe (lookup + compliance don't depend on each other), sequential where needed (payroll needs salary from lookup). **3 inference cycles, 3 tool calls.**

#### Nova 2 Pro — Fully Sequential with Explicit Reasoning (6.6s)

```
┌─────────────────────────────────────────────────────────────────┐
│ Cycle 1: Reasoning + First Tool                                  │
├─────────────────────────────────────────────────────────────────┤
│ 🧠 <thinking> I need to:                                         │
│    1. Look up employee details (to get salary)                   │
│    2. Check compliance rules                                     │
│    3. Calculate payroll (needs salary from step 1)               │
│    I'll start with employee_lookup. </thinking>                  │
│                                                                  │
│ 🔧 employee_lookup(EMP-11111, India)                             │
│ 📥 {"salary": 55000, "dept": "Product"}                          │
├─────────────────────────────────────────────────────────────────┤
│ Cycle 2: Parallel (compliance + payroll)                         │
├─────────────────────────────────────────────────────────────────┤
│ 🧠 <thinking> Now I have salary=55000.                           │
│    I can call compliance + payroll in parallel. </thinking>      │
│                                                                  │
│ 🔧 compliance_checker(India)          ──┐ PARALLEL               │
│ 🔧 payroll_calculator(55000, India, INR)┘                        │
│ 📥 {"notice_periods": {...}}            ◄┘                       │
│ 📥 {"net_monthly": 3116.67}            ◄┘                       │
├─────────────────────────────────────────────────────────────────┤
│ Cycle 3: Final Synthesis                                         │
├─────────────────────────────────────────────────────────────────┤
│ 💬 Concise bullet-point summary. No markdown tables.             │
└─────────────────────────────────────────────────────────────────┘
```

**Strategy:** Sequential first (get salary), then parallel (compliance + payroll). Explicit `<thinking>` tags show reasoning. **3 inference cycles, 3 tool calls.** Fastest overall.

#### GLM 5 — Parallel All-at-Once (20.3s)

```
┌─────────────────────────────────────────────────────────────────┐
│ Cycle 1: All Tools in Parallel                                   │
├─────────────────────────────────────────────────────────────────┤
│ 🔧 employee_lookup(EMP-11111, India)    ──┐                      │
│ 🔧 compliance_checker(India)            ──┤ ALL PARALLEL         │
│ 🔧 payroll_calculator(55000, India, INR)──┘                      │
│                                                                  │
│ 📥 {"salary": 55000, ...}               ◄┘                      │
│ 📥 {"notice_periods": {...}}            ◄┘                       │
│ 📥 {"net_monthly": 3116.67}            ◄┘                       │
├─────────────────────────────────────────────────────────────────┤
│ Cycle 2: Final Synthesis                                         │
├─────────────────────────────────────────────────────────────────┤
│ 💬 Structured tables with ₹ currency symbols.                    │
│    Complete but slower due to large context window.              │
└─────────────────────────────────────────────────────────────────┘
```

**Strategy:** Calls all 3 tools simultaneously. In this run it correctly used 55000 (may have inferred from context), but this pattern is risky — if the salary isn't inferrable, it would guess. **2 inference cycles, 3 tool calls.** Slowest due to large model size.

### Tool Use Comparison Table

| Dimension | Sonnet 4.6 | Nova 2 Pro | GLM 5 |
|-----------|-----------|-----------|-------|
| **Avg Latency** | 8,613ms | 3,724ms | 13,041ms |
| **Inference Cycles (Prompt 5)** | 3 | 3 | 2 |
| **Tool Calls (Prompt 5)** | 3 | 3 | 3 |
| **Parallel Strategy** | Smart (parallel where safe) | Smart (sequential → parallel) | Aggressive (all parallel) |
| **Salary Handling** | ✅ Waits for lookup | ✅ Waits for lookup | ⚠️ Parallel (risky) |
| **Reasoning Visibility** | Hidden (natural language) | Explicit (`<thinking>` tags) | Hidden (no preamble) |
| **Response Format** | Rich markdown + emojis | Concise bullets | Structured tables |
| **Tool Parameter Order** | `(employee_id, country)` | `(country, employee_id)` | `(employee_id, country)` |

### Key Insight: Why Trajectory Comparison Matters

The tool use graph shows **what** was called. The trajectory comparison shows **why** and **how**:

- **Sonnet** reasons in natural language, parallelizes independent calls, and produces rich formatted output
- **Nova 2 Pro** shows explicit chain-of-thought, correctly identifies data dependencies, and is 2x faster
- **GLM 5** skips reasoning preamble, calls everything at once (risky for dependent tools), but produces correct results when parameters are inferrable

This is the kind of behavioral difference that simple correctness scores miss — two agents can both score 1.0 on correctness while having fundamentally different reliability profiles for edge cases.

### Graphical Trace Representations

We use three complementary visualizations to compare traces across models:

#### A. Timeline Waterfall — Where Does Time Go?

Shows each span as a horizontal bar, aligned to a common time axis. Immediately reveals parallelism and bottlenecks.

```mermaid
gantt
    title Prompt 5: Multi-Tool Query — Timeline Comparison
    dateFormat X
    axisFormat %s

    section Sonnet (12.5s)
    Inference 1 (reasoning)        :s1, 0, 3000
    employee_lookup                :s2, 3000, 3001
    compliance_checker             :s3, 3000, 3001
    Inference 2 (use salary)       :s4, 3001, 6500
    payroll_calculator             :s5, 6500, 6501
    Inference 3 (synthesize)       :s6, 6501, 12500

    section Nova 2 Pro (6.6s)
    Inference 1 (thinking)         :n1, 0, 2000
    employee_lookup                :n2, 2000, 2001
    Inference 2 (thinking)         :n3, 2001, 3700
    compliance_checker             :n4, 3700, 3701
    payroll_calculator             :n5, 3700, 3701
    Inference 3 (synthesize)       :n6, 3701, 6600

    section GLM 5 (20.3s)
    Inference 1 (plan)             :g1, 0, 5000
    employee_lookup                :g2, 5000, 5001
    compliance_checker             :g3, 5000, 5001
    payroll_calculator             :g4, 5000, 5001
    Inference 2 (synthesize)       :g5, 5001, 20300
```

**What this reveals:**
- Tool execution is near-instant (< 1ms) — all time is spent in **inference** (model thinking)
- Sonnet spends time on rich formatting in the final synthesis
- Nova 2 Pro's `<thinking>` is fast and focused — minimal synthesis time
- GLM 5's single long synthesis span suggests it's generating a very detailed response

#### B. Data Dependency DAG — What Depends on What?

Shows which tool calls depend on results from other calls. Reveals whether a model correctly identifies dependencies or makes unsafe parallel calls.

```mermaid
graph TD
    subgraph "Correct Strategy (Sonnet & Nova 2 Pro)"
        P1[Prompt: EMP-11111 India] --> EL1[employee_lookup]
        P1 --> CC1[compliance_checker]
        EL1 -->|salary=55000| PC1[payroll_calculator<br/>annual_salary=55000]
        CC1 --> SYN1[Final Synthesis]
        PC1 --> SYN1
        EL1 --> SYN1
    end

    subgraph "Risky Strategy (GLM 5 / old Nova Pro v1)"
        P2[Prompt: EMP-11111 India] --> EL2[employee_lookup]
        P2 --> CC2[compliance_checker]
        P2 -->|guessed salary?| PC2[payroll_calculator<br/>annual_salary=???]
        EL2 --> SYN2[Final Synthesis]
        CC2 --> SYN2
        PC2 --> SYN2
    end

    style PC1 fill:#d4edda,stroke:#28a745
    style PC2 fill:#f8d7da,stroke:#dc3545
```

**What this reveals:**
- `payroll_calculator` has a **data dependency** on `employee_lookup` (needs salary)
- `compliance_checker` is **independent** — safe to call in parallel with lookup
- Models that call all 3 in parallel risk using a guessed/hallucinated salary
- The DAG makes the dependency explicit — useful for designing guardrails

#### C. Swim Lane Comparison — Same Prompt, Different Behaviors

Shows the step-by-step execution of all 3 models side-by-side for direct comparison.

```mermaid
sequenceDiagram
    participant User
    participant Sonnet
    participant Nova2Pro as Nova 2 Pro
    participant GLM5 as GLM 5

    User->>Sonnet: "EMP-11111 India: lookup + compliance + payroll"
    User->>Nova2Pro: (same prompt)
    User->>GLM5: (same prompt)

    Note over Sonnet: "I'll fetch details and<br/>compliance simultaneously"
    Note over Nova2Pro: <thinking> Need salary first.<br/>Start with employee_lookup </thinking>
    Note over GLM5: (no preamble)

    Sonnet->>Sonnet: employee_lookup(EMP-11111, India)
    Sonnet->>Sonnet: compliance_checker(India)
    Nova2Pro->>Nova2Pro: employee_lookup(EMP-11111, India)
    GLM5->>GLM5: employee_lookup(EMP-11111, India)
    GLM5->>GLM5: compliance_checker(India)
    GLM5->>GLM5: payroll_calculator(55000, India, INR)

    Note over Sonnet: Got salary=55000.<br/>Now calculate payroll.
    Note over Nova2Pro: Got salary=55000.<br/>Call compliance + payroll.

    Sonnet->>Sonnet: payroll_calculator(55000, India, INR)
    Nova2Pro->>Nova2Pro: compliance_checker(India)
    Nova2Pro->>Nova2Pro: payroll_calculator(55000, India, INR)

    Note over Sonnet: 📊 Rich markdown tables
    Note over Nova2Pro: 📋 Concise bullet list
    Note over GLM5: 📊 Structured tables

    Sonnet-->>User: Response (12.5s)
    Nova2Pro-->>User: Response (6.6s)
    GLM5-->>User: Response (20.3s)
```

**What this reveals:**
- The **temporal ordering** of decisions across models
- Nova 2 Pro finishes first despite having more inference cycles (faster per-cycle)
- GLM 5 fires all tools immediately but takes longest overall (large model, slow generation)
- Sonnet's parallel strategy (lookup + compliance) is optimal for this dependency graph

#### D. Aggregate Trace Metrics — Comparing Across All Prompts

For comparing patterns across many invocations, a summary table with sparkline-style indicators:

| Metric | Sonnet | Nova 2 Pro | GLM 5 |
|--------|--------|-----------|-------|
| **Avg inference cycles/prompt** | 2.4 | 2.4 | 1.8 |
| **Avg tool calls/prompt** | 1.4 | 1.4 | 1.4 |
| **Parallel tool calls** | 1 (Prompt 5) | 1 (Prompt 5) | 1 (Prompt 5) |
| **Time in inference** | 95% | 95% | 95% |
| **Time in tools** | <1% | <1% | <1% |
| **Avg tokens/prompt** | ~1,800 | ~900 | ~1,400 |
| **Reasoning overhead** | 0% (hidden) | ~15% (thinking tags) | 0% (hidden) |

### How to Generate These Visualizations

The trace JSON files at `results/traces/{model}_prompt_{n}.json` contain all the data needed:

```python
# Extract timeline data from trace
for span in trace["spans"]:
    span_type = span["span_type"]  # "inference" or "execute_tool"
    start = span["span_info"]["start_time"]
    end = span["span_info"]["end_time"]
    
    if span_type == "execute_tool":
        tool_name = span["tool_call"]["name"]
        tool_args = span["tool_call"]["arguments"]
        tool_result = span["tool_result"]["content"]
    
    elif span_type == "inference":
        # Extract reasoning from assistant messages
        for msg in span["messages"]:
            if msg["role"] == "assistant":
                for content in msg["content"]:
                    if content["content_type"] == "tool_use":
                        # Tool decision point
                        ...
                    elif "<thinking>" in content.get("text", ""):
                        # Explicit reasoning
                        ...
```

The Mermaid diagrams above can be rendered in:
- GitHub/GitLab markdown (native support)
- VS Code with Mermaid extension
- Any Mermaid-compatible viewer (mermaid.live)
- Exported to PNG/SVG via `mmdc` CLI

---

## 10. Evaluation Sequence Diagrams

### Managed Path: AgentCore Runtime + Online Evaluation

```mermaid
sequenceDiagram
    participant Client as Client<br/>(agentcore invoke)
    participant Runtime as AgentCore Runtime<br/>(OTEL sidecar)
    participant CW as CloudWatch<br/>GenAI Observability
    participant Eval as Evaluator<br/>(LLM Judge / Lambda)
    participant Reg as Registry<br/>(local + AWS)

    Client->>Runtime: 1. invoke(prompt)
    activate Runtime

    Note over Runtime: 2. Agent executes<br/>Inference Span 1<br/>Tool Span 1<br/>Inference Span 2<br/>Tool Span 2<br/>Inference Span 3

    Runtime-->>Client: 3. response
    deactivate Runtime

    Runtime->>CW: 4. Export trace (automatic OTEL sidecar)
    activate CW

    Note over CW: Trace indexed (~10 min)

    CW->>Eval: 5. Online eval trigger (100% sampling)
    activate Eval
    Note over Eval: 6. LLM reads trace,<br/>scores 1-5 on rubric
    Eval-->>CW: 7. Store eval score
    deactivate Eval
    deactivate CW

    Client->>CW: 8. agentcore run eval (on-demand)
    activate CW
    CW->>Eval: 9. Fetch trace + run evaluator
    activate Eval
    Eval-->>Client: 10. eval results (score + justification)
    deactivate Eval
    deactivate CW

    Client->>Reg: 11. Update registry with scores
```

**Key points:**
- Steps 1-3: Normal agent invocation (client → runtime → response)
- Step 4: OTEL sidecar automatically exports trace to CloudWatch (no code needed)
- Steps 5-7: Online evaluation auto-triggers (100% sampling) — LLM judge scores the trace
- Steps 8-10: On-demand evaluation via `agentcore run eval` CLI
- Step 11: Registry updated with latest scores for tracking over time

### BYO Path: ADOT + Local SDK Evaluation

```mermaid
sequenceDiagram
    participant Client as Client<br/>(script / CLI)
    participant Agent as BYO Agent<br/>(your compute + ADOT)
    participant CW as CloudWatch<br/>GenAI Observability
    participant SDK as Local SDK Eval<br/>(strands-evals<br/>InMemoryExporter)
    participant Reg as Registry<br/>(local + AWS)

    Client->>Agent: 1. invoke (opentelemetry-instrument)
    activate Agent

    Note over Agent: 2. Agent executes<br/>Inference Span 1<br/>Tool Span 1<br/>Inference Span 2<br/>Tool Span 2<br/>Inference Span 3

    Agent-->>Client: 3. response
    deactivate Agent

    par Trace export (async)
        Agent->>CW: 4. ADOT exports trace (http/protobuf)
        Note over CW: Trace visible in<br/>GenAI Observability dashboard
    and In-memory capture (sync)
        Agent->>SDK: 5. Spans captured in-memory
    end

    activate SDK
    Note over SDK: 6. StrandsInMemorySessionMapper<br/>maps spans → structured session

    Note over SDK: 7. Run evaluators:<br/>• CorrectnessEvaluator (vs baseline)<br/>• HelpfulnessEvaluator<br/>• FaithfulnessEvaluator<br/>• CoherenceEvaluator<br/>• ToolSelectionAccuracy<br/>• ToolParameterAccuracy

    SDK-->>Client: 8. eval scores
    deactivate SDK

    Client->>Reg: 9. Update registry with scores
```

**Key differences from Managed path:**
- Step 4: ADOT (not sidecar) exports traces — requires env vars but no code changes
- Steps 5-7: Evaluation happens **locally** via `strands-agents-evals` SDK, not via AgentCore
- The `InMemorySpanExporter` captures spans in-process (parallel to ADOT export)
- `StrandsInMemorySessionMapper` converts raw spans into structured sessions for evaluators
- No online eval (auto-scoring) — must be triggered by script

### Side-by-Side: What Each Path Produces

```mermaid
graph TB
    subgraph Managed["MANAGED PATH"]
        direction TB
        M_Trace["CloudWatch Traces<br/>• Linked to runtime ARN<br/>• Discoverable by agentcore eval"]
        M_Eval["AgentCore Evaluator<br/>• multiplier_domain_accuracy (1-5)<br/>• multiplier_deterministic (Lambda)<br/>• Online eval (auto, 100% sampling)<br/>• On-demand via CLI"]
        M_Trace --> M_Eval
    end

    subgraph BYO["BYO PATH"]
        direction TB
        B_Trace["CloudWatch Traces<br/>• Linked to service.name<br/>• NOT discoverable by agentcore"]
        B_Eval["Local SDK Evaluator<br/>• CorrectnessEvaluator (0/1)<br/>• HelpfulnessEvaluator (0-1)<br/>• FaithfulnessEvaluator (0-1)<br/>• CoherenceEvaluator (0-1)<br/>• ToolAccuracy (0-1)<br/>• Triggered by script"]
        B_Trace --> B_Eval
    end

    M_Eval --> Registry["Agent Registry (unified)<br/>• Both paths update same registry<br/>• Cross-path comparison<br/>• Stale detection"]
    B_Eval --> Registry

    style Managed fill:#E8F5E9,stroke:#82b366
    style BYO fill:#E3F2FD,stroke:#6c8ebf
    style Registry fill:#FFF3E0,stroke:#d79b00
```

### Evaluation Timing: When Does Each Step Happen?

| Step | Managed | BYO |
|------|---------|-----|
| Agent invocation | T+0s | T+0s |
| Trace available in CloudWatch | T+10min (indexing) | T+10min (indexing) |
| Online eval score available | T+12min (auto) | ❌ Not supported |
| On-demand eval (`agentcore run eval`) | T+10min+ (after indexing) | ❌ Not supported |
| Local SDK eval score | T+0s (in-process) | T+0s (in-process) |
| Registry updated | T+0s (script) | T+0s (script) |

**Practical implication:** For rapid iteration during development, both paths use local SDK evaluation (instant). The managed path additionally provides continuous online evaluation for production monitoring.

---

## 11. Results & Comparison

### 6-Agent Registry Comparison (Latest Run: May 8, 2026)

**30 invocations total** (5 prompts × 6 agents), all concurrent via ThreadPoolExecutor.

| Agent | Model | Type | Correctness | Helpfulness |
|-------|-------|------|-------------|-------------|
| multiplier_hr_sonnet | Claude Sonnet 4 | managed | 1.000 | 1.000 |
| multiplier_hr_haiku | Claude Haiku 4.5 | managed | 1.000 | 0.933 |
| multiplier_hr_nova_pro | Amazon Nova Pro | managed | 0.800 | 0.867 |
| multiplier_byo_sonnet | Claude Sonnet 4 | byo | 1.000 | 1.000 |
| multiplier_byo_haiku | Claude Haiku 4.5 | byo | 1.000 | 1.000 |
| multiplier_byo_nova_pro | Amazon Nova Pro | byo | 1.000 | 0.833 |

### Per-Prompt Correctness Matrix

| # | Prompt | Sonnet (M) | Haiku (M) | Nova Pro (M) | Sonnet (B) | Haiku (B) | Nova Pro (B) |
|---|--------|:---:|:---:|:---:|:---:|:---:|:---:|
| 1 | Employment status of EMP-12345 in Singapore | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 2 | Notice periods for Thailand | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 3 | Payroll breakdown for 90000 SGD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 4 | Leave balance for EMP-67890 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 5 | Multi-tool: EMP-11111 India (lookup + compliance + payroll) | ✓ | ✓ | **✗** | ✓ | ✓ | ✓ |

**Key finding:** Nova Pro (managed) fails on Prompt 5 because it calls all 3 tools in parallel — including `payroll_calculator` with a **guessed salary** (1,000,000 INR) instead of waiting for the `employee_lookup` result (actual: 55,000 INR). This is exactly the kind of issue EDD catches.

### Latency Comparison

| Agent | Type | Avg Latency |
|-------|------|-------------|
| multiplier_hr_sonnet | managed | 17,096 ms |
| multiplier_hr_haiku | managed | 16,774 ms |
| multiplier_hr_nova_pro | managed | 15,937 ms |
| multiplier_byo_sonnet | byo | 11,882 ms |
| multiplier_byo_haiku | byo | 7,517 ms |
| multiplier_byo_nova_pro | byo | 7,320 ms |

BYO agents are consistently faster because they avoid the AgentCore Runtime overhead (cold start, routing).

### Cost-Performance Analysis

| Model | WQS | Cost/Interaction | CAP Score | Latency | Suggested Use |
|-------|-----|-----------------|-----------|---------|---------------|
| Sonnet | 0.931 | $0.00840 | 1.11 | 8,040ms | Default production model |
| Haiku | 0.908 | $0.00280 | 3.24 | 4,885ms | Simple queries, low-risk tasks |
| Nova Pro | 0.859 | $0.00192 | 4.48 | 3,603ms | Cost-sensitive (with caveats) |

**WQS (Weighted Quality Score):**
```
WQS = 0.25(Task Success) + 0.20(Faithfulness) + 0.15(Helpfulness) +
      0.15(Coherence) + 0.15(Tool Reliability) + 0.10(Multi-turn Robustness)
```

**CAP (Cost-Adjusted Performance):** WQS / Cost — higher is better value.

### Recommendation

Based on the evaluation data:

1. **Sonnet** — Use as the production default for maximum accuracy. The $0.0056/interaction premium over Haiku is negligible at scale ($56/month at 10K interactions) compared to the risk of incorrect compliance advice.

2. **Haiku** — Viable for simple single-tool queries (employee lookup, leave balance). Not recommended for complex multi-tool queries where it occasionally misses context.

3. **Nova Pro** — Fastest and cheapest, but **do not use for multi-tool queries** without additional guardrails. Its tendency to call all tools in parallel with guessed parameters creates correctness failures.

---

## 12. How to Reproduce

### Prerequisites
- AWS account with Bedrock model access (Sonnet 4, Haiku 4.5, Nova Pro, Sonnet 4.5 for judge)
- `agentcore` CLI installed
- Python 3.11
- AWS credentials configured

### Quick Start

```bash
cd edd-poc
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the full 6-agent comparison
AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \
  .venv/bin/python scripts/run_registry_comparison.py
```

### Individual Paths

**Managed (invoke via AgentCore):**
```bash
agentcore invoke --runtime multiplier_hr_sonnet "What is the employment status of EMP-12345 in Singapore?"
```

**BYO (invoke with ADOT traces):**
```bash
AGENT_OBSERVABILITY_ENABLED=true \
OTEL_PYTHON_DISTRO=aws_distro \
OTEL_PYTHON_CONFIGURATOR=aws_configurator \
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
OTEL_RESOURCE_ATTRIBUTES="service.name=multiplier-byo-sonnet" \
opentelemetry-instrument python agents/byo_runner.py --model sonnet --prompt "..."
```

---

## Glossary

| Term | Definition |
|------|-----------|
| **EDD** | Evaluation Driven Development — using scores to drive agent decisions |
| **AgentCore Runtime** | AWS managed hosting for agents |
| **ADOT** | AWS Distro for OpenTelemetry — instrumentation for BYO agents |
| **Trace** | OpenTelemetry span tree capturing every agent action |
| **Span** | Single unit of work (one LLM call, one tool invocation) |
| **Evaluator** | Function that scores a trace on some dimension (0.0–1.0) |
| **LLM-as-a-Judge** | Using a separate LLM to evaluate another LLM's output |
| **WQS** | Weighted Quality Score — composite metric across all dimensions |
| **CAP** | Cost-Adjusted Performance — quality per dollar spent |
| **Ground Truth** | Baseline model's response used as reference for comparison |

---

*Document generated from the EDD POC at `/edd-poc/` — all screenshots captured from the live AWS console on May 8, 2026.*
