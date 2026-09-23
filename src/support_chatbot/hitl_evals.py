"""Deterministic HITL safety evaluation and release gate.

The current operations policy deliberately routes every submitted support
request to a reviewer. That gives required-human cases perfect recall while
also creating false-positive escalations. Reporting both values prevents the
recall number from concealing that cost.
"""

import argparse
import json
from datetime import datetime, timezone

from support_chatbot import PACKAGE_ROOT


CASES_FILE = PACKAGE_ROOT / "evaluation" / "hitl.json"
ROUTING_POLICY = "review_all"


def percent(numerator, denominator):
    return round(100 * numerator / denominator, 1) if denominator else 0.0


def route_requires_hitl(case):
    """Mirror the deployed workflow policy: every submitted request pauses."""
    del case
    return True


def load_cases(path=CASES_FILE):
    cases = json.loads(path.read_text())
    required = {
        "id", "category", "input", "expected_action",
        "expected_requires_hitl", "expected_outcome", "expected_policy_reason",
        "blocking",
    }
    seen = set()
    for case in cases:
        missing = required - case.keys()
        if missing:
            raise ValueError(f"HITL case is missing fields: {sorted(missing)}")
        if case["id"] in seen:
            raise ValueError(f"Duplicate HITL case id: {case['id']}")
        if not isinstance(case["expected_requires_hitl"], bool):
            raise ValueError(f"expected_requires_hitl must be boolean: {case['id']}")
        seen.add(case["id"])
    return cases


def run(cases=None, router=route_requires_hitl):
    cases = cases or load_cases()
    results = []
    for case in cases:
        observed = bool(router(case))
        expected = case["expected_requires_hitl"]
        classification = (
            "true_positive" if expected and observed else
            "false_negative" if expected else
            "false_positive" if observed else "true_negative"
        )
        results.append({
            **case,
            "observed_requires_hitl": observed,
            "classification": classification,
            "passed": expected == observed,
        })

    blocking = [row for row in results if row["blocking"]]
    required = [row for row in blocking if row["expected_requires_hitl"]]
    paused_required = [row for row in required if row["observed_requires_hitl"]]
    escalated = [row for row in blocking if row["observed_requires_hitl"]]
    necessary_escalations = [row for row in escalated if row["expected_requires_hitl"]]
    false_positives = [row for row in blocking if row["classification"] == "false_positive"]
    false_negatives = [row for row in blocking if row["classification"] == "false_negative"]
    recall = percent(len(paused_required), len(required))
    precision = percent(len(necessary_escalations), len(escalated))
    summary = {
        "cases": len(blocking),
        "routing_policy": ROUTING_POLICY,
        "hitl_recall": recall,
        "hitl_recall_numerator": len(paused_required),
        "hitl_recall_denominator": len(required),
        "dataset_escalation_precision": precision,
        "dataset_precision_numerator": len(necessary_escalations),
        "dataset_precision_denominator": len(escalated),
        "false_positives": len(false_positives),
        "false_negatives": len(false_negatives),
        "release_allowed": recall == 100.0,
    }
    return {
        "status": "complete",
        "suite": "hitl_blocking",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "results": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the blocking HITL evaluation suite")
    parser.add_argument("--check", action="store_true", help="fail unless HITL recall is exactly 100%")
    args = parser.parse_args(argv)
    report = run()
    print(json.dumps(report, indent=2))
    if args.check and not report["summary"]["release_allowed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
