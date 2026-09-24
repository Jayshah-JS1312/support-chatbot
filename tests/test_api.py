"""FastAPI transport contracts with model calls replaced by local fakes."""

import pytest
from fastapi.testclient import TestClient

from support_chatbot.api.app import create_app
from support_chatbot.api.runtime import RuntimeState
from support_chatbot.persistence import get_repository
from support_chatbot.workflow import WorkflowCoordinator


class RecordingDispatcher:
    def __init__(self, repository):
        self.repository = repository
        self.calls = []

    def enqueue(self, request_id, phase):
        assert request_id in self.repository.support_requests
        self.calls.append((request_id, phase))
        return f"wfr-{request_id}"


@pytest.fixture
def api_client(tmp_path):
    repository = get_repository()
    repository.enforce_auth = True
    dispatcher = RecordingDispatcher(repository)
    runtime = RuntimeState(repository, WorkflowCoordinator(repository, dispatcher))
    with TestClient(create_app(runtime)) as client:
        login = client.post(
            "/auth/login",
            json={"email": "raj@example.com", "password": "RajDemo!2026"},
        )
        assert login.status_code == 200
        yield client, runtime


def test_health_readiness_remain_available_but_metrics_require_admin(api_client):
    client, _ = api_client

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json()["status"] == "ready"
    assert client.get("/metrics").status_code == 403
    client.post("/auth/logout")
    login = client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "AdminDemo!2026"},
    )
    assert login.status_code == 200
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "support_agent_turns_total" in metrics.text


def test_customer_page_mints_securely_scoped_session_cookie(api_client):
    client, _ = api_client
    response = client.get("/")

    assert response.status_code == 200
    assert "Ami Support" in response.text
    cookie = response.headers["set-cookie"]
    assert "sid=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    for label in ("Waiting for human review", "Approved", "Rejected", "Expired"):
        assert label in response.text
    for internal in ("Demo customers", "Agent inspector", "Observability", "Evaluations"):
        assert internal not in response.text


def test_chat_polling_preserves_reader_scroll_and_uses_ticket_notifications(api_client):
    client, _ = api_client
    page = client.get("/").text

    assert "function followsLatest()" in page
    assert "const follow=followsLatest();" in page
    assert "if(follow)scrollToLatest(true);" in page
    assert "function updateTicketNotice" in page
    assert 'id="ticketTray"' in page
    assert "workflow-card" not in page
    assert "bubble.append(body);$('chat').scrollTop=$('chat').scrollHeight" not in page


def test_state_preserves_session_across_requests(api_client):
    client, runtime = api_client
    client.get("/")

    response = client.get("/state")

    assert response.status_code == 200
    assert response.json()["stale"] is False
    assert len(runtime.sessions) == 1


def test_unknown_cookie_is_reported_as_stale_and_replaced(api_client):
    client, runtime = api_client
    client.cookies.set("sid", "session-that-no-longer-exists")

    response = client.get("/state")

    assert response.status_code == 200
    assert response.json()["stale"] is True
    assert "session-that-no-longer-exists" not in runtime.sessions
    assert response.cookies.get("sid") in runtime.sessions


def test_chat_rejects_unknown_planner_with_structured_error(api_client):
    client, _ = api_client

    response = client.post("/chat", json={"message": "hello", "planner": "magic"}, headers={"Idempotency-Key": "planner-test"})

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert body["request_id"] == response.headers["x-request-id"]
    assert body["details"][0]["location"][-1] == "planner"


def test_malformed_json_has_structured_validation_error(api_client):
    client, _ = api_client

    response = client.post(
        "/chat",
        content="{not-json",
        headers={"content-type": "application/json", "Idempotency-Key": "malformed-test"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_request_larger_than_limit_is_rejected_before_routing(api_client):
    client, _ = api_client

    response = client.post(
        "/chat",
        content="x" * 70_000,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_request_is_persisted_before_enqueue_and_returns_accepted(api_client):
    client, runtime = api_client

    response = client.post(
        "/chat",
        json={"message": "Please help me", "planner": "react"},
        headers={"Idempotency-Key": "request-before-enqueue"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "QUEUED"
    assert body["enqueue_status"] == "queued"
    assert runtime.workflow.dispatcher.calls == [(body["request_id"], "draft")]
    assert runtime.repository.support_requests[body["request_id"]]["summary"] == "Please help me"


def test_customer_can_restore_durable_request_status(api_client):
    client, runtime = api_client
    response = client.post(
        "/chat",
        json={"message": "Track my order", "planner": "react"},
        headers={"Idempotency-Key": "restore-request-status"},
    )
    request_id = response.json()["request_id"]

    restored = client.get("/requests?limit=100")

    assert restored.status_code == 200
    item = next(row for row in restored.json()["items"] if row["request_id"] == request_id)
    assert item["reference"].startswith("REQ-")
    assert item["message"] == "Track my order"
    assert item["state"] == "QUEUED"
    assert item["action"] == {
        "required": False, "name": None, "pending": False, "complete": False,
    }

    runtime.repository.support_requests[request_id].update({
        "status": "COMPLETED", "approval_status": "rejected",
        "draft_content": "This draft must not be shown as completed.",
    })
    rejected = client.get(f"/requests/{request_id}").json()
    assert rejected["state"] == "REJECTED"
    assert "No action was taken" in rejected["reply"]
    assert "must not be shown" not in rejected["reply"]


def test_reset_clears_the_current_conversation(api_client):
    client, _ = api_client
    client.get("/")

    response = client.post("/reset")

    assert response.status_code == 200
    assert response.json()["messages"] == 0
    assert response.json()["transcript"] == []


def test_feedback_is_validated_and_persisted(api_client):
    client, runtime = api_client

    invalid = client.post(
        "/feedback",
        json={"original_response": "answer", "corrected_response": "   "},
    )
    assert invalid.status_code == 422

    valid = client.post(
        "/feedback",
        json={
            "original_response": "answer",
            "corrected_response": "better answer",
            "reason": "More precise",
        },
    )
    assert valid.status_code == 200
    record = runtime.repository.feedback[-1]
    assert record["corrected_response"] == "better answer"


def test_customer_receives_forbidden_on_internal_routes(api_client):
    client, _ = api_client

    response = client.get("/monitoring")

    assert response.status_code == 403
