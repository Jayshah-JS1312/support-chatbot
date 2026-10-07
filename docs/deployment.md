# Deployment

## Container

1. Copy `.env.example` to `.env` and set model credentials matching your
   provider's API endpoint and model. Do not commit `.env`.
2. Run `docker compose up --build`.
3. Verify `GET /healthz` returns `{"status":"ok"}` and `GET /readyz`
   reports `ready`.

Compose starts PostgreSQL, runs every pending migration, and only then starts
the FastAPI/Uvicorn container as an unprivileged user. PostgreSQL uses a named
volume, so orders, conversations, messages, and memory survive app/container
restarts. The Compose configuration enables
the monitoring console for the local demo. The Compose `DATABASE_URL` points
to the `postgres` service inside its network; the `127.0.0.1` URL in
`.env.example` is for Python running on the host, not inside a container. No
hosted Supabase project is needed for this local path. This Compose file also
uses development database credentials and is not a public deployment template.
Disable the internal UI before any public deployment unless the route is
protected by operator authentication.

## Environment

| Variable | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | Model-provider credential | required for model calls |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | required |
| `MODEL` | Chat model | `gpt-4o-mini` |
| `DATABASE_URL` | PostgreSQL/Supabase connection string | local PostgreSQL |
| `SUPPORT_CHATBOT_DB_POOL_MIN` | Minimum backend connections per process | `1` |
| `SUPPORT_CHATBOT_DB_POOL_MAX` | Maximum backend connections per process | `10` |
| `SUPPORT_CHATBOT_HOST` | Bind address | `127.0.0.1` |
| `SUPPORT_CHATBOT_PORT` | HTTP port | `8000` |
| `SUPPORT_CHATBOT_CACHE_DIR` | Model/vector cache | repository `.cache/` by default; Compose mounts `/data/cache` |
| `SUPPORT_CHATBOT_MAX_REQUEST_BYTES` | Request-body limit | `65536` |
| `SUPPORT_CHATBOT_SECURE_COOKIES` | Add the cookie `Secure` flag | `false` |
| `SUPPORT_CHATBOT_EXPOSE_INTERNAL_UI` | Enable traces and `/monitoring` | `false` |
| `SUPPORT_CHATBOT_AUTH_SESSION_HOURS` | Login-session lifetime | `24` |
| `SUPPORT_CHATBOT_EXPOSE_RESET_TOKEN` | Return reset token in API response; local testing only | `false` |
| `SUPPORT_CHATBOT_PUBLIC_BASE_URL` | Public HTTPS origin reachable by QStash | required for external workflows; not local dispatcher |
| `QSTASH_TOKEN` | Server-side Upstash/QStash publish credential | required for external workflows; not local dispatcher |
| `QSTASH_CURRENT_SIGNING_KEY` | Verify current QStash callback signatures | required for external workflows; not local dispatcher |
| `QSTASH_NEXT_SIGNING_KEY` | Verify callbacks during key rotation | required for external workflows; not local dispatcher |
| `SUPPORT_CHATBOT_WORKFLOW_RETRIES` | Upstash delivery retry count | `3` |
| `SUPPORT_CHATBOT_WORKFLOW_LEASE_SECONDS` | Crash-recovery claim lease | `300` |
| `SUPPORT_CHATBOT_APPROVAL_REMINDER_SECONDS` | First customer-delay reminder | `3600` |
| `SUPPORT_CHATBOT_APPROVAL_ESCALATION_SECONDS` | Move unanswered work to supervisor queue | `7200` |
| `SUPPORT_CHATBOT_APPROVAL_EXPIRY_SECONDS` | Final fail-closed deadline | `86400` |
| `SUPPORT_CHATBOT_ABSENCE_SCAN_SECONDS` | Durable absence-policy sweep interval | `15` |
| `SUPPORT_CHATBOT_ABSENCE_DEMO_MODE` | Override deadlines with 60s/120s/300s | `false` |
| `SUPPORT_CHATBOT_MODEL_TIMEOUT_SECONDS` | Provider request deadline | `30` |
| `SUPPORT_CHATBOT_WORKFLOW_ENQUEUE_MAX_ATTEMPTS` | Fail-closed queue publish limit | `5` |

## Runtime endpoints

| Endpoint | Purpose | Exposure |
|---|---|---|
| `/healthz` | Liveness probe | aggregate, no customer data |
| `/readyz` | Readiness and model name | aggregate, no customer data |
| `/metrics` | Prometheus metrics | admin authentication; aggregate, no customer data |
| `/monitoring` | Operator dashboard | authenticated admin only |
| `/logs.json` | Redacted runtime and durable workflow telemetry | authenticated admin only |
| `/admin/operations/requests/{id}.json` | Redacted request state and audit timeline | authenticated admin only |
| `/admin/approvals` | Human approval inbox | authenticated admin only |
| `/evals` | Retrieval evaluation dashboard | authenticated admin only |
| `/admin/hitl-evals` | HITL recall, escalation precision, and release gate | authenticated admin only |
| `/trace.jsonl` | Server-redacted agent events | authenticated admin only |
| `POST /chat` | Store and enqueue a request; returns `202` | authenticated customer |
| `GET /requests` | Restore the customer's recent ticket timeline | authenticated owner |
| `GET /requests/{id}` | Poll an owned request | authenticated owner |
| `POST /actions/preview` | Create a non-mutating sealed action preview | authenticated customer |
| `POST /actions/proposals/{id}/confirm` | Confirm the exact seal and create an approval task; returns `202` | authenticated owner |
| `POST /admin/requests/{id}/decision` | Approve or reject the sealed draft | authenticated admin |
| `POST /admin/requests/{id}/reassign` | Assign pending work to another admin | authenticated admin |
| `POST /workflow/requests` | Signed Upstash workflow delivery | QStash only |
| `POST /workflow/recover` | Retry stored-but-unenqueued work | authenticated admin |
| `POST /workflow/expire` | Apply the approval absence policy | authenticated admin |
| `POST /workflow/absence-policy` | Run reminder, escalation, and expiry sweep | authenticated admin |

The demo password-reset endpoint can expose its token only when
`SUPPORT_CHATBOT_EXPOSE_RESET_TOKEN=true`. Keep this disabled in every deployed
environment and connect reset-token delivery to a transactional email provider.

Without QStash credentials the application automatically uses a local
background dispatcher. This makes Docker development functional but is not a
multi-instance production queue. Production deployments must configure the
three QStash credentials and a public HTTPS base URL.

## Hosted Supabase

The committed `supabase/config.toml`, migrations, and seed file form a local
Supabase project. To provision the hosted database, create a free Supabase
project in your account, then run:

```bash
supabase login
supabase link --project-ref YOUR_PROJECT_REF
supabase db push --dry-run
supabase db push
```

For this long-running container, use the direct connection string when the host
supports IPv6; otherwise use Supabase's session-pooler string. Add
`sslmode=require` (or `verify-full` with the downloaded CA) in production. Do
not use `--include-seed` against a real production database.

## Before internet exposure

The current server is suitable for local validation and an internal demo. An
internet-facing release must first add:

- a managed reverse proxy/load balancer with TLS in front of Uvicorn
- distributed rate limiting and abuse protection
- encrypted state, retention/deletion controls, and secret management
- authenticated operator-only diagnostics
- centralized structured logs with sensitive-field redaction
- provider timeouts, circuit breakers, and alerting

The app runs an idempotent absence sweep in the background; a production
scheduler may also call `POST /workflow/absence-policy` as a recovery backstop.
Upstash performs delivery retries, while `/workflow/recover` closes the
store-before-enqueue crash window. The written absence policy is fail closed:
an unanswered approval is reminded, moved to the supervisor queue, then expires
and completes without executing the proposed action. Late approval returns
`409`, and execution rechecks the durable deadline.

Do not enable the internal UI on a public deployment.
