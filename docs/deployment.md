# Deployment

## Container

1. Copy `.env.example` to `.env` and set the model credentials.
2. Run `docker compose up --build`.
3. Verify `GET /healthz` returns `{"status":"ok"}` and `GET /readyz`
   reports `ready`.

Compose starts PostgreSQL, runs every pending migration, and only then starts
the FastAPI/Uvicorn container as an unprivileged user. PostgreSQL uses a named
volume, so orders, conversations, messages, and memory survive app/container
restarts. The Compose configuration enables
the monitoring console for the local demo. Disable it before a public
deployment unless the route is protected by operator authentication.

## Environment

| Variable | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | Model-provider credential | required for model calls |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | required |
| `MODEL` | Chat model | `gpt-4o-mini` |
| `DATABASE_URL` | PostgreSQL/Supabase connection string | local PostgreSQL |
| `SUPPORT_CHATBOT_DB_POOL_MIN` | Minimum backend connections per process | `1` |
| `SUPPORT_CHATBOT_DB_POOL_MAX` | Maximum backend connections per process | `5` |
| `SUPPORT_CHATBOT_HOST` | Bind address | `127.0.0.1` |
| `SUPPORT_CHATBOT_PORT` | HTTP port | `8000` |
| `SUPPORT_CHATBOT_CACHE_DIR` | Model/vector cache | repository `.cache/` |
| `SUPPORT_CHATBOT_MAX_REQUEST_BYTES` | Request-body limit | `65536` |
| `SUPPORT_CHATBOT_SECURE_COOKIES` | Add the cookie `Secure` flag | `false` |
| `SUPPORT_CHATBOT_EXPOSE_INTERNAL_UI` | Enable traces and `/monitoring` | `false` |

## Runtime endpoints

| Endpoint | Purpose | Exposure |
|---|---|---|
| `/healthz` | Liveness probe | aggregate, no customer data |
| `/readyz` | Readiness and model name | aggregate, no customer data |
| `/metrics` | Prometheus metrics | aggregate, no customer data |
| `/monitoring` | Operator dashboard | internal only |
| `/trace.jsonl` | Raw agent events | internal only |

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

- authenticated users and server-side authorization for every order
- idempotency keys supplied and enforced end-to-end at the HTTP boundary
- a managed reverse proxy/load balancer with TLS in front of Uvicorn
- distributed rate limiting and abuse protection
- encrypted state, retention/deletion controls, and secret management
- authenticated operator-only diagnostics
- centralized structured logs with sensitive-field redaction
- provider timeouts, circuit breakers, and alerting

Do not enable the internal UI on a public deployment.
