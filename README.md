# Support Chatbot

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
- Deterministic unit tests and opt-in live LLM evaluations

The commerce backend is still a local simulator. Connecting authenticated,
production order APIs is a required future milestone.

## Repository layout

```text
src/support_chatbot/   application package, UI, and knowledge documents
tests/                 deterministic unit and policy tests
evaluations/           opt-in behavioral and model-judged evaluations
scripts/               feedback and retrieval benchmarking utilities
docs/                  architecture, deployment, and security guidance
.github/workflows/     CI for deterministic checks
```

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Set `OPENAI_API_KEY` in `.env`, then run either interface:

```bash
support-chatbot-web
support-chatbot
```

The browser UI is available at <http://127.0.0.1:8000>. Internal traces and
the `/logs` endpoints are disabled by default. Enable them only in a trusted
development environment with `SUPPORT_CHATBOT_EXPOSE_INTERNAL_UI=true`.

## Validation

```bash
pytest
python -m evaluations.behavioral --runs 1
python -m evaluations.golden --audit
python -m evaluations.golden
```

`pytest` is deterministic and makes no model or network calls. Everything under
`evaluations/` calls the configured model provider, can incur cost, and is
therefore intentionally excluded from default CI.

## Deployment

```bash
docker compose up --build
curl http://localhost:8000/healthz
```

Read [deployment](docs/deployment.md), [architecture](docs/architecture.md),
and [security](SECURITY.md) before exposing the service beyond localhost.

## Current production boundary

The repository now has a deployable package and container foundation, but the
application is not yet ready for real customer traffic. Authentication,
authorization, a durable transactional order backend, encrypted customer data,
rate limiting, and a production HTTP/application server remain mandatory work.
