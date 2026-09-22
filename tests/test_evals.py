"""Deterministic retrieval-evaluation report tests."""

import json

from support_chatbot import evals


def test_run_scores_rank_and_persists_report(monkeypatch, tmp_path):
    cases = [
        {"id": "hit", "question": "q1", "expected": ["Right"], "category": "policies"},
        {"id": "miss", "question": "q2", "expected": ["Missing"], "category": "rules"},
    ]
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps(cases))
    report_file = tmp_path / "report.json"
    monkeypatch.setattr(evals, "CASES_FILE", cases_file)
    monkeypatch.setattr(evals, "REPORT_FILE", report_file)

    def fake_search(question, k, log_event):
        assert k == 3
        assert log_event is False
        if question == "q1":
            return [
                {"heading": "Other", "source": "x.md", "category": "policies", "score": .9},
                {"heading": "Right", "source": "right.md", "category": "policies", "score": .8},
            ]
        return [{"heading": "Wrong", "source": "wrong.md", "category": "policies", "score": .7}]

    monkeypatch.setattr(evals.knowledge, "search", fake_search)
    report = evals.run()

    assert report["status"] == "complete"
    assert report["summary"]["recall_at_1"] == 0.0
    assert report["summary"]["recall_at_3"] == 50.0
    assert report["summary"]["mrr"] == 0.25
    assert report["results"][0]["rank"] == 2
    assert report["results"][1]["rank"] is None
    assert json.loads(report_file.read_text())["summary"] == report["summary"]


def test_latest_returns_not_run_without_report(monkeypatch, tmp_path):
    monkeypatch.setattr(evals, "REPORT_FILE", tmp_path / "missing.json")
    assert evals.latest() == {"status": "not_run", "summary": None, "results": []}
