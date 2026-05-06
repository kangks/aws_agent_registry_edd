# Implementation Plan: AgentCore EDD POC (Lean)

## Overview

Minimum viable POC demonstrating AgentCore custom evaluators and observability for model evaluation. Build a simple agent, deploy it two ways (managed + BYO with OTEL), run custom evaluators against traces, compare models. That's it.

## Tasks

- [x] 1. Set up project structure and dependencies
  - [x] 1.1 Create directory structure and initialize Python project
    - Create `edd-poc/` with subdirectories: `agents/`, `evaluators/`, `scripts/`, `results/`
    - Create `requirements.txt` with: `strands-agents`, `strands-agents[otel]`, `strands-agents-tools`, `strands-agents-evals`, `boto3`, `pyyaml`
    - Create `.env.example` with required environment variables (AWS_REGION, AWS_ACCOUNT_ID, model IDs)

- [x] 2. Implement core agent with factory pattern and tools
  - [x] 2.1 Implement agent factory and tool functions in `agents/agent.py`
    - Define `SUPPORTED_MODELS` dict mapping 3 model keys to Bedrock model IDs: sonnet, haiku, nova_pro
    - Implement `create_agent(model_key: str) -> Agent` factory function
    - Implement system prompt enforcing tool usage for domain questions
    - Implement `@tool employee_lookup(employee_id: str, country: str)` returning mock employment data
    - Implement `@tool compliance_checker(country: str)` returning mock compliance rules
    - Implement `@tool payroll_calculator(annual_salary: float, country: str, currency: str)` with correct math
    - Implement `@tool leave_manager(employee_id: str, action: str)` returning mock leave data

- [x] 3. Implement AgentCore Runtime wrapper (Demo 1)
  - [x] 3.1 Create `agents/runtime_agent.py` for AgentCore Runtime deployment
    - Import agent factory from `agent.py`
    - Expose entry point expected by `agentcore deploy`
    - Handle invocation interface (accept prompt, return response)

- [x] 4. Implement local OTEL runner (Demo 2)
  - [x] 4.1 Create `agents/local_runner.py` with OTEL instrumentation
    - Accept `--model` CLI argument for model selection
    - Set `OTEL_RESOURCE_ATTRIBUTES` to `service.name=multiplier-local-{model_key}`
    - Accept `--prompt` CLI argument or read from stdin
    - Print response and exit (no interactive loop — keep it simple)

- [x] 5. Implement custom evaluators
  - [x] 5.1 Create LLM-as-a-judge config in `evaluators/domain_accuracy_config.json`
    - Configure judge model as `global.anthropic.claude-sonnet-4-5-20250929-v1:0`
    - Define instructions evaluating factual accuracy, completeness, and compliance safety
    - Define 5-level numerical rating scale: 1.0 (Excellent) to 0.0 (Unacceptable)
    - Set inferenceConfig with maxTokens=500, temperature=1.0
  - [x] 5.2 Implement Lambda evaluator in `evaluators/lambda_evaluator.py`
    - Implement `lambda_handler(event, context)` with 4 deterministic checks: tool_usage, empty_params, response_content, latency
    - Return `{"score": passing_checks / 4, "details": {...}}`
    - Handle malformed trace input gracefully (return score 0.0)

- [x] 6. Create model comparison script
  - [x] 6.1 Create `scripts/run_and_compare.py` — single script that does everything
    - Accept `--models` CLI argument (comma-separated model keys)
    - For each model: invoke agent with 5 hardcoded prompts, save traces
    - After all models run: call `agentcore eval run` with all 8 evaluators (6 built-in + 2 custom)
    - Parse evaluation results and print comparison table to stdout
    - Save `results/comparison.md` with model × evaluator scores + cost-per-interaction

- [x] 7. Create README with run instructions
  - [x] 7.1 Write `README.md` — short, focused on "how to run this"
    - Prerequisites: AWS account, model access, `agentcore` CLI, Python 3.11
    - Setup: pip install, env vars, deploy Lambda evaluator
    - Demo 1: `agentcore deploy` + `agentcore invoke` + `agentcore eval run`
    - Demo 2: `python agents/local_runner.py --model sonnet --prompt "..."` + eval
    - Compare: `python scripts/run_and_compare.py --models sonnet,haiku,nova_pro`
    - What to show the customer: which numbers matter and why

- [x] 8. Test end-to-end on AWS
  - [x] 8.1 Run the local agent against Bedrock and verify traces
    - Use default AWS_PROFILE and AWS_REGION=us-east-1
    - Install dependencies in a Python 3.11 venv
    - Run `python agents/local_runner.py --model sonnet --prompt "What is the employment status of employee EMP-12345 in Singapore?"` and confirm a valid response
    - Run `python agents/local_runner.py --model haiku --prompt "What are the compliance requirements for Thailand?"` and confirm a valid response
    - Run `python agents/local_runner.py --model nova_pro --prompt "Calculate monthly payroll for 72000 THB annual salary in Thailand"` and confirm a valid response
  - [x] 8.2 Run the full model comparison and verify output
    - Run `python scripts/run_and_compare.py --models sonnet,haiku,nova_pro`
    - Verify `results/comparison.md` is generated with scores for all 3 models
    - Confirm no errors or crashes during the run
  - [x] 8.3 Run trace capture comparison and verify trace output
    - Run `python scripts/run_and_compare.py --models sonnet,haiku,nova_pro`
    - Verify `results/traces/` directory contains JSON trace files for each model/prompt combination
    - Verify `results/traces/tool_use_graph.md` is generated with tool call sequences
    - Verify `results/comparison.md` includes tool use summary section
  - [x] 8.4 Run multi-turn evaluation and verify output
    - Run `python scripts/run_multi_turn.py --models sonnet,haiku,nova_pro`
    - Verify `results/multi_turn_comparison.md` is generated with scores for all 3 models
    - Verify conversations complete within max_turns
    - Confirm no errors or crashes during the run

- [x] 9. Add trace capture and tool use graph to comparison script
  - [x] 9.1 Update `scripts/run_and_compare.py` to set up InMemorySpanExporter and capture traces
    - Add imports: `InMemorySpanExporter`, `BatchSpanProcessor`, `StrandsEvalsTelemetry`, `StrandsInMemorySessionMapper`
    - Initialize telemetry with in-memory exporter before agent invocations
    - After each invocation: capture spans, map to session using `StrandsInMemorySessionMapper`, clear exporter
    - Save trace JSON files to `results/traces/{model_key}_prompt_{idx}.json` with complete span tree (model calls, tool invocations, parameters, responses, timing)
    - _Requirements: 8.1, 8.2, 8.3, 8.4_
  - [x] 9.2 Generate tool use graph, detailed agentic traces, and update comparison output
    - Parse captured traces to extract tool call sequences per model per prompt
    - Generate `results/traces/tool_use_graph.md` with table showing tool sequences, parameters, and relationships per model per prompt
    - Generate "Detailed Agentic Traces" section in `results/comparison.md` showing per-prompt per-model reasoning loops: model reasoning text, tool call decisions (with parallel detection), tool results, and final synthesis
    - Append tool use summary section to `results/comparison.md` showing which tools were called, in what order, with what parameters, and how they connect per prompt
    - _Requirements: 8.5, 8.6, 8.7, 8.8, 8.9_
  - [x] 9.3 Test trace capture end-to-end
    - Run `python scripts/run_and_compare.py --models sonnet,haiku,nova_pro`
    - Verify `results/traces/` directory contains JSON files for each model/prompt combination (15 files total)
    - Verify `results/traces/tool_use_graph.md` is generated with tool sequences for all models and prompts
    - Verify `results/comparison.md` includes tool use summary section
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

- [x] 10. Implement multi-turn evaluation with ActorSimulator
  - [x] 10.1 Create `scripts/run_multi_turn.py` with actor profiles and test cases
    - Define 3 `ActorProfile` instances: `impatient_hr_manager`, `new_employee`, `compliance_auditor` with traits, context, and actor_goal
    - Define 3 `Case` instances with input queries and task_descriptions that exercise multi-tool conversations
    - Accept `--models` CLI argument (comma-separated model keys, default: `sonnet,haiku,nova_pro`)
    - Accept `--max-turns` CLI argument (default: 5)
    - _Requirements: 9.1, 9.6_
  - [x] 10.2 Implement multi-turn conversation loop with trace capture
    - Set up `InMemorySpanExporter` and `StrandsEvalsTelemetry` for trace capture
    - For each model × case combination: create agent via `create_agent()`, create `ActorSimulator.from_case_for_user_simulator()`, run conversation loop up to max_turns
    - Capture spans across all turns using `InMemorySpanExporter`, map to session using `StrandsInMemorySessionMapper`
    - Record conversation history with tool usage per turn
    - _Requirements: 9.1, 9.2, 9.3_
  - [x] 10.3 Implement evaluation and output generation
    - Evaluate each conversation using `HelpfulnessEvaluator`
    - Calculate goal success rate per model (did the agent provide all requested information)
    - Generate `results/multi_turn_comparison.md` with summary table (model, avg helpfulness, goal success rate, avg turns, avg latency) and per-case conversation details
    - Include per-model scores, tool usage per turn, and conversation transcripts
    - _Requirements: 9.4, 9.5, 9.7_
  - [x] 10.4 Test multi-turn evaluation end-to-end
    - Run `python scripts/run_multi_turn.py --models sonnet,haiku,nova_pro`
    - Verify `results/multi_turn_comparison.md` is generated with scores for all 3 models
    - Verify conversations complete within max_turns (3–5 turns)
    - Confirm no errors or crashes during the run
    - _Requirements: 9.2, 9.5, 9.7_

- [x] 11. Add local SDK-based evaluation scores to comparison
  - [x] 11.1 Replace agentcore CLI evaluation with strands-agents-evals SDK evaluators
    - Update `scripts/run_and_compare.py` to use `HelpfulnessEvaluator` from strands-agents-evals directly on captured sessions
    - For each model × prompt: pass the captured session (from StrandsInMemorySessionMapper) to HelpfulnessEvaluator
    - Store the helpfulness score per prompt per model
    - Fall back to agentcore CLI only if SDK evaluators fail
    - Update the comparison table to show actual scores instead of N/A
    - _Requirements: 10.1, 10.2, 10.3, 10.4_
  - [x] 11.2 Test that comparison.md shows actual scores
    - Run `python scripts/run_and_compare.py --models sonnet,haiku,nova_pro`
    - Verify `results/comparison.md` shows numeric scores (not N/A) for at least helpfulness
    - _Requirements: 10.4_

- [x]* 12. Generate visual Mermaid trace diagrams (optional)
  - [x]* 12.1 Add Mermaid diagram generation to comparison script
    - Parse trace data to build sequence diagrams per prompt per model
    - Generate `results/traces/trace_diagrams.md` with Mermaid `sequenceDiagram` blocks
    - Show: User → Agent reasoning → Tool calls (with params) → Tool results → Agent synthesis
    - Use Mermaid `par` blocks for parallel tool calls
    - Add timing annotations on arrows
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_
  - [x]* 12.2 Test diagram rendering
    - Run comparison script and verify `results/traces/trace_diagrams.md` is generated
    - Verify Mermaid syntax is valid (renders in VS Code markdown preview)
    - Verify parallel tool calls render as parallel branches
    - _Requirements: 11.1, 11.3, 11.4_

- [x] 13. Deploy multi-model AgentCore runtimes for CloudWatch comparison
  - [x] 13.1 Create agentcore project with 3 model-specific runtimes
    - Configure `agentcore/agentcore.json` with 3 runtimes: `multiplier_hr_sonnet`, `multiplier_hr_haiku`, `multiplier_hr_nova_pro`
    - Each runtime uses same code (`agents/main.py`) with different `AGENT_MODEL_KEY` env var
    - Deploy with `agentcore deploy` to create all runtimes in us-east-1
  - [x] 13.2 Add online evaluation configs for automatic scoring
    - Define `multiplier_domain_accuracy` evaluator (LLM-as-a-Judge with Claude Sonnet 4.5)
    - Create 3 `onlineEvalConfigs` linking each runtime to the evaluator at 100% sampling
    - Verify evaluator scores traces with 1-5 numerical scale
  - [x] 13.3 Run end-to-end comparison across all 3 models
    - Invoke 5 HR prompts against each runtime (15 total invocations)
    - Run on-demand evaluation per session: `agentcore run eval --runtime <name> --evaluator multiplier_domain_accuracy --session-id <id>`
    - Generate `results/agentcore_comparison.md` with summary table, per-session scores, and observations
    - Results: Sonnet avg 4.0, Haiku avg 4.2, Nova Pro avg 4.0
  - [x] 13.4 Verify evaluation scores appear in CloudWatch GenAI Observability
    - Confirmed traces emit with `strands.telemetry.tracer` scope
    - Confirmed on-demand eval returns scores per session with explanations
    - All 15 sessions evaluated successfully with the `multiplier_domain_accuracy` evaluator

- [x] 14. Implement BYO agent with CloudWatch observability
  - [x] 14.1 Create byo_runner.py for non-runtime agent execution
  - [x] 14.2 Run BYO agent with ADOT instrumentation for all 3 models
  - [x] 14.3 Verify BYO traces appear in CloudWatch GenAI Observability
  - [ ] 14.4 Run evaluator against BYO traces

## Notes

- 3 models (not 5) — enough to show the comparison pattern without burning time/tokens on Opus and Nova Lite
- 5 hardcoded prompts in the comparison script — no separate dataset file needed
- No registry, no governance workflows, no MCP endpoint
- No CloudWatch dashboard, no alarms, no online evaluation — those are Week 2 polish
- No property-based tests — this is a demo, not a production system
- The entire POC should be readable in 30 minutes and runnable in under an hour (given AWS access)
- `strands-agents-evals` is required for trace capture (InMemorySpanExporter, StrandsInMemorySessionMapper) and multi-turn evaluation (ActorSimulator, HelpfulnessEvaluator)
- Multi-turn evaluation uses ActorSimulator with 3 HR-specific personas to test realistic conversation flows
- Task 12 (Mermaid diagrams) is optional — marked with `*` — useful for presentations but not required for the core demo
