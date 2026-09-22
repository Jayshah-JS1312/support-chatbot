# Architecture

## Request flow

1. `web.py` or `cli.py` accepts a customer message.
2. `policy.check_input` removes card-like data and marks suspected instruction override attempts.
3. The selected planner receives conversation, working, and customer memory.
4. The model can request only tools declared in `tools.SCHEMAS`.
5. `policy.guarded_run` enforces cross-turn confirmation and escalation rules.
6. Tool results update working memory and are returned to the planner.
7. `policy.check_output` removes identifiers that lack a trusted source.
8. The response, memory, and trace are persisted.

## Boundaries

- `agent_profile.py`: identity, scope, and response behavior
- `planner.py`: adaptive ReAct execution
- `plan_execute.py`: plan-first execution
- `policy.py`: deterministic controls around model input, actions, and output
- `tools.py`: model-facing tool schemas and dispatch
- `store.py`: replaceable simulated commerce adapter
- `memory.py`: conversation, working, and customer memory
- `knowledge.py` / `embedder.py`: local retrieval pipeline
- `observe.py`: trace, latency, token, and cost events

## Runtime data

Runtime files are intentionally outside the Python package:

- `SUPPORT_CHATBOT_STATE_DIR`: sessions, customer memory, traces, and feedback
- `SUPPORT_CHATBOT_CACHE_DIR`: embedding model and ChromaDB index

Container deployments mount both below `/data`. Application assets—the UI and
knowledge Markdown—ship as package data and remain immutable.

## Test tiers

- `tests/`: deterministic and network-free; runs on every commit.
- `evaluations/behavioral.py`: live model behavior and side effects.
- `evaluations/golden.py`: live answer correctness, grounding, and judge audit.

Live evaluations are release evidence, not a substitute for deterministic
authorization and state-transition tests.
