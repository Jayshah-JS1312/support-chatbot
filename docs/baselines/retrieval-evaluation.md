# Retrieval evaluation baseline

This is the Phase 0 retrieval-quality baseline captured before the asynchronous
workflow migration. It is evidence for detecting later regressions, not a claim
that retrieval is already production-ready.

## Run context

- Captured: `2026-09-22T23:10:52Z`
- Source revision: `4ccf643c492fd7da1a017e0df522777131dda35e`
- Runtime: Docker, Python 3.12
- Embedding model: `TaylorAI/bge-micro-v2`
- Knowledge corpus: 48 chunks from 15 Markdown documents
- Cases: `src/support_chatbot/evaluation/retrieval.json`
- Command path: `POST /evals/run`
- Model-provider calls: none

## Summary

| Metric | Baseline |
|---|---:|
| Questions | 14 |
| Recall@1 | 64.3% (9/14) |
| Recall@3 | 92.9% (13/14) |
| Category accuracy | 92.9% (13/14) |
| Mean reciprocal rank | 0.774 |
| Median search latency | 3.76 ms |
| First-result misses | 5 |

The first query took 244.25 ms because it included process/model warm-up. The
median describes warm steady-state searches and must not be interpreted as a
cold-start latency guarantee.

## Per-case rank

| Case | Correct rank | Top category correct | Result |
|---|---:|---:|---|
| `return-window` | 2 | yes | top-3 pass |
| `return-process` | 1 | yes | pass |
| `gift-card-return` | 1 | yes | pass |
| `missing-package` | 1 | yes | pass |
| `tracking-stalled` | 1 | yes | pass |
| `damaged-arrival` | 1 | yes | pass |
| `cancel-window` | 1 | yes | pass |
| `address-change` | not found | yes | fail |
| `late-return` | 3 | yes | top-3 pass |
| `chargeback` | 1 | yes | pass |
| `gift-card-expiry` | 1 | yes | pass |
| `privacy-delete` | 1 | yes | pass |
| `second-refund` | 2 | no | top-3 pass |
| `upset-customer` | 2 | yes | top-3 pass |

The known complete miss is `address-change`. This baseline should remain
unchanged during infrastructure-only work; future retrieval improvements should
raise Recall@1/Recall@3 without reducing safety-oriented rule and regulation
coverage.
