"""Task 3 quantitative baseline aggregation contracts."""

import json
import xml.etree.ElementTree as ET

from evaluations import golden, quantitative_baseline


def test_percentile_uses_the_nearest_rank_definition():
    values = list(range(1, 29))

    assert quantitative_baseline.percentile(values, 0.50) == 14
    assert quantitative_baseline.percentile(values, 0.95) == 27


def test_judge_audit_persists_provider_failure_as_failed_control(monkeypatch):
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(args)
        raise TimeoutError("provider unavailable")
    monkeypatch.setattr(golden, "judge", unavailable)
    rows = [
        {"id": "one", "reference": "First answer", "turns": ["first"], "tags": ["a"]},
        {"id": "two", "reference": "Second answer", "turns": ["second"], "tags": ["b"]},
    ]

    report = golden.audit(rows, "judge-model")

    assert report["status"] == "failed"
    assert report["separated_numerator"] == 0
    assert report["results"][0]["error"] == "TimeoutError"
    assert report["results"][1]["error"] == "not run after provider failure"
    assert len(calls) == 1


def test_baseline_aggregates_cost_quality_safety_and_slo(tmp_path, monkeypatch):
    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps([{
        "row": "sample", "runs": 1, "passed": 1, "planner": "react",
        "agent_model": "agent-model", "judge_model": "judge-model",
        "outcomes": [{
            "fails": [], "score": 0.9, "llm_calls": 2, "judge_calls": 1,
            "tokens": 100, "judge_tokens": 25, "cost": 0.01,
            "judge_cost": 0.002, "ms": 800,
        }],
    }]))
    load = tmp_path / "load.json"
    load.write_text(json.dumps({
        "slo_ms": 2000, "virtual_users": 1000,
        "users_at_or_over_slo": 4, "failed": 1,
    }))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({
        "status": "passed", "separated_numerator": 1,
        "separated_denominator": 1,
    }))

    mapping = json.loads(quantitative_baseline.EVIDENCE.read_text())
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for name in sorted({name for names in mapping.values() for name in names}):
        ET.SubElement(suite, "testcase", name=name)
    junit = tmp_path / "junit.xml"
    ET.ElementTree(root).write(junit)

    monkeypatch.setattr(quantitative_baseline.hitl_evals, "run", lambda: {
        "summary": {
            "hitl_recall": 100.0,
            "hitl_recall_numerator": 10,
            "hitl_recall_denominator": 10,
            "dataset_escalation_precision": 90.0,
            "dataset_precision_numerator": 9,
            "dataset_precision_denominator": 10,
        }
    })

    report = quantitative_baseline.build(
        golden, junit, load, audit, commit="abc123"
    )
    metrics = report["metrics"]

    assert metrics["complete_run_cost_usd"] == 0.012
    assert metrics["total_model_calls"] == 3
    assert metrics["total_tokens"] == 125
    assert metrics["quality_score_percent"] == 90.0
    assert metrics["task_completion_percent"] == 100.0
    assert metrics["incorrect_success_count"] == 0
    assert metrics["cross_user_isolation_failures"] == 0
    assert metrics["abandoned_or_confusing_workflows"] == 0
    assert metrics["users_affected_at_slo_limit"] == 4
    assert metrics["judge_audit_passed"] is True
    assert report["run_configuration"]["planners"] == ["react"]
    assert report["run_configuration"]["agent_models"] == ["agent-model"]
    assert report["run_configuration"]["judge_models"] == ["judge-model"]


def test_failed_cross_user_evidence_is_visible(tmp_path, monkeypatch):
    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps([{
        "row": "sample", "outcomes": [{
            "fails": [], "score": 1.0, "llm_calls": 0, "judge_calls": 0,
            "tokens": 0, "judge_tokens": 0, "cost": 0, "judge_cost": 0,
            "ms": 1,
        }],
    }]))
    load = tmp_path / "load.json"
    load.write_text(json.dumps({
        "slo_ms": 2000, "virtual_users": 1,
        "users_at_or_over_slo": 0, "failed": 0,
    }))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({
        "status": "failed", "separated_numerator": 0,
        "separated_denominator": 1,
    }))
    mapping = json.loads(quantitative_baseline.EVIDENCE.read_text())
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    target = "test_raj_cannot_retrieve_meis_order_by_changing_the_id"
    for name in sorted({name for names in mapping.values() for name in names}):
        case = ET.SubElement(suite, "testcase", name=name)
        if name == target:
            ET.SubElement(case, "failure")
    junit = tmp_path / "junit.xml"
    ET.ElementTree(root).write(junit)
    monkeypatch.setattr(quantitative_baseline.hitl_evals, "run", lambda: {
        "summary": {
            "hitl_recall": 100.0, "hitl_recall_numerator": 1,
            "hitl_recall_denominator": 1,
            "dataset_escalation_precision": 100.0,
            "dataset_precision_numerator": 1,
            "dataset_precision_denominator": 1,
        }
    })

    report = quantitative_baseline.build(
        golden, junit, load, audit, commit="abc123"
    )

    assert report["metrics"]["cross_user_isolation_failures"] == 1
    assert report["metrics"]["task_completion_numerator"] == 19
