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
- Confirmation gates for state-changing actions
- Card-number redaction, injection signalling, and output identifier checks
- A local ChromaDB knowledge index using `bge-micro-v2`
- Browser and terminal interfaces
- Responsive customer chat UI with built-in demo prompts
- FastAPI application served by Uvicorn with validated request schemas
- PostgreSQL-backed orders, conversations, verified customer memory, and audit data
- Supabase-compatible versioned migrations with reproducible demo seeds
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

Set `OPENAI_API_KEY` and `DATABASE_URL` in `.env`, apply migrations, then run
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
Internal traces can contain customer text, so put that route behind operator
authentication in production.

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

The repository now has a FastAPI/Uvicorn service and durable PostgreSQL business
state, but it is not ready for real customer traffic. Authentication,
authorization, encrypted customer data, rate limiting, TLS/reverse-proxy
configuration, and the asynchronous human-approval workflow remain mandatory.
