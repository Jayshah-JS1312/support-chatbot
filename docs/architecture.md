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

When QStash credentials are absent, local Docker uses an in-process queue
worker. HTTP submission still returns before model work begins, and PostgreSQL
remains the source of truth. Startup recovery re-enqueues persisted work. This
dispatcher is for one-process local development; deployed instances use signed
Upstash delivery so work survives process and host loss.

Cancellation and return requests add a stricter boundary before step 3. The
customer first receives a non-mutating preview from `POST /actions/preview`.
That preview contains the exact normalized arguments, customer consequences,
policy evidence, and current order version. A canonical SHA-256 action hash
seals those fields. `POST /actions/proposals/{id}/confirm` records an explicit
customer confirmation and creates the approval task; it does not mutate the
order. An admin decision stores the same hash, and execution recomputes and
compares both seals before locking and revalidating the order. A changed order
or mismatched seal completes without action.

## Boundaries

- `agent_profile.py`: identity, scope, and response behavior
- `planner.py`: adaptive ReAct execution
- `plan_execute.py`: plan-first execution
- `policy.py`: deterministic controls around model input, actions, and output
- `tools.py`: model-facing tool schemas and dispatch
- `actions.py`: privileged-action previews and canonical cryptographic seals
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
absence policy: remind the customer, move overdue work to the supervisor queue,
then expire and complete without action—never execute silently. Approval and
execution both recheck the durable final deadline, so a late decision cannot
revive work even when the periodic sweep is delayed.

`/admin/approvals` is the human decision surface. It presents only the submitted
request plus verified order facts, retrieved evidence, draft response, exact
action, consequences, age, deadline, and audit history; it does not expose the
customer's general conversation history. Decisions require
the reviewer to label whether human review was genuinely necessary. Approval,
response edits, rejection, reassignment, reviewer identity, and timestamps are
durably audited.

The customer UI restores requests from `GET /requests`, not browser memory, and
polls non-terminal work through `GET /requests/{id}`. Ordinary answers remain
normal chat bubbles; ticket history and status stay on the dedicated customer
ticket page, with only an open-ticket count in the chat sidebar. Draft content is returned as a customer answer only after
the request reaches `COMPLETED`; approval alone never appears as a successful
account action. The latest owned conversation is restored when the browser
session cookie is missing.

Three identifiers intentionally describe different durable objects:

- `REQ-*` is an internal asynchronous processing request. It records drafting,
  approval, execution, failure, and recovery state; it is not automatically a
  customer support ticket.
- `ESC-*` is a human-support ticket created only when Ami actually calls the
  escalation tool. Customers track their owned tickets at `/tickets`, while
  operators work the same records at `/admin/tickets`.
- An approval task is a separate safety gate for a proposed privileged action.
  It appears in `/admin/approvals` and must never be synthesized from a failed
  request or a general request for human assistance.

Support ticket references are scoped to the conversation that created them and
are not copied into long-term customer memory. This prevents a later issue from
silently reusing an old escalation.

## Authentication and authorization

Passwords are bcrypt-hashed. The browser receives an opaque `HttpOnly`,
`SameSite=Lax` session token; only its SHA-256 digest is stored. Refresh rotates
the session, logout revokes it, and a successful password reset revokes every
active session for that user. Signup has no role input and always creates a
`customer`.

Customer and administrator logins use distinct cookie names and strict route
realms. Customer routes never fall back to an administrator cookie, and admin
routes never authorize a customer cookie. This permits a customer and an admin
to be open in separate tabs without one identity replacing the other. The
opaque token is deliberate: changing its serialization to JWT would not fix
cookie selection or tab isolation, while database-backed sessions provide
immediate logout, rotation, and revocation without exposing credentials to
browser JavaScript.

The application switches each business transaction to the restricted
`app_backend` PostgreSQL role and sets the verified user/role as transaction-
local context. RLS then limits orders, conversations, messages, memory, support
requests, actions, and audit events to their owner. Admin-only policies guard
approval and evaluation records. Application checks still return clear 401/403
responses; RLS is the defense-in-depth boundary.

## Observability

- `/healthz`: lightweight liveness probe
- `/readyz`: application readiness and configured model
- `/metrics`: admin-only, low-cardinality Prometheus text metrics
- `/monitoring`: operator dashboard for queue depth, staged workflow latency,
  decisions, HITL quality, runtime health, and request audit timelines
- `/evals`: deterministic retrieval quality dashboard (Recall@1, Recall@3,
  category accuracy, ranking quality, and latency)

The dashboard and trace download are available only when
`SUPPORT_CHATBOT_EXPOSE_INTERNAL_UI=true`, require an administrator, and are
redacted on the server before serialization. `/metrics` also requires an
administrator and exposes aggregate, low-cardinality values only.

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
status, return window, and expected version are rechecked while holding the
order lock immediately before execution; a stale approval cannot overwrite a
newer action. The order event and unique action execution are committed in the
same transaction as the status change.

Only local telemetry/evaluation report files and the ChromaDB embedding cache
remain filesystem-backed. They are not customer/business state. Application
assets—the UI and knowledge Markdown—ship as immutable package data.

## Test tiers

- `tests/`: deterministic and network-free; runs on every commit.
- `evaluations/behavioral.py`: live model behavior and side effects.
- `evaluations/golden.py`: live answer correctness, grounding, and judge audit.

Live evaluations are release evidence, not a substitute for deterministic
authorization and state-transition tests.
