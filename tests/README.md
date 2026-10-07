# Test strategy

Run the deterministic suite from the repository root:

```bash
python -m pytest
```

These tests never call the model provider or download the embedding model.
They cover store transitions, tool contracts, memory persistence, planning,
policy enforcement, pricing, observability, knowledge-document chunking, and
FastAPI HTTP contracts. `test_four_walls.py` adds release-critical latency,
absence, retry, concurrency, tenant-isolation, callback, and role-boundary
contracts. HTTP tests use an isolated temporary runtime and fake
planner, so those HTTP tests make no model or network calls.

PostgreSQL integration tests are the exception: they run only when
`DATABASE_URL` is exported in the shell and PostgreSQL is reachable. Compose
starts a local development database and applies migrations; from a host
virtualenv use
`postgresql://support_chatbot:support_chatbot@127.0.0.1:5432/support_chatbot`.
Do not point integration tests at production or shared customer data. Without
the exported variable, these tests are skipped. CI supplies its own temporary
PostgreSQL service and runs them.

Fixtures isolate mutable state:

- `fresh_store` restores simulated orders and returns after every test.
- `tmp_state` redirects traces and vector data into a temporary directory.
- `fake_llm` scripts model replies for planner tests.

Live, probabilistic checks belong under `evaluations/` and are never part of
default CI. Run them explicitly when validating a prompt, model, planner, or
release candidate.
