"""Deterministic tests for the HITL release gate and operations metrics."""

from fastapi.testclient import TestClient

from support_chatbot import hitl_evals
from support_chatbot.api.app import create_app
from support_chatbot.api.runtime import RuntimeState
from support_chatbot.config import settings
from support_chatbot.persistence import get_repository


REQUIRED_CATEGORIES = {
    "cancellation", "return", "refund_override", "address_change",
    "privacy_deletion", "safe_tracking", "knowledge_question",
    "prompt_injection", "false_authority", "cross_user_access",
}


def login(client, email="admin@example.com", password="AdminDemo!2026"):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_labelled_dataset_has_required_schema_and_threat_categories():
    cases = hitl_evals.load_cases()
    assert REQUIRED_CATEGORIES <= {case["category"] for case in cases}
    assert all(case["input"] and case["expected_action"] for case in cases)
    assert all(case["expected_outcome"] and case["expected_policy_reason"] for case in cases)


def test_review_all_policy_has_perfect_recall_and_truthfully_low_precision():
    report = hitl_evals.run()
    summary = report["summary"]
    assert summary["hitl_recall"] == 100.0
    assert summary["release_allowed"] is True
    assert summary["dataset_escalation_precision"] < 100.0
    assert summary["false_positives"] > 0
    assert summary["false_negatives"] == 0


def test_release_gate_fails_on_one_missed_required_handoff():
    first_required = next(case["id"] for case in hitl_evals.load_cases()
                          if case["expected_requires_hitl"])
    report = hitl_evals.run(router=lambda case: case["id"] != first_required)
    assert report["summary"]["hitl_recall"] < 100.0
    assert report["summary"]["false_negatives"] == 1
    assert report["summary"]["release_allowed"] is False


def test_operational_precision_uses_only_decided_reviewer_labels(fresh_store):
    fresh_store.support_requests.update({
        "necessary": {"approval_status": "approved", "review_necessary": True},
        "unnecessary": {"approval_status": "rejected", "review_necessary": False},
        "pending": {"approval_status": "pending", "review_necessary": None},
        "expired": {"approval_status": "expired", "review_necessary": None},
    })
    metrics = fresh_store.hitl_operational_metrics()
    assert metrics == {
        "decided": 2, "marked_necessary": 1, "unlabeled_decisions": 0,
        "pending": 1, "expired": 1, "escalation_precision": 50.0,
    }


def test_hitl_dashboard_is_admin_only_and_persists_latest_run():
    original = settings.expose_internal_ui
    object.__setattr__(settings, "expose_internal_ui", True)
    repository = get_repository()
    repository.enforce_auth = True
    try:
        with TestClient(create_app(RuntimeState(repository))) as client:
            assert client.get("/admin/hitl-evals").status_code == 401
            assert login(client, "raj@example.com", "RajDemo!2026").status_code == 200
            assert client.get("/admin/hitl-evals").status_code == 403
            client.post("/auth/logout")
            assert login(client).status_code == 200
            assert client.get("/admin/hitl-evals").status_code == 200
            result = client.post("/admin/hitl-evals/run", json={})
            assert result.status_code == 200
            assert result.json()["report"]["summary"]["release_allowed"] is True
            latest = client.get("/admin/hitl-evals.json").json()
            assert latest["report"]["run_id"] == result.json()["report"]["run_id"]
    finally:
        object.__setattr__(settings, "expose_internal_ui", original)
