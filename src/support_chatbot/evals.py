"""Deterministic retrieval evaluation used by the operator dashboard.

This suite measures the local knowledge index only. It never calls the paid
LLM, which makes it safe to run on demand and in CI. Live agent behavior stays
in the opt-in suites under the repository's top-level ``evaluations`` folder.
"""

import json
import threading
import time
from datetime import datetime, timezone

from support_chatbot import PACKAGE_ROOT, STATE_DIR
from support_chatbot import knowledge, observe

CASES_FILE = PACKAGE_ROOT / "evaluation" / "retrieval.json"
REPORT_FILE = STATE_DIR / "retrieval-evaluation.json"
_lock = threading.Lock()


def _matches(hit, expected):
    return hit["heading"] in expected or hit["source"] in expected


def _percent(numerator, denominator):
    return round(100 * numerator / denominator, 1) if denominator else 0.0


def run():
    """Run every retrieval case, persist the report, and return it."""
    if not _lock.acquire(blocking=False):
        return {"status": "running"}
    try:
        cases = json.loads(CASES_FILE.read_text())
        results = []
        for case in cases:
            started = time.perf_counter()
            hits = knowledge.search(case["question"], k=3, log_event=False)
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            rank = next(
                (index for index, hit in enumerate(hits, 1)
                 if _matches(hit, case["expected"])),
                None,
            )
            results.append({
                **case,
                "hits": [
                    {"heading": hit["heading"], "source": hit["source"],
                     "category": hit["category"], "score": hit["score"]}
                    for hit in hits
                ],
                "rank": rank,
                "category_correct": bool(hits and hits[0]["category"] == case["category"]),
                "ms": elapsed,
                "passed": rank is not None,
            })

        count = len(results)
        latencies = sorted(result["ms"] for result in results)
        summary = {
            "questions": count,
            # One local knowledge-index search is performed for every case.
            # Keep the aggregate and breakdown explicit so operators do not
            # mistake retrieval work for paid model traffic.
            "total_calls": count,
            "retrieval_calls": count,
            "model_calls": 0,
            "estimated_cost_usd": 0.0,
            "recall_at_1": _percent(sum(r["rank"] == 1 for r in results), count),
            "recall_at_3": _percent(sum(r["rank"] is not None for r in results), count),
            "category_accuracy": _percent(sum(r["category_correct"] for r in results), count),
            "mrr": round(sum(1 / r["rank"] if r["rank"] else 0 for r in results) / count, 3)
            if count else 0,
            "median_ms": latencies[count // 2] if count else 0,
            "first_result_misses": sum(r["rank"] != 1 for r in results),
        }
        report = {
            "status": "complete",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "results": results,
        }
        REPORT_FILE.parent.mkdir(exist_ok=True)
        temporary = REPORT_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2))
        temporary.replace(REPORT_FILE)
        observe.log("eval", suite="retrieval", **summary)
        return report
    finally:
        _lock.release()


def latest():
    """Return the last completed report without starting expensive work."""
    try:
        return json.loads(REPORT_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"status": "not_run", "summary": None, "results": []}
