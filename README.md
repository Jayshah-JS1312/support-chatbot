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
- Optional deterministic Phase-12 dataset with exactly 10,000 synthetic core
  business records spread across 12 test customers
- Customer/admin authentication with revocable, rotating server-side sessions
- PostgreSQL RLS-backed ownership isolation for customer data
- Durable `202 Accepted` support requests orchestrated by Upstash Workflow
- Human-approval state machine with retries, expiry, recovery, and dead letters
- Admin approval inbox with filtering, editing, reassignment, and audit history
- Fail-closed absence policy with reminders, supervisor escalation, hard expiry,
  and accelerated 60/120/300-second demo timing
- Normal customer chat bubbles with human-support status kept in a dedicated
  ticket page and a compact open-ticket count in the sidebar
- Strict role-separated surfaces: customers cannot open internal dashboards and
  administrators cannot use customer chat or customer conversation APIs
- End-to-end idempotency for submissions, deliveries, and action execution
- Cryptographically sealed cancellation/return proposals with optimistic revalidation
- Operations dashboard plus health, readiness, and Prometheus metrics endpoints
- Durable queue, staged-latency, decision, HITL-quality, and redacted
  per-request audit views for operators
- Deterministic retrieval-evaluation dashboard with versioned test cases
- Admin-only HITL evaluation dashboard with recall, reviewer-labelled precision,
  false-positive/negative inspection, queue health, and a 100% recall release gate
- Deterministic structured-action routing: safe reads and refusals complete
  automatically; privileged, irreversible, and unknown actions fail closed
- Four-wall regression coverage for latency, absence, regression, and tenant isolation
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
The HITL safety dashboard is at <http://127.0.0.1:8000/admin/hitl-evals>.
The admin approval inbox is at <http://127.0.0.1:8000/admin/approvals>.
Customers can track human-support tickets at <http://127.0.0.1:8000/tickets>,
and administrators manage them at <http://127.0.0.1:8000/admin/tickets>.
Monitoring and trace routes require operator authentication, and payloads are
redacted server-side before they reach the browser or trace download.
The operator-facing metrics experience is the visual `/monitoring` dashboard.
`/metrics` is intentionally raw Prometheus exposition for monitoring collectors
and is not linked as a human-facing page. Browser navigation to that endpoint
redirects to `/monitoring`.

Local demo sign-in accounts are:

| Role | Email | Password |
|---|---|---|
| Customer | `raj@example.com` | `RajDemo!2026` |
| Customer | `mei@example.com` | `MeiDemo!2026` |
| Admin | `admin@example.com` | `AdminDemo!2026` |

### Optional synthetic dataset

The normal application starts with only the small demo dataset. To install the
larger development dataset, run this explicit Docker profile:

```bash
docker compose --profile seed run --rm synthetic-seed
```

The command reproducibly replaces only dataset `synthetic-dev-phase12-v1` and
leaves demo/user-created data untouched. It creates 12 authenticated synthetic
customers (`synthetic+customer01@example.invalid` through
`synthetic+customer12@example.invalid`, password `SyntheticDemo!2026`) connected
to 3,000 orders, 4,000 order events, 2,000 workflow requests, and 1,000 support
tickets. It also creates representative conversations, approval drafts, pending
reviews, supervisor escalations, approvals, rejections, expiries, refunds, and
return edge cases.

Synthetic rows use the `[SYNTHETIC]` label, `SYN-*` references, reserved UUIDs,
and metadata containing `"synthetic": true`. The bulk seed is not a migration
and is never run during normal startup. Its CLI refuses to run unless the
environment, enable flag, exact database name, confirmation phrase, and a
non-hosted database target all pass validation.

These credentials are test fixtures only. Replace or remove them before using
the schema with real customer data.

For demo customers, order numbers, and more than 30 test conversations, see
[the manual testing guide](docs/manual-testing.md). Machine-readable scenarios
are also available in [`test-data/chat-scenarios.json`](test-data/chat-scenarios.json).

## Validation

```bash
pytest
python -m support_chatbot.hitl_evals --check
python -m evaluations.behavioral --runs 1
python -m evaluations.golden --audit
python -m evaluations.golden
```

`pytest` is deterministic and makes no model calls. PostgreSQL integration
tests run when `DATABASE_URL` is exported and otherwise report as skipped.
Everything under
`evaluations/` calls the configured model provider, can incur cost, and is
therefore intentionally excluded from default CI.

The deterministic HITL command is a release gate: it exits non-zero unless
every blocking case that requires a human actually pauses. Its dashboard always
shows recall beside escalation precision. Routing is performed at the structured
action boundary rather than from free-form customer text: approved read-only
actions and deterministic denials skip the queue, privileged actions pause, and
unknown actions fail closed. Operational precision comes from real reviewer labels.

The pre-migration retrieval measurements are recorded in
[`docs/baselines/retrieval-evaluation.md`](docs/baselines/retrieval-evaluation.md).

## Deployment

```bash
docker compose up --build
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
curl --cookie "ami_auth=<admin-session-token>" http://localhost:8000/metrics
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
