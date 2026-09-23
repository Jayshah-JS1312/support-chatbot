"""Two-party authorization and sealed privileged-action tests."""

import pytest
from fastapi.testclient import TestClient

from support_chatbot.api.app import create_app
from support_chatbot.api.runtime import RuntimeState
from support_chatbot.persistence import get_repository
from support_chatbot.workflow import WorkflowCoordinator


class Dispatcher:
    def __init__(self): self.calls = []
    def enqueue(self, request_id, phase):
        self.calls.append((request_id, phase)); return f"msg-{len(self.calls)}"


@pytest.fixture
def action_app():
    repository = get_repository(); repository.enforce_auth = True
    dispatcher = Dispatcher()
    coordinator = WorkflowCoordinator(repository, dispatcher, lambda request: ("draft", {"type": "send_resolution"}))
    with TestClient(create_app(RuntimeState(repository, coordinator))) as client:
        client.post("/auth/login", json={"email": "mei@example.com", "password": "MeiDemo!2026"})
        yield client, repository, dispatcher, coordinator


def preview_cancel(client):
    response = client.post("/actions/preview", json={
        "action": "cancel_order", "order_id": "112-3333333-3333333",
    })
    assert response.status_code == 200
    return response.json()


def confirm(client, proposal, key="cancel-confirm-001"):
    return client.post(
        f"/actions/proposals/{proposal['proposal_id']}/confirm",
        json={"action_hash": proposal["action_hash"], "idempotency_key": key},
    )


def become_admin(client):
    client.post("/auth/logout")
    assert client.post("/auth/login", json={
        "email": "admin@example.com", "password": "AdminDemo!2026",
    }).status_code == 200


def test_customer_confirmation_creates_sealed_draft_but_does_not_execute(action_app):
    client, repository, _, _ = action_app
    proposal = preview_cancel(client)
    assert proposal["arguments"] == {"order_id": "112-3333333-3333333"}
    assert proposal["consequences"]["refund_amount"] == 149.99
    assert proposal["policy_evidence"]["observed_status"] == "preparing"
    assert len(proposal["action_hash"]) == 64
    assert repository.orders["112-3333333-3333333"]["status"] == "preparing"

    response = confirm(client, proposal)
    assert response.status_code == 202
    request = repository.support_requests[response.json()["request_id"]]
    assert request["status"] == "AWAITING_APPROVAL"
    assert request["customer_confirmed_at"]
    assert request["action_hash"] == proposal["action_hash"]
    assert repository.orders["112-3333333-3333333"]["status"] == "preparing"


def test_wrong_hash_cannot_confirm_a_different_action(action_app):
    client, repository, _, _ = action_app
    proposal = preview_cancel(client)
    response = client.post(
        f"/actions/proposals/{proposal['proposal_id']}/confirm",
        json={"action_hash": "0" * 64, "idempotency_key": "wrong-hash-001"},
    )
    assert response.status_code == 409
    assert repository.support_requests == {}


def test_approved_action_executes_once(action_app):
    client, repository, dispatcher, coordinator = action_app
    proposal = preview_cancel(client)
    request_id = confirm(client, proposal).json()["request_id"]
    become_admin(client)
    approved = client.post(f"/admin/requests/{request_id}/decision", json={
        "decision": "approve", "reason": "Preview and policy evidence verified",
    })
    assert approved.status_code == 200
    assert dispatcher.calls[-1] == (request_id, "execute")
    assert repository.orders["112-3333333-3333333"]["status"] == "preparing"

    assert coordinator.execute(request_id)["state"] == "COMPLETED"
    assert coordinator.execute(request_id) == {"duplicate": True, "state": "COMPLETED"}
    assert repository.orders["112-3333333-3333333"]["status"] == "cancelled"
    assert repository.orders["112-3333333-3333333"]["version"] == 2
    assert len(repository.action_executions) == 1


def test_stale_approval_fails_without_action(action_app):
    client, repository, _, coordinator = action_app
    proposal = preview_cancel(client)
    request_id = confirm(client, proposal, "stale-action-001").json()["request_id"]
    repository.orders["112-3333333-3333333"]["version"] += 1
    become_admin(client)
    client.post(f"/admin/requests/{request_id}/decision", json={"decision": "approve"})

    result = coordinator.execute(request_id)
    assert result["state"] == "COMPLETED_WITHOUT_ACTION"
    assert repository.orders["112-3333333-3333333"]["status"] == "preparing"
    assert repository.action_executions[f"support-request:{request_id}:execute"]["status"] == "failed"


def test_arguments_edited_after_approval_are_rejected(action_app):
    client, repository, _, coordinator = action_app
    proposal = preview_cancel(client)
    request_id = confirm(client, proposal, "tamper-action-001").json()["request_id"]
    become_admin(client)
    client.post(f"/admin/requests/{request_id}/decision", json={"decision": "approve"})
    repository.support_requests[request_id]["action_arguments"]["order_id"] = "112-1111111-1111111"

    result = coordinator.execute(request_id)
    assert result["state"] == "COMPLETED_WITHOUT_ACTION"
    assert repository.support_requests[request_id]["last_error"] == "action_seal_mismatch"
    assert repository.orders["112-3333333-3333333"]["status"] == "preparing"
    assert repository.orders["112-1111111-1111111"]["status"] == "delivered"
