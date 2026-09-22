# Architecture

## Request flow

1. A FastAPI customer route or `cli.py` accepts a customer message.
2. `policy.check_input` removes card-like data and marks suspected instruction override attempts.
3. The selected planner receives conversation, working, and customer memory.
4. The model can request only tools declared in `tools.SCHEMAS`.
5. `policy.guarded_run` enforces cross-turn confirmation and escalation rules.
6. Tool results update working memory and are returned to the planner.
7. `policy.check_output` removes identifiers that lack a trusted source.
8. The sanitized response—not the raw model response—memory, and trace are
   persisted.

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
- `api/app.py`: FastAPI factory, request limits, and structured errors
- `api/runtime.py`: process-local sessions, persistence, and shared locks
- `api/routes/customer.py`: chat, browser session, reset, and feedback routes
- `api/routes/authentication.py`: reserved authentication boundary
- `api/routes/admin.py`: internal operator pages, events, and raw traces
- `api/routes/workflow_callbacks.py`: reserved asynchronous callback boundary
- `api/routes/observability.py`: health, readiness, metrics, and retrieval evals

The `support-chatbot-web` command serves `support_chatbot.web:app` through
Uvicorn. Authentication and workflow callback routers are intentionally empty
until their security, idempotency, and durable-state contracts are implemented.

## Observability

- `/healthz`: lightweight liveness probe
- `/readyz`: application readiness and configured model
- `/metrics`: low-cardinality Prometheus text metrics
- `/monitoring`: operator dashboard for latency, success rate, tool outcomes,
  token use, cost, runtime health, and individual traces
- `/evals`: deterministic retrieval quality dashboard (Recall@1, Recall@3,
  category accuracy, ranking quality, and latency)

The dashboard and raw traces are available only when
`SUPPORT_CHATBOT_EXPOSE_INTERNAL_UI=true`. They may include customer messages
and must be protected by authentication and restricted to support operators in
any public deployment. `/metrics` exposes aggregate values only.

The retrieval dashboard reads versioned cases from
`src/support_chatbot/evaluation/retrieval.json`. It exercises only the local
embedding and knowledge index, so it does not make paid model calls. Live agent
behavior evaluations remain opt-in release checks under `evaluations/`.

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
