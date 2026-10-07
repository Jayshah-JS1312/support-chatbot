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
test-data/             manual conversation prompts and expected outcomes
.github/workflows/     CI for deterministic checks
```

## Quick start: local Docker Compose

Each contributor runs their own PostgreSQL database in Docker. You do **not**
need a `DATABASE_URL` from the maintainer or a Supabase account to develop and
test this project. You need Docker with Compose, and a model-provider key only
if you want live Ami responses or live evaluations.

From the repository root:

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
curl -fsS http://localhost:8000/healthz
curl -fsS http://localhost:8000/readyz
```

Before starting, edit the new `.env` and set `OPENAI_API_KEY` plus an
`OPENAI_BASE_URL` and `MODEL` that **match your provider account**. The example
base URL targets the course provider; a key from a different provider needs
that provider's compatible endpoint and model name. Keep `.env` private: it is
Git-ignored and must never be committed. Without working model credentials,
the deterministic checks still run, but live chat/evaluations will not produce
normal model answers.

Open <http://localhost:8000> after the readiness check succeeds. Compose
starts PostgreSQL, waits for it to become healthy, applies pending versioned
migrations, and starts the FastAPI app. The database URL is supplied to the
containers by [`compose.yaml`](compose.yaml); you do not need to obtain or edit
one for this path. QStash/Upstash credentials are **not** needed for local
workflow testing: the app uses a local dispatcher. That dispatcher is not a
production multi-instance queue.

If startup fails, inspect `docker compose ps` and `docker compose logs app
migrate postgres` (omit `-f` to avoid a continuously running log command).
The Compose file publishes port 8000 on the host's interfaces and includes
known demo credentials. Use it only on a trusted development machine; do not
forward that port or expose this stack to the internet.
`docker compose down` stops the stack but preserves the named PostgreSQL volume
and your data. Do **not** run `docker compose down -v` unless you deliberately
want to delete this local database and start over.

### Where `DATABASE_URL` comes from

| Where the application runs | PostgreSQL host in the URL | Who supplies it |
|---|---|---|
| Docker Compose app/migrations | `postgres:5432` | `compose.yaml` automatically |
| Python on your laptop, database in Compose | `127.0.0.1:5432` | `.env.example` / your local `.env` |
| Hosted deployment | Provider-specific host | Your own managed PostgreSQL or Supabase project |

The hostname `postgres` resolves **inside** the Compose network only. The
`127.0.0.1` URL works from your laptop because Compose publishes PostgreSQL on
localhost. Hosted Supabase is optional for contributors and is not installed or
provisioned by cloning this repository. Never share a production database URL
or credentials in Git or chat.

### Alternative: run Python on the host

Start only the database in Docker, then use a local Python 3.11+ environment:

```bash
docker compose up -d postgres
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
support-chatbot-migrate
support-chatbot-web
```

Use the host-style `DATABASE_URL` already in `.env.example`; do not change it
to `@postgres` for this path. Configure the model provider in `.env` as above.
`support-chatbot-web` runs Uvicorn; the optional terminal interface is
`support-chatbot`. For tooling that expects an ASGI import string, use
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
| Trust-study customer | `priya@example.com` | `PriyaTrust!2026` |
| Trust-study customer | `noah@example.com` | `NoahTrust!2026` |
| Admin | `admin@example.com` | `AdminDemo!2026` |

Raj, Priya, and Noah have isolated, versioned order scenarios for the trust
study. Their frozen states, prompts, and screenshot protocol are recorded
in [`docs/trust-study/baseline.md`](docs/trust-study/baseline.md). Use a separate
account for every participant when testing against one shared environment.

The standard migrations do not create the two additional trust-study accounts
in production. Install the frozen fixtures explicitly in a local Compose
database before the study:

```bash
docker compose --profile trust-study run --rm trust-study-seed
```

The seed is additive and idempotent: it does not reset an existing password or
overwrite an order whose state has changed. Use a fresh development database
when you need the exact starting state again.

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

For demo customers, order numbers, and manual test conversations, see
[the manual testing guide](docs/manual-testing.md). Machine-readable scenarios
with account and human-review expectations are in
[`test-data/chat-scenarios.json`](test-data/chat-scenarios.json).

## Validation

```bash
python -m pytest
python -m support_chatbot.hitl_evals --check
python -m evaluations.trust_suite --check
```

These deterministic checks make no model calls. From a host virtualenv, start
the Compose database and apply migrations as shown above before running tests
that need PostgreSQL. PostgreSQL integration tests run only when `DATABASE_URL`
is **exported in the shell** (a value in `.env` alone does not activate them):

```bash
export DATABASE_URL=postgresql://support_chatbot:support_chatbot@127.0.0.1:5432/support_chatbot
python -m pytest
```

Use only a disposable development database for integration tests. When the
variable is absent, PostgreSQL integration tests report as skipped. CI runs
them against its own temporary PostgreSQL service.

The following are optional **live-model** evaluations and can incur provider
cost; run them only after configuring working model credentials:

```bash
python -m evaluations.behavioral --runs 1
python -m evaluations.golden --audit
python -m evaluations.golden
```

These live evaluations are intentionally excluded from default CI.

The 20-journey frozen trust contract and its expected outcomes are documented in
[`docs/trust-study/trust-suite.md`](docs/trust-study/trust-suite.md). The
deterministic command above validates its required fields and frozen checksum;
it makes no model calls.

The deterministic HITL command is a release gate: it exits non-zero unless
every blocking case that requires a human actually pauses. Its dashboard always
shows recall beside escalation precision. Routing is performed at the structured
action boundary rather than from free-form customer text: approved read-only
actions and deterministic denials skip the queue, privileged actions pause, and
unknown actions fail closed. Operational precision comes from real reviewer labels.

The pre-migration retrieval measurements are recorded in
[`docs/baselines/retrieval-evaluation.md`](docs/baselines/retrieval-evaluation.md).

## Deployment

The quick start above is for **local development**, not an internet-facing
deployment. Read [deployment](docs/deployment.md), [architecture](docs/architecture.md),
[scaling and load testing](docs/scaling.md), and [security](SECURITY.md) before
exposing the service beyond localhost.

## Current production boundary

The repository now has a FastAPI/Uvicorn service, durable PostgreSQL business
state, customer/admin authentication, ownership isolation, and an asynchronous
human-approval workflow. Privileged actions are previewed, customer-confirmed,
cryptographically sealed, human-approved, and revalidated immediately before
exactly-once execution. Administrators review work in a durable approval inbox,
including context, evidence, edits, reassignment, and an audit trail. It is not
ready for real customer traffic until encrypted customer-data operations, rate limiting,
TLS/reverse-proxy configuration, and email-based reset delivery are complete.
