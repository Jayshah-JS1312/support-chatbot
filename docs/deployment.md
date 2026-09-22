# Deployment

## Container

1. Copy `.env.example` to `.env` and set the model credentials.
2. Run `docker compose up --build`.
3. Verify `GET /healthz` returns `{"status":"ok"}`.

The container runs as an unprivileged user and persists runtime state beneath
`/data`. Internal logs and reasoning views are disabled by default.

## Environment

| Variable | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | Model-provider credential | required for model calls |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | required |
| `MODEL` | Chat model | `gpt-4o-mini` |
| `SUPPORT_CHATBOT_HOST` | Bind address | `127.0.0.1` |
| `SUPPORT_CHATBOT_PORT` | HTTP port | `8000` |
| `SUPPORT_CHATBOT_STATE_DIR` | Persistent application state | repository `state/` |
| `SUPPORT_CHATBOT_CACHE_DIR` | Model/vector cache | repository `.cache/` |
| `SUPPORT_CHATBOT_MAX_REQUEST_BYTES` | Request-body limit | `65536` |
| `SUPPORT_CHATBOT_SECURE_COOKIES` | Add the cookie `Secure` flag | `false` |
| `SUPPORT_CHATBOT_EXPOSE_INTERNAL_UI` | Enable traces and `/logs` | `false` |

## Before internet exposure

The current server is suitable for local validation and an internal demo. An
internet-facing release must first add:

- authenticated users and server-side authorization for every order
- a durable transactional database and idempotency keys for mutations
- a production ASGI/WSGI server behind TLS
- distributed rate limiting and abuse protection
- encrypted state, retention/deletion controls, and secret management
- authenticated operator-only diagnostics
- centralized structured logs with sensitive-field redaction
- provider timeouts, circuit breakers, and alerting

Do not enable the internal UI on a public deployment.
