# Phase 12 synthetic dataset

Dataset `synthetic-dev-phase12-v1` is deterministic, optional, and intended only
for local development, performance checks, and isolation testing. It is never
installed by application startup or a schema migration.

## Contents

| Records | Count | Coverage |
| --- | ---: | --- |
| Authenticated customers | 12 | Many records per login; no mass account creation |
| Orders | 3,000 | 500 each preparing, shipped, delivered, cancelled, return-started, returned |
| Order events | 4,000 | Stable placed and state-transition history |
| Workflow requests | 2,000 | Tracking, knowledge, cancellations, returns, refund overrides, address changes, privacy deletion |
| Support tickets | 1,000 | 250 each open, in progress, resolved, closed |
| Approval tasks | 500 | Pending, supervisor-overdue, approved, rejected, expired |

Returns include expired-window, damaged-item, and already-returned cases. Refunds
include high-value overrides, partial refunds, and missing-original-payment cases.
Ownership is round-robin across customers so changing an identifier exercises
PostgreSQL RLS rather than finding a record owned by the same account.

All generated business text starts with `[SYNTHETIC]`, references start with
`SYN-`, emails use the reserved `example.invalid` domain, and JSON metadata
contains the dataset identifier and `"synthetic": true`.

## Run locally

```bash
docker compose --profile seed run --rm synthetic-seed
```

Every synthetic customer uses password `SyntheticDemo!2026`; for example:

```text
synthetic+customer01@example.invalid
synthetic+customer02@example.invalid
```

Running the command again deletes and rebuilds only the generator's 12 fixed
customer identities. Existing Raj, Mei, admin, and user-created data are not
deleted or overwritten.

## Production safety

The command requires all of the following before it opens a transaction:

1. `SUPPORT_CHATBOT_ENVIRONMENT` is `local`, `development`, or `test`.
2. `SUPPORT_CHATBOT_ENABLE_SYNTHETIC_SEED=true`.
3. `SUPPORT_CHATBOT_SYNTHETIC_SEED_DATABASE` exactly matches the connected DB.
4. The CLI receives the literal `--confirm SYNTHETIC_DATA_ONLY`.
5. The hostname is not a known hosted target such as Supabase, Render, AWS, or Neon.

The Docker seed service supplies these values only in its manually selected
`seed` profile. Do not copy them into a deployed application's environment.

## Local benchmark

On the development Docker PostgreSQL instance, a full replacement took under
0.6 seconds. Fetching the newest 50 of a synthetic customer's 250 orders used
`orders_user_created_idx` and completed in approximately 0.1 ms. These are local
smoke thresholds, not production capacity claims.
