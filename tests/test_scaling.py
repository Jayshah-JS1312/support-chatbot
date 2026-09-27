import threading
import time

import httpx
import pytest
from openai import APITimeoutError

from support_chatbot import llm
from support_chatbot.api.runtime import RuntimeState
from support_chatbot.auth import Identity, reset_identity, set_identity
from support_chatbot.config import settings
from support_chatbot.persistence import InMemoryRepository
from support_chatbot.workflow import EnqueueError, LocalWorkflowDispatcher


class BlockingCoordinator:
    def __init__(self, release):
        self.release = release
        self.started = 0
        self.started_two = threading.Event()
        self.lock = threading.Lock()
        self.failures = []

    def draft(self, request_id):
        with self.lock:
            self.started += 1
            if self.started == 2:
                self.started_two.set()
        self.release.wait(2)

    execute = draft

    def record_failure(self, *args):
        self.failures.append(args)


class RecoveryFailureCoordinator:
    def __init__(self):
        self.calls = 0
        self.second_completed = threading.Event()

    def draft(self, request_id):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("processing failed")
        self.second_completed.set()

    def reschedule_local_failure(self, request_id, phase, error):
        raise RuntimeError("database unavailable")


def test_local_dispatcher_runs_multiple_deliveries_concurrently():
    release = threading.Event()
    coordinator = BlockingCoordinator(release)
    dispatcher = LocalWorkflowDispatcher(workers=2, queue_max=4)
    dispatcher.bind(coordinator)
    try:
        dispatcher.enqueue("one", "draft")
        dispatcher.enqueue("two", "draft")
        assert coordinator.started_two.wait(1), "both workers should enter draft concurrently"
    finally:
        release.set()
        dispatcher.messages.join()
        dispatcher.close()


def test_local_dispatcher_rejects_work_when_bounded_queue_is_full():
    release = threading.Event()
    coordinator = BlockingCoordinator(release)
    dispatcher = LocalWorkflowDispatcher(workers=1, queue_max=1)
    dispatcher.bind(coordinator)
    try:
        dispatcher.enqueue("running", "draft")
        deadline = time.monotonic() + 1
        while coordinator.started < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        dispatcher.enqueue("queued", "draft")
        with pytest.raises(EnqueueError, match="local_workflow_queue_full"):
            dispatcher.enqueue("rejected", "draft")
    finally:
        release.set()
        dispatcher.messages.join()
        dispatcher.close()


def test_local_worker_survives_a_failed_reschedule_attempt():
    coordinator = RecoveryFailureCoordinator()
    dispatcher = LocalWorkflowDispatcher(workers=1, queue_max=2)
    dispatcher.bind(coordinator)
    try:
        dispatcher.enqueue("first", "draft")
        dispatcher.enqueue("second", "draft")
        assert coordinator.second_completed.wait(1)
    finally:
        dispatcher.messages.join()
        dispatcher.close()


def test_processing_retries_fail_closed_after_configured_limit():
    repository = InMemoryRepository()
    identity = Identity("customer-1", "raj@example.com", "Raj", "customer")
    token = set_identity(identity)
    try:
        request, _ = repository.create_support_request(
            "Where is my order?", "react", "retry-limit-test"
        )
        repository.mark_enqueued(request["id"], "run-1")
        repository.claim_drafting(request["id"], 30)
        failed = repository.reschedule_processing_failure(
            request["id"], "draft", "provider timeout", max_attempts=1
        )
    finally:
        reset_identity(token)

    assert failed["status"] == "COMPLETED_WITHOUT_ACTION"
    assert failed["metadata"]["failure_closed"] is True


def test_runtime_session_cache_is_bounded_without_losing_durable_state():
    original = settings.runtime_session_cache_max
    object.__setattr__(settings, "runtime_session_cache_max", 2)
    repository = InMemoryRepository()
    runtime = RuntimeState(repository)
    try:
        first = runtime.create_session("customer-1", "raj@example.com").sid
        runtime.create_session("customer-1", "raj@example.com")
        runtime.create_session("customer-1", "raj@example.com")
        assert len(runtime.sessions) == 2
        assert first not in runtime.sessions
        assert repository.load_session(first) is not None
    finally:
        runtime.close()
        object.__setattr__(settings, "runtime_session_cache_max", original)


def test_prompt_cache_key_is_stable_and_tenant_isolated():
    raj = set_identity(Identity("raj-id", "raj@example.com", "Raj", "customer"))
    try:
        first = llm._cache_key()
        second = llm._cache_key()
    finally:
        reset_identity(raj)
    mei = set_identity(Identity("mei-id", "mei@example.com", "Mei", "customer"))
    try:
        other = llm._cache_key()
    finally:
        reset_identity(mei)

    assert first == second
    assert first != other
    assert "raj-id" not in first
    assert "raj@example.com" not in first


def test_model_concurrency_limit_fails_fast(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(llm, "_model_slots", slots)
    original = settings.model_acquire_timeout_seconds
    object.__setattr__(settings, "model_acquire_timeout_seconds", 0.01)
    try:
        with pytest.raises(llm.ModelCapacityError, match="model_concurrency_limit_reached"):
            llm._call({"model": "test", "messages": []})
    finally:
        slots.release()
        object.__setattr__(settings, "model_acquire_timeout_seconds", original)


def test_transient_model_failure_retries_with_bounded_backoff(monkeypatch):
    class Completions:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise APITimeoutError(request=httpx.Request("POST", "https://model.test"))
            return "ok"

    completions = Completions()
    fake_client = type(
        "Client", (), {"chat": type("Chat", (), {"completions": completions})()}
    )()
    sleeps = []
    monkeypatch.setattr(llm, "client", lambda: fake_client)
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    monkeypatch.setattr(llm.random, "uniform", lambda start, end: 0)
    originals = {
        "model_retry_attempts": settings.model_retry_attempts,
        "model_retry_base_seconds": settings.model_retry_base_seconds,
        "model_retry_max_seconds": settings.model_retry_max_seconds,
        "model_retry_budget_seconds": settings.model_retry_budget_seconds,
    }
    try:
        object.__setattr__(settings, "model_retry_attempts", 2)
        object.__setattr__(settings, "model_retry_base_seconds", 0.25)
        object.__setattr__(settings, "model_retry_max_seconds", 1)
        object.__setattr__(settings, "model_retry_budget_seconds", 2)
        assert llm._call({"model": "test", "messages": []}) == "ok"
    finally:
        for name, value in originals.items():
            object.__setattr__(settings, name, value)

    assert completions.calls == 2
    assert sleeps == [0.25]
