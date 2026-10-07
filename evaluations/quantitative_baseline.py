"""Build the Task 3 baseline from frozen, independently inspectable evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from evaluations.trust_suite import load as load_trust, validate as validate_trust
from support_chatbot import hitl_evals
from support_chatbot.llm import MODEL


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path(__file__).with_name("trust_evidence.json")
DEFAULT_OUT = ROOT / "artifacts/evaluations/quantitative-baseline.json"
CONFUSION_RISK_IDS = {
    "trust-02-indirect-order-follow-up", "trust-03-ambiguous-product",
    "trust-05-eligible-return-approval", "trust-06-explicit-human-handoff",
    "trust-09-admin-rejects-action", "trust-10-approval-expiry",
    "trust-14-model-failure", "trust-15-retrieval-failure",
    "trust-16-refresh-pending-workflow", "trust-17-ambiguous-confirmation",
}
INCORRECT_SUCCESS_IDS = {
    "trust-04-ineligible-cancellation", "trust-05-eligible-return-approval",
    "trust-08-admin-approves-action", "trust-09-admin-rejects-action",
    "trust-10-approval-expiry", "trust-14-model-failure",
    "trust-15-retrieval-failure", "trust-18-duplicate-submission",
    "trust-19-stale-approval",
}


def percentile(values, fraction):
    if not values:
        return 0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, math.ceil(len(ordered) * fraction) - 1),
    )
    return ordered[index]


def _load_json(path):
    return json.loads(Path(path).read_text())


def _junit_results(path):
    root = ET.parse(path).getroot()
    results = {}
    for case in root.iter("testcase"):
        failed = case.find("failure") is not None or case.find("error") is not None
        skipped = case.find("skipped") is not None
        results.setdefault(case.attrib["name"].split("[")[0], []).append(
            "failed" if failed else "skipped" if skipped else "passed"
        )
    return results


def _journey_results(junit_path):
    tests = _junit_results(junit_path)
    mapping = _load_json(EVIDENCE)
    trust_rows = load_trust()
    errors = validate_trust(trust_rows)
    if errors:
        raise ValueError("Frozen trust suite is invalid: " + "; ".join(errors))
    if set(mapping) != {row["id"] for row in trust_rows}:
        raise ValueError("Trust evidence map must cover every frozen journey exactly once")

    journeys = []
    for row in trust_rows:
        names = mapping[row["id"]]
        missing = [name for name in names if name not in tests]
        statuses = [status for name in names for status in tests.get(name, [])]
        passed = not missing and statuses and all(status == "passed" for status in statuses)
        journeys.append({
            "id": row["id"], "title": row["title"], "passed": bool(passed),
            "evidence_tests": names, "missing_tests": missing,
            "observed_test_outcomes": statuses,
        })
    return journeys


def _golden_metrics(path):
    groups = _load_json(path)
    outcomes = [outcome for group in groups for outcome in group["outcomes"]]
    if not outcomes:
        raise ValueError("Golden report contains no outcomes")
    planners = sorted({group.get("planner", "unknown") for group in groups})
    agent_models = sorted({group.get("agent_model", MODEL) for group in groups})
    judge_models = sorted({group.get("judge_model", MODEL) for group in groups})
    return {
        "planners": planners,
        "agent_models": agent_models,
        "judge_models": judge_models,
        "rows": len(groups),
        "outcomes": len(outcomes),
        "clean_outcomes": sum(not outcome["fails"] for outcome in outcomes),
        "quality_score_percent": round(
            100 * sum(outcome["score"] for outcome in outcomes) / len(outcomes), 1
        ),
        "agent_model_calls": sum(outcome.get("llm_calls", 0) for outcome in outcomes),
        "judge_model_calls": sum(outcome.get("judge_calls", 0) for outcome in outcomes),
        "total_model_calls": sum(
            outcome.get("llm_calls", 0) + outcome.get("judge_calls", 0)
            for outcome in outcomes
        ),
        "agent_tokens": sum(outcome.get("tokens", 0) for outcome in outcomes),
        "judge_tokens": sum(outcome.get("judge_tokens", 0) for outcome in outcomes),
        "total_tokens": sum(
            outcome.get("tokens", 0) + outcome.get("judge_tokens", 0)
            for outcome in outcomes
        ),
        "agent_cost_usd": round(sum(outcome.get("cost", 0) for outcome in outcomes), 6),
        "judge_cost_usd": round(sum(outcome.get("judge_cost", 0) for outcome in outcomes), 6),
        "complete_run_cost_usd": round(sum(
            outcome.get("cost", 0) + outcome.get("judge_cost", 0)
            for outcome in outcomes
        ), 6),
        "response_latency_p50_ms": percentile([outcome["ms"] for outcome in outcomes], 0.50),
        "response_latency_p95_ms": percentile([outcome["ms"] for outcome in outcomes], 0.95),
    }


def build(golden_path, junit_path, load_path, judge_audit_path, commit=None):
    golden = _golden_metrics(golden_path)
    journeys = _journey_results(junit_path)
    hitl = hitl_evals.run()["summary"]
    load = _load_json(load_path)
    judge_audit = _load_json(judge_audit_path)
    passed = sum(row["passed"] for row in journeys)
    failures = {row["id"] for row in journeys if not row["passed"]}
    commit = commit or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    return {
        "status": "complete",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit": commit,
        "model": MODEL,
        "run_configuration": {
            "planners": golden.pop("planners"),
            "agent_models": golden.pop("agent_models"),
            "judge_models": golden.pop("judge_models"),
            "provider_host": urlsplit(os.getenv("OPENAI_BASE_URL", "")).hostname
            or "not-configured",
            "golden_runs_per_row": golden["outcomes"] // golden["rows"],
        },
        "suite": {
            "trust_journeys": len(journeys),
            "golden_answer_rows": golden["rows"],
            "note": "Workflow completion uses the 20 frozen trust journeys; live answer quality uses the separately frozen golden-answer rows.",
        },
        "metrics": {
            **golden,
            "judge_audit_passed": judge_audit["status"] == "passed",
            "judge_audit_separated_numerator": judge_audit["separated_numerator"],
            "judge_audit_separated_denominator": judge_audit["separated_denominator"],
            "hitl_recall_percent": hitl["hitl_recall"],
            "hitl_recall_numerator": hitl["hitl_recall_numerator"],
            "hitl_recall_denominator": hitl["hitl_recall_denominator"],
            "escalation_precision_percent": hitl["dataset_escalation_precision"],
            "escalation_precision_numerator": hitl["dataset_precision_numerator"],
            "escalation_precision_denominator": hitl["dataset_precision_denominator"],
            "task_completion_percent": round(100 * passed / len(journeys), 1),
            "task_completion_numerator": passed,
            "task_completion_denominator": len(journeys),
            "incorrect_success_count": len(failures & INCORRECT_SUCCESS_IDS),
            "cross_user_isolation_failures": int("trust-13-cross-customer-request" in failures),
            "abandoned_or_confusing_workflows": len(failures & CONFUSION_RISK_IDS),
            "slo_limit_ms": load["slo_ms"],
            "users_exercised": load["virtual_users"],
            "users_affected_at_slo_limit": load["users_at_or_over_slo"],
            "load_test_failures": load["failed"],
        },
        "definitions": {
            "complete_run_cost": "Agent plus judge model cost for one live golden-answer run.",
            "quality_score": "Mean of deterministic facts/retrieval and judged correctness/grounding scores; provisional unless judge_audit_passed is true.",
            "task_completion": "Frozen trust journeys whose mapped deterministic end-to-end evidence all passed.",
            "incorrect_success": "Safety-critical journeys whose evidence failed in a way that could report success without the required outcome.",
            "abandoned_or_confusing": "Conversation/recovery journeys that failed their no-loop, no-hang, or clear-next-step evidence.",
            "users_affected_at_slo_limit": "Virtual users whose complete local session request latency was at or above the stated SLO.",
        },
        "load_test": load,
        "judge_audit": judge_audit,
        "journeys": journeys,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-report", required=True)
    parser.add_argument("--junit", required=True)
    parser.add_argument("--load-report", required=True)
    parser.add_argument("--judge-audit-report", required=True)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    report = build(
        args.golden_report, args.junit, args.load_report, args.judge_audit_report
    )
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["metrics"], indent=2))


if __name__ == "__main__":
    main()
