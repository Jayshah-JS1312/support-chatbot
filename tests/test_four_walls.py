"""Phase 10: deterministic contracts for latency, absence, regression, and isolation."""

import concurrent.futures
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from support_chatbot import hitl_evals
from support_chatbot.api.app import create_app
from support_chatbot.api.runtime import RuntimeState
from support_chatbot.auth import Identity, reset_identity, set_identity
from support_chatbot.hitl_policy import RoutingDecision
from support_chatbot.persistence import ActionProposalError, get_repository
from support_chatbot.workflow import EnqueueError, WorkflowCoordinator


RAJ = Identity("11111111-1111-4111-8111-111111111111", "raj@example.com", "Raj", "customer")


class Dispatcher:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def enqueue(self, request_id, phase):
        self.calls.append((request_id, phase))
        if self.fail:
            raise EnqueueError("provider unavailable")
        return f"queued-{len(self.calls)}"


def create_request(repository, key, summary="Where is my order?"):
    token = set_identity(RAJ)
    try:
        return repository.create_support_request(summary, "react", key)[0]
    finally:
        reset_identity(token)


def test_slow_llm_does_not_delay_request_acceptance():
    repository = get_repository()
    dispatcher = Dispatcher()

    def slow_draft(_request):
        time.sleep(0.2)
        return "done", {"type": "send_resolution"}

    coordinator = WorkflowCoordinator(repository, dispatcher, slow_draft)
    request = create_request(repository, "slow-llm-001")
    started = time.perf_counter()
    assert coordinator.enqueue(request)[0] is True
    assert time.perf_counter() - started < 0.1
    assert request["id"] not in coordinator.repository.action_executions
    coordinator.close()


def test_cold_start_recovery_enqueues_stored_request():
    repository = get_repository()
    request = create_request(repository, "cold-start-001")
    dispatcher = Dispatcher()
    coordinator = WorkflowCoordinator(repository, dispatcher)
    try:
        assert coordinator.recover() == {"attempted": 1, "enqueued": 1, "pending": 0}
        assert dispatcher.calls == [(request["id"], "draft")]
    finally:
        coordinator.close()


def test_queue_delay_preserves_request_without_claiming_success():
    repository = get_repository()
    request = create_request(repository, "queue-delay-001")
    coordinator = WorkflowCoordinator(repository, Dispatcher())
    try:
        coordinator.enqueue(request)
        stored = repository.support_requests[request["id"]]
        assert stored["status"] == "QUEUED"
        assert stored["draft_content"] is None
        assert stored["approval_status"] is None
    finally:
        coordinator.close()


def test_provider_timeout_is_dead_lettered_without_action():
    repository = get_repository()
    request = create_request(repository, "provider-timeout-001")
    coordinator = WorkflowCoordinator(
        repository, Dispatcher(),
        draft_generator=lambda _: (_ for _ in ()).throw(TimeoutError("model timed out")),
    )
    try:
        repository.mark_enqueued(request["id"], "provider-timeout-run")
        with pytest.raises(TimeoutError):
            coordinator.draft(request["id"])
        coordinator.record_failure(
            request["id"], "provider-timeout-run", 504, "provider_timeout", {}
        )
        assert repository.dead_letters[-1]["status"] == 504
        assert repository.action_executions == {}
    finally:
        coordinator.close()


def test_concurrent_duplicate_deliveries_produce_one_draft():
    repository = get_repository()
    request = create_request(repository, "concurrent-delivery-001")
    repository.mark_enqueued(request["id"], "concurrent-run")
    coordinator = WorkflowCoordinator(
        repository, Dispatcher(), lambda _: ("safe answer", {"type": "send_resolution"})
    )
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: coordinator.draft(request["id"]), range(8)))
        assert sum(not outcome["duplicate"] for outcome in outcomes) == 1
        assert repository.support_requests[request["id"]]["status"] == "COMPLETED"
        assert repository.action_executions == {}
    finally:
        coordinator.close()


def test_enqueue_retry_exhaustion_fails_closed_and_dead_letters():
    repository = get_repository()
    request = create_request(repository, "retry-exhaustion-001")
    coordinator = WorkflowCoordinator(repository, Dispatcher(fail=True))
    try:
        for _ in range(5):
            coordinator.enqueue(repository.support_requests[request["id"]])
        stored = repository.support_requests[request["id"]]
        assert stored["status"] == "COMPLETED_WITHOUT_ACTION"
        assert stored["enqueue_attempts"] == 5
        assert len(repository.dead_letters) == 1
        assert repository.action_executions == {}
    finally:
        coordinator.close()


def pending_review(repository, key):
    request = create_request(repository, key, "A privileged request")
    repository.mark_enqueued(request["id"], f"run-{key}")
    coordinator = WorkflowCoordinator(
        repository, Dispatcher(), lambda _: ("review this", {"type": "refund_override"}),
        hitl_router=lambda _: RoutingDecision(True, "privileged_or_irreversible_action"),
    )
    coordinator.draft(request["id"])
    return request["id"], coordinator


def test_no_reviewer_and_unavailable_supervisor_end_in_expiry():
    repository = get_repository()
    repository.profiles.pop("admin@example.com")
    request_id, coordinator = pending_review(repository, "no-reviewer-001")
    row = repository.support_requests[request_id]
    now = datetime.now(timezone.utc)
    row["reminder_at"] = (now - timedelta(seconds=3)).isoformat()
    row["escalation_at"] = (now - timedelta(seconds=2)).isoformat()
    row["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    try:
        result = repository.process_absence_policy(now)
        assert result["expired"] == [request_id]
        assert row["status"] == "COMPLETED_WITHOUT_ACTION"
        assert repository.action_executions == {}
    finally:
        coordinator.close()


def test_disabled_reviewer_cannot_be_assigned():
    repository = get_repository()
    request_id, coordinator = pending_review(repository, "disabled-reviewer-001")
    disabled_id = repository.profiles.pop("admin@example.com")["id"]
    try:
        with pytest.raises(ActionProposalError):
            repository.reassign_approval(request_id, disabled_id, "try", RAJ.user_id)
    finally:
        coordinator.close()


def test_failed_reminder_attempt_does_not_authorize_or_execute():
    repository = get_repository()
    request_id, coordinator = pending_review(repository, "notification-failure-001")
    row = repository.support_requests[request_id]
    now = datetime.now(timezone.utc)
    row["reminder_at"] = (now - timedelta(seconds=1)).isoformat()
    row["expires_at"] = (now + timedelta(seconds=1)).isoformat()
    try:
        repository.process_absence_policy(now)
        # Notification delivery is not an authorization signal. Even if an
        # external notification provider failed, the request remains pending.
        assert row["status"] == "AWAITING_APPROVAL"
        assert row["approval_status"] == "pending"
        assert repository.action_executions == {}
    finally:
        coordinator.close()


def test_blocking_regression_gate_requires_full_recall():
    report = hitl_evals.run()
    assert report["summary"]["hitl_recall"] == 100.0
    assert report["summary"]["release_allowed"] is True
    assert report["summary"]["dataset_escalation_precision"] == 100.0


def test_cross_user_memory_is_not_visible():
    repository = get_repository()
    repository.enforce_auth = True
    repository.memories["mei@example.com"] = {"sessions": ["private"], "actions": ["secret"]}
    token = set_identity(RAJ)
    try:
        assert repository.recall("mei@example.com") is None
    finally:
        reset_identity(token)


def test_identifier_enumeration_has_same_non_disclosing_result():
    repository = get_repository()
    repository.enforce_auth = True
    mei = Identity("22222222-2222-4222-8222-222222222222", "mei@example.com", "Mei", "customer")
    token = set_identity(mei)
    try:
        private, _ = repository.create_support_request("Mei private", "react", "mei-private-001")
    finally:
        reset_identity(token)
    token = set_identity(RAJ)
    try:
        assert repository.get_support_request(private["id"]) is None
        assert repository.get_support_request("00000000-0000-4000-8000-000000000000") is None
    finally:
        reset_identity(token)


def test_forged_workflow_callback_is_not_accepted_without_signature_setup():
    repository = get_repository()
    repository.enforce_auth = True
    with TestClient(create_app(RuntimeState(repository))) as client:
        response = client.post(
            "/workflow/requests",
            json={"request_id": "00000000-0000-4000-8000-000000000000", "phase": "execute"},
            headers={"Upstash-Signature": "forged"},
        )
        assert response.status_code in {401, 404}
        assert repository.action_executions == {}
