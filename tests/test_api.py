"""FastAPI transport contracts with model calls replaced by local fakes."""

import json

import pytest
from fastapi.testclient import TestClient

from support_chatbot.api.app import create_app
from support_chatbot.api.routes import customer
from support_chatbot.api.runtime import RuntimeState


@pytest.fixture
def api_client(tmp_path):
    runtime = RuntimeState(tmp_path / "state")
    with TestClient(create_app(runtime)) as client:
        yield client, runtime


def test_health_readiness_and_metrics_remain_available(api_client):
    client, _ = api_client

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json()["status"] == "ready"
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

    response = client.post("/chat", json={"message": "hello", "planner": "magic"})

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
        headers={"content-type": "application/json"},
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


def test_only_sanitized_agent_response_is_persisted(api_client, monkeypatch):
    client, runtime = api_client
    raw_reply = "Your new ticket is ESC-999999."
    safe_reply = "Your new ticket is [unverified]."

    def fake_planner(convo, work, **kwargs):
        del work, kwargs
        convo.add_assistant({"role": "assistant", "content": raw_reply})
        return raw_reply

    monkeypatch.setitem(customer.PLANNERS, "react", (fake_planner, "\nHOW YOU PLAN\n"))

    response = client.post(
        "/chat",
        json={"message": "Please help me", "planner": "react"},
    )

    assert response.status_code == 200
    assert response.json()["reply"] == safe_reply
    assert response.json()["transcript"][-1]["content"] == safe_reply
    assert raw_reply not in runtime.sessions_file.read_text()
    assert safe_reply in runtime.sessions_file.read_text()


def test_reset_clears_the_current_conversation(api_client, monkeypatch):
    client, _ = api_client

    def fake_planner(convo, work, **kwargs):
        del work, kwargs
        reply = "I can help with that."
        convo.add_assistant({"role": "assistant", "content": reply})
        return reply

    monkeypatch.setitem(customer.PLANNERS, "react", (fake_planner, "\nHOW YOU PLAN\n"))
    client.post("/chat", json={"message": "hello"})

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
    record = json.loads(runtime.feedback_file.read_text())
    assert record["corrected_response"] == "better answer"


def test_internal_routes_remain_hidden_by_default(api_client):
    client, _ = api_client

    response = client.get("/monitoring")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
