"""Phase 11 operational metrics, audit explanation, and redaction contracts."""

from fastapi.testclient import TestClient

from support_chatbot import hitl_evals, observe
from support_chatbot.api.app import create_app
from support_chatbot.api.runtime import RuntimeState
from support_chatbot.auth import Identity, reset_identity, set_identity
from support_chatbot.config import settings
from support_chatbot.operational import public_event, safe_audit_detail, sanitized_jsonl
from support_chatbot.persistence import get_repository
from support_chatbot.workflow import WorkflowCoordinator


RAJ = Identity("11111111-1111-4111-8111-111111111111", "raj@example.com", "Raj", "customer")


class Dispatcher:
    def enqueue(self, request_id, phase):
        return f"{phase}-{request_id}"


def request(repository, key="operations-001"):
    token = set_identity(RAJ)
    try:
        return repository.create_support_request(
            "My email is raj@example.com and card is 4111 1111 1111 1111",
            "react", key,
        )[0]
    finally:
        reset_identity(token)


def test_event_redaction_is_server_side_and_allowlisted():
    event = {
        "seq": 1, "ts": 1, "kind": "tool", "session": "abcd", "turn": "t1",
        "tool": "find_orders", "ok": True,
        "args": {"email": "private@example.com", "token": "secret-token"},
        "message": "card 4111 1111 1111 1111",
        "error": "api_key=super-secret for private@example.com",
    }
    safe = public_event(event)
    encoded = sanitized_jsonl([event])
    assert "args" not in safe and "message" not in safe
    assert "private@example.com" not in encoded
    assert "super-secret" not in encoded
    assert "4111" not in encoded
    assert "[secret redacted]" in encoded


def test_audit_details_drop_reviewer_notes_and_action_arguments():
    safe = safe_audit_detail({
        "action": "cancel_order", "routing_reason": "privileged",
        "reason": "Customer private details", "arguments": {"order_id": "secret"},
        "response_edited": True,
    })
    assert safe == {
        "action": "cancel_order", "routing_reason": "privileged",
        "response_edited": True,
    }


def test_operational_metrics_and_timeline_explain_safe_request():
    repository = get_repository()
    workflow = WorkflowCoordinator(
        repository, Dispatcher(), lambda _: ("Safe answer", {"type": "send_resolution"})
    )
    row = request(repository)
    try:
        workflow.enqueue(row)
        workflow.draft(row["id"])
        repository.save_hitl_evaluation(hitl_evals.run())
        metrics = repository.operational_metrics()
        assert metrics["queue_depth"] == 0
        assert metrics["retry_count"] == 0
        assert metrics["drafting_p50_ms"] is not None
        assert metrics["resolution_p50_ms"] is not None
        assert metrics["hitl_recall"] == 100.0
        assert metrics["decision_breakdown"] == {}
        item = repository.operational_requests()[0]
        assert item["status"] == "COMPLETED"
        assert "customer" not in str(item).lower()
        timeline = repository.operational_request_timeline(row["id"])
        assert timeline["state_explanation"]
        assert "resolution_completed_without_review" in {
            event["event"] for event in timeline["timeline"]
        }
        assert "request_received" in {event["event"] for event in timeline["timeline"]}
        assert "raj@example.com" not in str(timeline)
        assert "4111" not in str(timeline)
    finally:
        workflow.close()


def test_monitoring_api_requires_admin_and_returns_redacted_operations():
    original = settings.expose_internal_ui
    object.__setattr__(settings, "expose_internal_ui", True)
    repository = get_repository()
    repository.enforce_auth = True
    observe.log("tool", tool="find_orders", ok=False,
                args={"email": "private@example.com", "password": "bad"})
    try:
        with TestClient(create_app(RuntimeState(repository))) as client:
            assert client.get("/logs.json").status_code == 401
            client.post("/auth/login", json={
                "email": "raj@example.com", "password": "RajDemo!2026",
            })
            assert client.get("/logs.json").status_code == 403
            client.post("/auth/logout")
            client.post("/auth/login", json={
                "email": "admin@example.com", "password": "AdminDemo!2026",
            })
            response = client.get("/logs.json")
            assert response.status_code == 200
            payload = response.json()
            assert {"operations", "requests", "events", "stats", "runtime"} <= payload.keys()
            assert "private@example.com" not in response.text
            assert '"args"' not in response.text
            trace = client.get("/trace.jsonl")
            assert trace.status_code == 200
            assert "private@example.com" not in trace.text
    finally:
        object.__setattr__(settings, "expose_internal_ui", original)


def test_request_timeline_endpoint_is_admin_only_and_non_disclosing():
    original = settings.expose_internal_ui
    object.__setattr__(settings, "expose_internal_ui", True)
    repository = get_repository()
    repository.enforce_auth = True
    row = request(repository, "timeline-admin-001")
    try:
        with TestClient(create_app(RuntimeState(repository))) as client:
            client.post("/auth/login", json={
                "email": "raj@example.com", "password": "RajDemo!2026",
            })
            endpoint = f"/admin/operations/requests/{row['id']}.json"
            assert client.get(endpoint).status_code == 403
            client.post("/auth/logout")
            client.post("/auth/login", json={
                "email": "admin@example.com", "password": "AdminDemo!2026",
            })
            response = client.get(endpoint)
            assert response.status_code == 200
            assert "raj@example.com" not in response.text
            assert "4111" not in response.text
            missing = client.get(
                "/admin/operations/requests/00000000-0000-4000-8000-000000000000.json"
            )
            assert missing.status_code == 404
    finally:
        object.__setattr__(settings, "expose_internal_ui", original)
