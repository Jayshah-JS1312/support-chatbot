# Architecture

## Request flow

1. A FastAPI customer route accepts a customer message and idempotency key.
2. Authentication middleware resolves the opaque session cookie and establishes
   a request identity used by authorization and PostgreSQL RLS.
3. PostgreSQL commits a `RECEIVED` support request before any queue call.
4. QStash delivers the initial signed Upstash Workflow invocation. The customer
   receives `202 Accepted` without waiting for the model.
5. A leased worker moves the request through `QUEUED → DRAFTING`, creates a
   sanitized resolution draft, and commits `AWAITING_APPROVAL`.
6. An authenticated admin later approves or rejects. Approval publishes a new
   durable execution delivery; rejection completes without action.
7. Execution claims the request and inserts an action using a unique execution
   idempotency key before completing. Duplicate deliveries are safe no-ops.

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
- `api/routes/customer.py`: asynchronous submissions/status, browser session, reset, and feedback
- `api/routes/authentication.py`: signup, login, logout, refresh, identity, and password reset
- `api/routes/admin.py`: internal operator pages, events, and raw traces
- `api/routes/workflow_callbacks.py`: operator recovery and expiry controls
- `workflow.py`: Upstash dispatch, signed callback registration, drafting,
  execution, retry recovery, and dead-letter handling
- `api/routes/observability.py`: health, readiness, metrics, and retrieval evals

The `support-chatbot-web` command serves `support_chatbot.web:app` through
Uvicorn. The Upstash SDK verifies QStash signatures with the current and next
signing keys before accepting workflow invocations.

## Durable workflow states

```text
RECEIVED → QUEUED → DRAFTING → AWAITING_APPROVAL
                                   ├→ APPROVED → EXECUTING → COMPLETED
                                   ├→ REJECTED → COMPLETED
                                   └→ EXPIRED → COMPLETED_WITHOUT_ACTION
```

Submission idempotency is unique per user and payload-bound. Drafting and
execution use expiring database leases; an active duplicate returns without
work, while a delivery after a crashed worker's lease can resume. Drafts and
approval tasks are unique per request, and action executions have a unique
business idempotency key. Failed enqueue attempts remain `RECEIVED` or
`APPROVED` and are retried by `/workflow/recover`. Exhausted Upstash retries
are recorded in `workflow_dead_letters`. `/workflow/expire` implements the
absence policy: expire and complete without action—never execute silently.

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
