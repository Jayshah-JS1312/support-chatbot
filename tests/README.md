# Test strategy

Run the deterministic suite from the repository root:

```bash
pytest
```

These tests never call the model provider or download the embedding model.
They cover store transitions, tool contracts, memory persistence, planning,
policy enforcement, pricing, observability, and knowledge-document chunking.

Fixtures isolate mutable state:

- `fresh_store` restores simulated orders and returns after every test.
- `tmp_state` redirects traces and vector data into a temporary directory.
- `fake_llm` scripts model replies for planner tests.

Live, probabilistic checks belong under `evaluations/` and are never part of
default CI. Run them explicitly when validating a prompt, model, planner, or
release candidate.
