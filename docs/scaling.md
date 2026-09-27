# Scaling Ami to 1,000 online customers

The target is **1,000 concurrent browser sessions**, not 1,000 model calls per
second. Capacity depends on customer message rate and calls per turn. For
example, 1,000 customers sending one message per minute create about 17 new
turns per second; a two-call agent plan needs roughly 34 model requests per
second plus retries.

## Request path

The web request validates ownership, stores the request in PostgreSQL, enqueues
durable work, and returns `202 Accepted`. Model work never holds the customer
HTTP request open. Production must use Upstash Workflow; the bounded local
dispatcher is for one-container development only.

Capacity controls:

- `SUPPORT_CHATBOT_MODEL_MAX_CONCURRENCY` prevents a provider outage or burst
  from consuming every application worker.
- transient model errors use `Retry-After` where supplied, exponential backoff
  with jitter otherwise, and a total retry-time budget.
- the local queue is bounded. A full queue records an enqueue failure and uses
  the existing durable recovery/dead-letter path rather than growing memory.
- PostgreSQL pool acquisition has a short timeout and a bounded waiter count.
  Total possible connections are `application replicas × DB_POOL_MAX`; keep
  that below the database or Supabase pooler limit.
- process-local conversation state is an LRU/TTL read-through cache. Durable
  transcripts and compact memory remain in PostgreSQL and are reloaded after
  eviction or restart.
- prompt-cache routing uses a stable, hashed per-user key. This improves reuse
  of stable system/tool prefixes without sharing a cache-routing key between
  customers. Unsupported OpenAI-compatible proxies fall back safely.

## Production topology

Use at least two stateless web replicas behind TLS, Upstash Workflow for durable
execution, Supabase's connection pooler, and provider limits sized to the
measured calls-per-turn rate. Autoscale from queue age/depth and p95 latency,
not CPU alone. Keep action execution idempotent and database-authorized; adding
workers must never relax the existing proposal, approval, version, and action
hash checks.

The browser currently polls only active request state. Ticket-count polling is
low-frequency and pauses in hidden tabs. At materially larger scale, replace
polling with an authenticated push channel (SSE/WebSocket) backed by shared
pub/sub; do not keep subscriber state only inside a web process.

## Load test

Start Docker, then run:

```bash
python3 scripts/load_test_online_users.py --users 1000 --concurrency 200
```

This creates 1,000 isolated browser conversation IDs, reads each state, and
cleans them up. It does not call the LLM. To test the durable async path without
paid provider traffic:

```bash
python3 scripts/load_test_online_users.py --users 1000 --concurrency 200 --exercise-chat
```

That mode submits a deterministic security-boundary request which goes through
the database and workflow but is answered before the provider boundary. Provider
load testing must use a dedicated test project, explicit cost ceiling, and a
rate below the provider account's published limit. Passing either local test is
evidence for that topology only, not a universal production-capacity guarantee.
