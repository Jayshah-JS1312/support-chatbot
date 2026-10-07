"""Durable workflow state, recovery, and duplicate-delivery contracts."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from support_chatbot.api.app import create_app
from support_chatbot.api.runtime import RuntimeState
from support_chatbot.auth import Identity, reset_identity, set_identity
from support_chatbot.persistence import IdempotencyConflictError, get_repository
from support_chatbot.workflow import EnqueueError, LocalWorkflowDispatcher, WorkflowCoordinator
from support_chatbot.config import settings
from support_chatbot.hitl_policy import RoutingDecision
from support_chatbot.memory import ConversationMemory, WorkingMemory
from support_chatbot import agent_profile, policy
from support_chatbot import workflow as workflow_module
from tests.fakes import Reply, tool_call


class Dispatcher:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def enqueue(self, request_id, phase):
        self.calls.append((request_id, phase))
        if self.fail:
            raise EnqueueError("temporarily unavailable")
        return f"msg-{len(self.calls)}"


@pytest.fixture
def workflow_setup():
    repository = get_repository()
    repository.enforce_auth = True
    dispatcher = Dispatcher()
    drafts = []

    def generate(request):
        drafts.append(request["id"])
        return "Proposed safe response", {"type": "send_resolution"}

    coordinator = WorkflowCoordinator(
        repository, dispatcher, generate,
        hitl_router=lambda proposal: RoutingDecision(True, "fixture_requires_review"),
    )
    runtime = RuntimeState(repository, coordinator)
    with TestClient(create_app(runtime)) as client:
        assert client.post("/auth/login", json={
            "email": "raj@example.com", "password": "RajDemo!2026",
        }).status_code == 200
        client.get("/")
        yield repository, dispatcher, coordinator, drafts, client


def submit(client, key="request-key-001", message="Please investigate my order"):
    return client.post(
        "/chat",
        json={"message": message, "planner": "react"},
        headers={"Idempotency-Key": key},
    )


def pending_return_request(repository, planner, reply, *, legacy=False):
    identity, auth_token = repository.login("raj@example.com", "RajDemo!2026")
    identity_token = set_identity(identity)
    sid = f"confirm-{planner}-{reply.replace(' ', '-').lower()}"
    try:
        work = WorkingMemory()
        work.customer_email = identity.email
        work.turn = 1
        policy.guarded_run(
            "start_return",
            {"order_id": "112-1111111-1111111", "reason": "reported defect"},
            work,
        )
        conversation = ConversationMemory(agent_profile.system_prompt())
        conversation.add_user("My headphones have a real defect")
        conversation.add_assistant({
            "role": "assistant",
            "content": "I can submit a return proposal. Would you like me to proceed?",
            "tool_calls": [{
                "id": "call_start_return", "type": "function",
                "function": {
                    "name": "start_return",
                    "arguments": '{"order_id":"112-1111111-1111111","reason":"reported defect"}',
                },
            }],
        })
        if legacy:
            work.pending.pop("args")
        repository.create_session(sid, work.to_dict(), identity.user_id)
        repository.save_session(sid, conversation.history, work.to_dict(), planner)
        work.turn = 2
        conversation.add_user(reply)
        repository.save_session(sid, conversation.history, work.to_dict(), planner)
        request, _ = repository.create_support_request(
            reply, planner, f"confirm-key-{planner}-{legacy}-{reply}", sid,
        )
        return request, sid, auth_token
    finally:
        reset_identity(identity_token)


def load_customer_session(repository, auth_token, sid):
    identity = repository.authenticate(auth_token)
    identity_token = set_identity(identity)
    try:
        return repository.load_session(sid)
    finally:
        reset_identity(identity_token)


def test_draft_persists_unambiguous_order_focus_for_the_next_request(fake_llm):
    repository = get_repository()
    repository.enforce_auth = True
    identity, auth_token = repository.login("raj@example.com", "RajDemo!2026")
    identity_token = set_identity(identity)
    sid = "focus-persists-after-offer"
    try:
        work = WorkingMemory()
        work.customer_email = identity.email
        work.turn = 1
        conversation = ConversationMemory(agent_profile.system_prompt())
        conversation.add_user("List my current orders")
        repository.create_session(sid, work.to_dict(), identity.user_id)
        repository.save_session(sid, conversation.history, work.to_dict(), "react")
        request, _ = repository.create_support_request(
            "List my current orders", "react", "focus-persist-key", sid,
        )
    finally:
        reset_identity(identity_token)

    fake_llm.script(
        Reply(tool_calls=[tool_call(
            "find_orders", email="raj@example.com",
            thought="List the authenticated customer's orders.",
        )]),
        Reply(content=(
            "You have Sony headphones and an Instant Pot Duo 6qt. "
            "I can track the Instant Pot if you would like."
        )),
    )
    coordinator = WorkflowCoordinator(repository, Dispatcher())
    try:
        result = coordinator.draft(request["id"])
        persisted = load_customer_session(repository, auth_token, sid)

        assert result == {"duplicate": False, "state": "COMPLETED"}
        assert persisted["work"]["active_order_id"] == "112-2222222-2222222"
    finally:
        coordinator.close()
        repository.logout(auth_token)


@pytest.mark.parametrize("planner", ["react", "plan"])
@pytest.mark.parametrize("confirmation", [
    "Yes please start a return request.",
    "Yes, please submit that return request for review.",
])
def test_one_yes_consumes_return_confirmation_and_opens_one_review(
    planner, confirmation, fake_llm,
):
    repository = get_repository()
    repository.enforce_auth = True
    request, sid, auth_token = pending_return_request(
        repository, planner, confirmation,
    )
    coordinator = WorkflowCoordinator(repository, Dispatcher())
    try:
        result = coordinator.draft(request["id"])
        row = repository.support_requests[request["id"]]
        persisted = load_customer_session(repository, auth_token, sid)

        assert result == {"duplicate": False, "state": "AWAITING_APPROVAL"}
        assert row["action_name"] == "start_return"
        assert row["action_arguments"] == {
            "order_id": "112-1111111-1111111", "reason": "reported defect",
        }
        assert row["approval_status"] == "pending"
        assert persisted["work"]["pending"] is None
        assert "submitted the return proposal" in row["draft_content"].lower()
        assert "confirm" not in row["draft_content"].lower()
        assert fake_llm.calls == []
    finally:
        coordinator.close()
        repository.logout(auth_token)


def test_no_declines_pending_return_without_human_review(fake_llm):
    repository = get_repository()
    repository.enforce_auth = True
    request, sid, auth_token = pending_return_request(repository, "react", "No thanks")
    coordinator = WorkflowCoordinator(repository, Dispatcher())
    try:
        result = coordinator.draft(request["id"])
        row = repository.support_requests[request["id"]]

        assert result == {"duplicate": False, "state": "COMPLETED"}
        assert row["approval_status"] is None
        assert load_customer_session(repository, auth_token, sid)["work"]["pending"] is None
        assert "no account action was taken" in row["draft_content"].lower()
        assert fake_llm.calls == []
    finally:
        coordinator.close()
        repository.logout(auth_token)


def test_refresh_compatible_legacy_pending_state_recovers_original_arguments(fake_llm):
    repository = get_repository()
    repository.enforce_auth = True
    request, _, auth_token = pending_return_request(
        repository, "react", "Proceed", legacy=True,
    )
    coordinator = WorkflowCoordinator(repository, Dispatcher())
    try:
        coordinator.draft(request["id"])
        row = repository.support_requests[request["id"]]

        assert row["status"] == "AWAITING_APPROVAL"
        assert row["action_arguments"]["reason"] == "reported defect"
        assert fake_llm.calls == []
    finally:
        coordinator.close()
        repository.logout(auth_token)


def test_ambiguous_reply_cannot_be_promoted_to_confirmation_by_model(fake_llm):
    repository = get_repository()
    repository.enforce_auth = True
    request, sid, auth_token = pending_return_request(
        repository, "react", "Yes, but use another order",
    )
    fake_llm.script(
        Reply(tool_calls=[tool_call(
            "start_return", order_id="112-1111111-1111111",
            reason="reported defect", confirmed=True,
            thought="The customer confirmed the earlier return.",
        )]),
        Reply(content="Which order would you like to use instead?"),
    )
    coordinator = WorkflowCoordinator(repository, Dispatcher())
    try:
        result = coordinator.draft(request["id"])
        row = repository.support_requests[request["id"]]
        persisted = load_customer_session(repository, auth_token, sid)

        assert result == {"duplicate": False, "state": "COMPLETED"}
        assert row["approval_status"] is None
        assert persisted["work"]["pending"]["key"] == [
            "start_return", "112-1111111-1111111",
        ]
        assert "which order" in row["draft_content"].lower()
        assert len(fake_llm.calls) == 2
    finally:
        coordinator.close()
        repository.logout(auth_token)


def test_confirmation_revalidates_changed_order_before_opening_review(fake_llm):
    repository = get_repository()
    repository.enforce_auth = True
    request, sid, auth_token = pending_return_request(repository, "react", "Yes")
    repository.orders["112-1111111-1111111"]["status"] = "return started"
    repository.orders["112-1111111-1111111"]["version"] += 1
    coordinator = WorkflowCoordinator(repository, Dispatcher())
    try:
        result = coordinator.draft(request["id"])
        row = repository.support_requests[request["id"]]
        persisted = load_customer_session(repository, auth_token, sid)

        assert result == {"duplicate": False, "state": "COMPLETED"}
        assert row["approval_status"] is None
        assert row["action_name"] == "send_resolution"
        assert persisted["work"]["pending"] is None
        assert "not delivered yet" in row["draft_content"].lower()
        assert fake_llm.calls == []
    finally:
        coordinator.close()
        repository.logout(auth_token)


def test_duplicate_submission_returns_one_durable_request(workflow_setup):
    repository, dispatcher, _, _, client = workflow_setup
    first = submit(client)
    second = submit(client)

    assert first.status_code == second.status_code == 202
    assert first.json()["request_id"] == second.json()["request_id"]
    assert len(repository.support_requests) == 1
    assert len(dispatcher.calls) == 1


def test_reusing_idempotency_key_with_different_payload_conflicts(workflow_setup):
    _, _, _, _, client = workflow_setup
    assert submit(client).status_code == 202
    conflict = submit(client, message="A different operation")
    assert conflict.status_code == 409


def test_duplicate_draft_delivery_creates_one_result(workflow_setup):
    repository, _, coordinator, drafts, client = workflow_setup
    request_id = submit(client).json()["request_id"]

    first = coordinator.draft(request_id)
    duplicate = coordinator.draft(request_id)

    assert first == {"duplicate": False, "state": "AWAITING_APPROVAL"}
    assert duplicate == {"duplicate": True}
    assert drafts == [request_id]
    assert repository.support_requests[request_id]["draft_content"] == "Proposed safe response"


def test_approval_resumes_later_and_duplicate_execution_is_one_action(workflow_setup):
    repository, dispatcher, coordinator, _, client = workflow_setup
    request_id = submit(client).json()["request_id"]
    coordinator.draft(request_id)
    client.post("/auth/logout")
    assert client.post("/auth/login", json={
        "email": "admin@example.com", "password": "AdminDemo!2026",
    }).status_code == 200

    decision = client.post(
        f"/admin/requests/{request_id}/decision",
        json={"decision": "approve", "reason": "Required human review",
              "review_necessary": True},
    )
    assert decision.status_code == 200
    assert decision.json()["state"] == "APPROVED"
    assert dispatcher.calls[-1] == (request_id, "execute")

    first = coordinator.execute(request_id)
    duplicate = coordinator.execute(request_id)
    assert first == {"duplicate": False, "state": "COMPLETED"}
    assert duplicate == {"duplicate": True, "state": "COMPLETED"}
    assert list(repository.action_executions) == [f"support-request:{request_id}:execute"]

    client.post("/auth/logout?role=admin")
    assert client.post("/auth/login?role=customer", json={
        "email": "raj@example.com", "password": "RajDemo!2026",
    }).status_code == 200
    customer = client.get(f"/requests/{request_id}")
    assert customer.status_code == 200
    assert customer.json()["state"] == "COMPLETED"
    assert customer.json()["reply"] == "Proposed safe response"
    assert customer.json()["action"]["complete"] is False


def test_stored_but_unenqueued_request_is_recovered(workflow_setup):
    repository, _, coordinator, _, client = workflow_setup
    failing = Dispatcher(fail=True)
    coordinator.dispatcher = failing
    response = submit(client, key="recovery-key-001")
    request_id = response.json()["request_id"]
    assert response.status_code == 202
    assert response.json()["enqueue_status"] == "pending_recovery"
    assert repository.support_requests[request_id]["status"] == "RECEIVED"

    healthy = Dispatcher()
    coordinator.dispatcher = healthy
    result = coordinator.recover()
    assert result == {"attempted": 1, "enqueued": 1, "pending": 0}
    assert repository.support_requests[request_id]["status"] == "QUEUED"


def test_local_dispatcher_processes_without_upstash():
    repository = get_repository()
    repository.enforce_auth = True
    token = set_identity(Identity(
        "11111111-1111-4111-8111-111111111111", "raj@example.com", "Raj", "customer"
    ))
    try:
        request, _ = repository.create_support_request(
            "Where is my order?", "react", "local-worker-001"
        )
    finally:
        reset_identity(token)
    coordinator = WorkflowCoordinator(
        repository, draft_generator=lambda row: ("Grounded draft", {"type": "send_resolution"})
    )
    try:
        assert isinstance(coordinator.dispatcher, LocalWorkflowDispatcher)
        assert coordinator.enqueue(request)[0] is True
        coordinator.dispatcher.messages.join()
        assert repository.support_requests[request["id"]]["status"] == "COMPLETED"
        assert repository.support_requests[request["id"]]["approval_status"] is None
    finally:
        coordinator.close()


def test_rejection_completes_without_execution(workflow_setup):
    repository, _, coordinator, _, client = workflow_setup
    request_id = submit(client, key="reject-key-001").json()["request_id"]
    coordinator.draft(request_id)
    client.post("/auth/logout")
    client.post("/auth/login", json={"email": "admin@example.com", "password": "AdminDemo!2026"})
    response = client.post(
        f"/admin/requests/{request_id}/decision",
        json={"decision": "reject", "reason": "Draft is not appropriate",
              "review_necessary": True},
    )
    assert response.json()["state"] == "COMPLETED"
    assert repository.action_executions == {}
    client.post("/auth/logout?role=admin")
    client.post("/auth/login?role=customer", json={
        "email": "raj@example.com", "password": "RajDemo!2026",
    })
    customer = client.get(f"/requests/{request_id}")
    assert customer.status_code == 200
    assert customer.json()["state"] == "REJECTED"
    assert "No action was taken" in customer.json()["reply"]
    assert "Proposed safe response" not in customer.json()["reply"]


def test_admin_inbox_edit_decision_and_audit_are_durable(workflow_setup):
    repository, dispatcher, coordinator, _, customer = workflow_setup
    request_id = submit(customer, key="inbox-edit-001").json()["request_id"]
    coordinator.draft(request_id)

    app = customer.app
    with TestClient(app) as admin:
        assert admin.post("/auth/login", json={
            "email": "admin@example.com", "password": "AdminDemo!2026",
        }).status_code == 200
        queue = admin.get("/admin/approvals.json?status=pending")
        assert queue.status_code == 200
        assert request_id in {item["request_id"] for item in queue.json()["items"]}
        detail = admin.get(f"/admin/approvals/{request_id}.json")
        assert detail.status_code == 200
        assert detail.json()["proposed_response"] == "Proposed safe response"
        assert "conversation" not in detail.json()
        decision = admin.post(f"/admin/requests/{request_id}/decision", json={
            "decision": "approve",
            "reason": "Corrected tone before sending",
            "edited_response": "Updated safe response",
            "review_necessary": False,
        })
        assert decision.status_code == 200

    row = repository.support_requests[request_id]
    assert row["draft_content"] == "Updated safe response"
    assert row["review_necessary"] is False
    assert repository.audit_events[-1]["detail"]["response_edited"] is True
    assert dispatcher.calls[-1] == (request_id, "execute")
    assert coordinator.execute(request_id)["state"] == "COMPLETED"
    customer.post("/auth/logout?role=admin")
    customer.post("/auth/login?role=customer", json={
        "email": "raj@example.com", "password": "RajDemo!2026",
    })
    visible = customer.get(f"/requests/{request_id}").json()
    assert visible["state"] == "COMPLETED"
    assert visible["reply"] == "Updated safe response"
    assert "Proposed safe response" not in visible["reply"]


def test_expired_approval_completes_without_action(workflow_setup):
    repository, _, coordinator, _, client = workflow_setup
    request_id = submit(client, key="expiry-key-001").json()["request_id"]
    coordinator.draft(request_id)
    repository.support_requests[request_id]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    expired = repository.expire_approvals()
    assert expired == [request_id]
    assert repository.support_requests[request_id]["status"] == "COMPLETED_WITHOUT_ACTION"


def test_absence_policy_reminds_escalates_and_expires_once(workflow_setup):
    repository, _, coordinator, _, client = workflow_setup
    request_id = submit(client, key="absence-lifecycle-001").json()["request_id"]
    coordinator.draft(request_id)
    row = repository.support_requests[request_id]
    baseline = datetime.now(timezone.utc)
    row["reminder_at"] = (baseline + timedelta(seconds=10)).isoformat()
    row["escalation_at"] = (baseline + timedelta(seconds=20)).isoformat()
    row["expires_at"] = (baseline + timedelta(seconds=30)).isoformat()

    reminder = repository.process_absence_policy(baseline + timedelta(seconds=11))
    assert reminder == {"reminded": [request_id], "escalated": [], "expired": []}
    assert "no action has been taken" in row["metadata"]["customer_status"]

    escalation = repository.process_absence_policy(baseline + timedelta(seconds=21))
    assert escalation == {"reminded": [], "escalated": [request_id], "expired": []}
    assert row["queue_name"] == "supervisor"
    assert row["assigned_to"] is None

    expiry = repository.process_absence_policy(baseline + timedelta(seconds=31))
    assert expiry == {"reminded": [], "escalated": [], "expired": [request_id]}
    assert row["status"] == "COMPLETED_WITHOUT_ACTION"
    assert row["approval_status"] == "expired"
    assert repository.process_absence_policy(baseline + timedelta(seconds=40)) == {
        "reminded": [], "escalated": [], "expired": [],
    }
    assert [event["event_type"] for event in repository.audit_events[-3:]] == [
        "approval_reminder_sent", "approval_escalated", "approval_expired",
    ]


def test_late_approval_is_409_and_cannot_revive_request(workflow_setup):
    repository, dispatcher, coordinator, _, client = workflow_setup
    request_id = submit(client, key="late-approval-001").json()["request_id"]
    coordinator.draft(request_id)
    repository.support_requests[request_id]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    client.post("/auth/logout")
    client.post("/auth/login", json={
        "email": "admin@example.com", "password": "AdminDemo!2026",
    })
    response = client.post(f"/admin/requests/{request_id}/decision", json={
        "decision": "approve", "reason": "Too late", "review_necessary": True,
    })
    assert response.status_code == 409
    assert repository.support_requests[request_id]["status"] == "COMPLETED_WITHOUT_ACTION"
    assert repository.support_requests[request_id]["approval_status"] == "expired"
    assert not any(call == (request_id, "execute") for call in dispatcher.calls)
    client.post("/auth/logout")
    client.post("/auth/login", json={
        "email": "raj@example.com", "password": "RajDemo!2026",
    })
    customer_status = client.get(f"/requests/{request_id}")
    assert customer_status.status_code == 200
    assert customer_status.json()["state"] == "EXPIRED"
    assert "No action was taken" in customer_status.json()["status_message"]


def test_customer_cannot_read_another_customers_request(workflow_setup):
    repository, _, _, _, client = workflow_setup
    request_id = submit(client, key="ownership-key-001").json()["request_id"]
    client.post("/auth/logout")
    client.post("/auth/login", json={"email": "mei@example.com", "password": "MeiDemo!2026"})
    assert client.get(f"/requests/{request_id}").status_code == 404


def test_repository_idempotency_is_scoped_per_customer():
    repository = get_repository()
    repository.enforce_auth = True
    raj = set_identity(Identity("11111111-1111-4111-8111-111111111111", "raj@example.com", "Raj", "customer"))
    try:
        raj_request, _ = repository.create_support_request("Help", "react", "shared-key")
        with pytest.raises(IdempotencyConflictError):
            repository.create_support_request("Different", "react", "shared-key")
    finally:
        reset_identity(raj)
    mei = set_identity(Identity("22222222-2222-4222-8222-222222222222", "mei@example.com", "Mei", "customer"))
    try:
        mei_request, _ = repository.create_support_request("Different", "react", "shared-key")
    finally:
        reset_identity(mei)
    assert raj_request["id"] != mei_request["id"]


def test_upstash_dispatch_uses_deduplication_and_retries(monkeypatch):
    captured = {}

    class Messages:
        def publish_json(self, **kwargs):
            captured.update(kwargs)
            return type("Published", (), {"message_id": "msg-123"})()

    class FakeQStash:
        def __init__(self, token):
            captured["token"] = token
            self.message = Messages()

    values = {
        "public_base_url": "https://support.example",
        "qstash_token": "test-token",
        "qstash_current_signing_key": "current-key",
        "qstash_next_signing_key": "next-key",
    }
    originals = {key: getattr(settings, key) for key in values}
    try:
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        monkeypatch.setattr(workflow_module, "QStash", FakeQStash)
        message_id = workflow_module.UpstashDispatcher().enqueue("request-1", "draft")
    finally:
        for key, value in originals.items():
            object.__setattr__(settings, key, value)

    assert message_id == "msg-123"
    assert captured["url"] == "https://support.example/workflow/requests"
    assert captured["deduplication_id"] == "request-1-draft"
    assert captured["retries"] == settings.workflow_retries
