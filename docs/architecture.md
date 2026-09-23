# Architecture

## Request flow

1. A FastAPI customer route or `cli.py` accepts a customer message.
2. Authentication middleware resolves the opaque session cookie and establishes
   a request identity used by authorization and PostgreSQL RLS.
3. `policy.check_input` removes card-like data and marks suspected instruction override attempts.
4. The selected planner receives conversation, working, and customer memory.
5. The model can request only tools declared in `tools.SCHEMAS`.
6. `policy.guarded_run` enforces cross-turn confirmation and escalation rules.
7. Tool results update working memory and are returned to the planner.
8. `policy.check_output` removes identifiers that lack a trusted source.
9. The sanitized response—not the raw model response—memory, and trace are
   persisted.

## Boundaries

- `agent_profile.py`: identity, scope, and response behavior
- `planner.py`: adaptive ReAct execution
- `plan_execute.py`: plan-first execution
- `policy.py`: deterministic controls around model input, actions, and output
- `tools.py`: model-facing tool schemas and dispatch
- `store.py`: model-tool facade over the durable commerce repository
- `persistence.py`: pooled PostgreSQL access and atomic state transitions
- `memory.py`: conversation and working-memory models plus durable customer-memory facade
- `db_migrations.py`: ordered migration runner for Docker/non-Supabase deployments
- `knowledge.py` / `embedder.py`: local retrieval pipeline
- `observe.py`: trace, latency, token, and cost events
- `api/app.py`: FastAPI factory, request limits, and structured errors
- `api/runtime.py`: process-local cache/locks over durable PostgreSQL conversations
- `api/routes/customer.py`: chat, browser session, reset, and feedback routes
- `api/routes/authentication.py`: signup, login, logout, refresh, identity, and password reset
- `api/routes/admin.py`: internal operator pages, events, and raw traces
- `api/routes/workflow_callbacks.py`: reserved asynchronous callback boundary
- `api/routes/observability.py`: health, readiness, metrics, and retrieval evals

The `support-chatbot-web` command serves `support_chatbot.web:app` through
Uvicorn. Workflow callbacks remain deliberately unimplemented until signed
payload, replay-protection, and idempotency contracts are introduced.

## Authentication and authorization

Passwords are bcrypt-hashed. The browser receives an opaque `HttpOnly`,
`SameSite=Lax` session token; only its SHA-256 digest is stored. Refresh rotates
the session, logout revokes it, and a successful password reset revokes every
active session for that user. Signup has no role input and always creates a
`customer`.

The application switches each business transaction to the restricted
`app_backend` PostgreSQL role and sets the verified user/role as transaction-
local context. RLS then limits orders, conversations, messages, memory, support
requests, actions, and audit events to their owner. Admin-only policies guard
approval and evaluation records. Application checks still return clear 401/403
responses; RLS is the defense-in-depth boundary.

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

## Durable data

PostgreSQL is the source of truth for profiles, orders/events, conversations,
messages, verified memory facts/summaries, support requests, drafts, approval
tasks, action executions, audit events, and evaluation records. The schema lives
under `supabase/migrations/`; the Docker migration job applies every new file
exactly once.

Order mutations use `orders.version` for optimistic concurrency control. The
status and expected version are checked in the same SQL update; a stale worker
cannot overwrite a newer action. The order event and action execution are
committed in the same transaction as the status change.

Only local telemetry/evaluation report files and the ChromaDB embedding cache
remain filesystem-backed. They are not customer/business state. Application
assets—the UI and knowledge Markdown—ship as immutable package data.

## Test tiers

- `tests/`: deterministic and network-free; runs on every commit.
- `evaluations/behavioral.py`: live model behavior and side effects.
- `evaluations/golden.py`: live answer correctness, grounding, and judge audit.

Live evaluations are release evidence, not a substitute for deterministic
authorization and state-transition tests.
