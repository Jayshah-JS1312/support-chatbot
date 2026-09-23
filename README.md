# Customer Support Agent

A tool-using customer-support agent with deterministic policy checks, short- and
long-term memory, local retrieval, observability, and live-model evaluations.

This repository contains one supported application. The former teaching stages
have been consolidated; the policy-enabled implementation is now the product
baseline.

## What is included

- ReAct and plan-and-execute planners
- Order lookup, tracking, cancellation, return, knowledge, and escalation tools
- Separate conversation, working, and customer memory
- Customer-confirmation and human-approval gates for state-changing actions
- Card-number redaction, injection signalling, and output identifier checks
- A local ChromaDB knowledge index using `bge-micro-v2`
- Browser and terminal interfaces
- Responsive customer chat UI with built-in demo prompts
- FastAPI application served by Uvicorn with validated request schemas
- PostgreSQL-backed orders, conversations, verified customer memory, and audit data
- Supabase-compatible versioned migrations with reproducible demo seeds
- Customer/admin authentication with revocable, rotating server-side sessions
- PostgreSQL RLS-backed ownership isolation for customer data
- Durable `202 Accepted` support requests orchestrated by Upstash Workflow
- Human-approval state machine with retries, expiry, recovery, and dead letters
- Admin approval inbox with filtering, editing, reassignment, and audit history
- Fail-closed absence policy with reminders, supervisor escalation, hard expiry,
  and accelerated 60/120/300-second demo timing
- End-to-end idempotency for submissions, deliveries, and action execution
- Cryptographically sealed cancellation/return proposals with optimistic revalidation
- Operations dashboard plus health, readiness, and Prometheus metrics endpoints
- Deterministic retrieval-evaluation dashboard with versioned test cases
- Deterministic unit tests and opt-in live LLM evaluations

The commerce workflows use durable local PostgreSQL records, but they still
simulate an external retailer. Connecting authenticated production commerce
APIs is a required future milestone.

## Repository layout

```text
src/support_chatbot/   application package, UI, and knowledge documents
tests/                 deterministic unit and policy tests
evaluations/           opt-in behavioral and model-judged evaluations
scripts/               feedback and retrieval benchmarking utilities
supabase/              versioned PostgreSQL schema and reproducible demo seed
docs/                  architecture, deployment, and security guidance
test-data/             ready-to-run manual conversation scenarios
.github/workflows/     CI for deterministic checks
```

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Set `OPENAI_API_KEY` and `DATABASE_URL` in `.env`. For asynchronous processing,
also set the QStash token/signing keys and the application's public base URL.
Apply migrations, then run
either interface:

```bash
support-chatbot-migrate
support-chatbot-web
support-chatbot
```

`support-chatbot-web` starts the FastAPI application through Uvicorn. For
development tooling that expects an ASGI import string, use
`uvicorn support_chatbot.web:app --host 127.0.0.1 --port 8000`.

The browser UI is available at <http://127.0.0.1:8000>. When internal views are
enabled, the monitoring console is at <http://127.0.0.1:8000/monitoring>.
The retrieval evaluation dashboard is at <http://127.0.0.1:8000/evals>.
The admin approval inbox is at <http://127.0.0.1:8000/admin/approvals>.
Internal traces can contain customer text, so put that route behind operator
authentication in production.

Local demo sign-in accounts are:

| Role | Email | Password |
|---|---|---|
| Customer | `raj@example.com` | `RajDemo!2026` |
| Customer | `mei@example.com` | `MeiDemo!2026` |
| Admin | `admin@example.com` | `AdminDemo!2026` |

These credentials are test fixtures only. Replace or remove them before using
the schema with real customer data.

For demo customers, order numbers, and more than 30 test conversations, see
[the manual testing guide](docs/manual-testing.md). Machine-readable scenarios
are also available in [`test-data/chat-scenarios.json`](test-data/chat-scenarios.json).

## Validation

```bash
pytest
python -m evaluations.behavioral --runs 1
python -m evaluations.golden --audit
python -m evaluations.golden
```

`pytest` is deterministic and makes no model calls. PostgreSQL integration
tests run when `DATABASE_URL` is exported and otherwise report as skipped.
Everything under
`evaluations/` calls the configured model provider, can incur cost, and is
therefore intentionally excluded from default CI.

The pre-migration retrieval measurements are recorded in
[`docs/baselines/retrieval-evaluation.md`](docs/baselines/retrieval-evaluation.md).

## Deployment

```bash
docker compose up --build
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
curl http://localhost:8000/metrics
```

Read [deployment](docs/deployment.md), [architecture](docs/architecture.md),
and [security](SECURITY.md) before exposing the service beyond localhost.

## Current production boundary

The repository now has a FastAPI/Uvicorn service, durable PostgreSQL business
state, customer/admin authentication, ownership isolation, and an asynchronous
human-approval workflow. Privileged actions are previewed, customer-confirmed,
cryptographically sealed, human-approved, and revalidated immediately before
exactly-once execution. Administrators review work in a durable approval inbox,
including context, evidence, edits, reassignment, and an audit trail. It is not
ready for real customer traffic until encrypted customer-data operations, rate limiting,
TLS/reverse-proxy configuration, and email-based reset delivery are complete.
