"""FastAPI transport contracts with model calls replaced by local fakes."""

import pytest
from fastapi.testclient import TestClient

from support_chatbot import dashboard
from support_chatbot.api.app import create_app
from support_chatbot.api.runtime import RuntimeState
from support_chatbot.persistence import get_repository
from support_chatbot.workflow import WorkflowCoordinator
from support_chatbot.auth import Identity, reset_identity, set_identity


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
    assert metrics.headers["content-type"].startswith("text/plain")
    browser_metrics = client.get(
        "/metrics", headers={"Accept": "text/html"}, follow_redirects=False,
    )
    assert browser_metrics.status_code == 303
    assert browser_metrics.headers["location"] == "/monitoring"
    assert 'href="/metrics"' not in dashboard.PAGE
    assert "Operations overview" in dashboard.PAGE


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


def test_chat_polling_preserves_reader_scroll_and_keeps_ticket_status_out_of_chat(api_client):
    client, _ = api_client
    page = client.get("/").text

    assert "function followsLatest()" in page
    assert "const follow=followsLatest();" in page
    assert "if(follow)scrollToLatest(true);" in page
    assert "function refreshTicketLink" in page
    assert 'id="ticketLinkStatus"' in page
    assert 'id="ticketTray"' not in page
    assert 'id="activity"' not in page
    assert "function updateTicketNotice" not in page
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


def test_second_message_is_rejected_until_current_turn_finishes(api_client):
    client, _ = api_client
    client.get("/")
    first = client.post(
        "/chat", json={"message": "First question", "planner": "react"},
        headers={"Idempotency-Key": "one-turn-at-a-time-1"},
    )
    assert first.status_code == 202
    second = client.post(
        "/chat", json={"message": "Second question", "planner": "react"},
        headers={"Idempotency-Key": "one-turn-at-a-time-2"},
    )
    assert second.status_code == 409
    assert "finish the current response" in second.json()["error"]["message"]


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


def test_new_conversation_preserves_old_thread_and_can_restore_it(api_client):
    client, _ = api_client
    client.get("/")
    old_id = client.get("/state").json()["conversation_id"]
    client.post(
        "/chat", json={"message": "Keep this old chat", "planner": "react"},
        headers={"Idempotency-Key": "preserve-old-thread"},
    )

    created = client.post("/conversations")
    assert created.status_code == 201
    assert created.json()["conversation_id"] != old_id
    history = client.get("/conversations").json()["items"]
    assert any(item["conversation_id"] == old_id and
               item["title"] == "Keep this old chat" for item in history)

    restored = client.post(f"/conversations/{old_id}/activate")
    assert restored.status_code == 200
    assert restored.json()["transcript"][0]["content"] == "Keep this old chat"


def test_customer_can_rename_and_delete_completed_conversation(api_client):
    client, runtime = api_client
    client.get("/")
    sid = client.get("/state").json()["conversation_id"]
    created = client.post(
        "/chat", json={"message": "A chat to manage", "planner": "react"},
        headers={"Idempotency-Key": "manage-chat-thread"},
    ).json()
    runtime.repository.support_requests[created["request_id"]]["status"] = "COMPLETED"
    runtime.repository.memories["raj@example.com"] = {
        "sessions": [sid], "orders_discussed": [], "actions": [],
        "escalations": [], "refusals": 0,
    }

    renamed = client.patch(f"/conversations/{sid}", json={"title": "Shipping help"})
    assert renamed.status_code == 200
    assert client.get("/conversations").json()["items"][0]["title"] == "Shipping help"

    deleted = client.delete(f"/conversations/{sid}")
    assert deleted.status_code == 200
    assert deleted.json()["conversation_id"] != sid
    assert runtime.repository.support_requests[created["request_id"]]["conversation_id"] is None
    assert "raj@example.com" not in runtime.repository.memories
    assert client.get("/conversations").json()["items"] == []


def test_active_conversation_cannot_be_deleted(api_client):
    client, _ = api_client
    client.get("/")
    sid = client.get("/state").json()["conversation_id"]
    client.post(
        "/chat", json={"message": "Still processing", "planner": "react"},
        headers={"Idempotency-Key": "active-delete-guard"},
    )
    response = client.delete(f"/conversations/{sid}")
    assert response.status_code == 409


def test_dead_letter_closes_request_instead_of_leaving_spinner(api_client):
    client, runtime = api_client
    client.get("/")
    created = client.post(
        "/chat", json={"message": "Ambiguous request", "planner": "react"},
        headers={"Idempotency-Key": "dead-letter-terminal"},
    ).json()
    runtime.repository.record_dead_letter(
        created["request_id"], "local-workflow", 500, "database failure", {},
    )

    status = client.get(f"/requests/{created['request_id']}").json()
    assert status["state"] == "COMPLETED_WITHOUT_ACTION"
    assert "No account action was taken" in status["reply"]


def test_customer_logout_does_not_inherit_admin_cookie(api_client):
    client, _ = api_client
    admin = client.post(
        "/auth/login?role=admin",
        json={"email": "admin@example.com", "password": "AdminDemo!2026"},
    )
    assert admin.status_code == 200
    logged_out = client.post("/auth/logout?role=customer")
    assert logged_out.status_code == 200

    login_page = client.get("/login?role=customer")
    assert "url=/admin/approvals" not in login_page.text
    assert "Sign in" in login_page.text
    assert client.get("/admin/approvals").status_code == 200


def test_admin_cookie_never_authenticates_customer_routes(api_client):
    client, _ = api_client
    client.post(
        "/auth/login?role=admin",
        json={"email": "admin@example.com", "password": "AdminDemo!2026"},
    )
    client.post("/auth/logout?role=customer")

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_support_ticket_lifecycle_is_customer_visible_and_transition_safe(api_client):
    client, runtime = api_client
    token = set_identity(Identity(
        "11111111-1111-4111-8111-111111111111",
        "raj@example.com", "Raj", "customer",
    ))
    try:
        reference = runtime.repository.create_escalation("Customer requested a person")
    finally:
        reset_identity(token)

    customer = client.get("/tickets.json")
    assert customer.status_code == 200
    ticket = customer.json()["items"][0]
    assert ticket["reference_number"] == reference
    assert ticket["status"] == "open"

    client.post(
        "/auth/login?role=admin",
        json={"email": "admin@example.com", "password": "AdminDemo!2026"},
    )
    admin = client.get("/admin/tickets.json?status=open")
    assert admin.status_code == 200
    assert admin.json()["items"][0]["reference_number"] == reference

    empty_resolution = client.patch(
        f"/admin/tickets/{ticket['id']}",
        json={"status": "resolved", "resolution": "   "},
    )
    assert empty_resolution.status_code == 422
    assert "customer will see" in empty_resolution.json()["error"]["message"]

    started = client.patch(
        f"/admin/tickets/{ticket['id']}",
        json={"status": "in_progress", "resolution": "private draft must not leak"},
    )
    assert started.status_code == 200
    assert started.json()["status"] == "in_progress"
    assert started.json()["resolution"] is None

    customer_in_progress = client.get("/tickets.json?role=customer").json()["items"][0]
    assert customer_in_progress["status"] == "in_progress"
    assert customer_in_progress["resolution"] is None

    premature_close = client.patch(
        f"/admin/tickets/{ticket['id']}",
        json={"status": "closed", "resolution": "Should not close yet"},
    )
    assert premature_close.status_code == 409

    resolved = client.patch(
        f"/admin/tickets/{ticket['id']}",
        json={"status": "resolved", "resolution": "We contacted the customer."},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"

    customer_after = client.get("/tickets.json?role=customer")
    assert customer_after.status_code == 200
    assert customer_after.json()["items"][0]["resolution"] == "We contacted the customer."
    assert customer_after.json()["items"][0]["status"] == "resolved"

    closed = client.patch(
        f"/admin/tickets/{ticket['id']}",
        json={"status": "closed", "resolution": "We contacted the customer."},
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    customer_closed = client.get("/tickets.json?role=customer").json()["items"][0]
    assert customer_closed["status"] == "closed"
    assert customer_closed["resolution"] == "We contacted the customer."

    cannot_reopen = client.patch(
        f"/admin/tickets/{ticket['id']}",
        json={"status": "in_progress", "resolution": ""},
    )
    assert cannot_reopen.status_code == 409

    client.post("/auth/logout?role=customer")
    client.post(
        "/auth/login?role=customer",
        json={"email": "mei@example.com", "password": "MeiDemo!2026"},
    )
    assert client.get("/tickets.json").json()["items"] == []


def test_ticket_pages_protect_typing_and_explain_customer_outcomes(api_client):
    client, _ = api_client
    customer_page = client.get("/tickets")
    assert customer_page.status_code == 200
    for copy in (
        "Waiting for a support specialist", "currently working",
        "has responded", "no further work is pending",
    ):
        assert copy in customer_page.text

    client.post(
        "/auth/login?role=admin",
        json={"email": "admin@example.com", "password": "AdminDemo!2026"},
    )
    admin_page = client.get("/admin/tickets")
    assert admin_page.status_code == 200
    for contract in (
        "Response visible to customer", "Draft text stays private",
        "function editing()", "drafts=new Map()",
        "if(loading||(!force&&editing()))return",
        "Send response & resolve",
    ):
        assert contract in admin_page.text
    assert "internal decision note" not in admin_page.text


def test_customer_page_has_identity_quick_actions_history_and_ticket_status_link(api_client):
    client, _ = api_client
    page = client.get("/").text
    for text in ("Quick actions", "Track a package", "Cancel an order",
                 "Return an order", "Your conversations", "welcomeName"):
        assert text in page
    assert "refreshTicketLink" in page
    assert "document.title=`${copy[0]} · Ami Support`" in page
    assert "waitForTurn" in page
    assert "renderConversation" in page
    assert "renameConversation" in page
    assert "deleteConversation" in page
    assert "Support tickets" in page


def test_empty_conversations_are_not_listed(api_client):
    client, _ = api_client
    client.get("/")
    assert client.get("/conversations").json()["items"] == []


def test_requests_can_be_filtered_to_the_open_conversation(api_client):
    client, _ = api_client
    client.get("/")
    first_sid = client.get("/state").json()["conversation_id"]
    first = client.post(
        "/chat", json={"message": "Conversation one", "planner": "react"},
        headers={"Idempotency-Key": "conversation-filter-one"},
    ).json()
    second_sid = client.post("/conversations").json()["conversation_id"]
    second = client.post(
        "/chat", json={"message": "Conversation two", "planner": "react"},
        headers={"Idempotency-Key": "conversation-filter-two"},
    ).json()

    first_items = client.get(f"/requests?conversation_id={first_sid}").json()["items"]
    second_items = client.get(f"/requests?conversation_id={second_sid}").json()["items"]
    assert [item["request_id"] for item in first_items] == [first["request_id"]]
    assert [item["request_id"] for item in second_items] == [second["request_id"]]


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
