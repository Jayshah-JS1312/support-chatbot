"""Durable support-request orchestration and Upstash Workflow integration."""

from __future__ import annotations

import json
import queue
import threading
import uuid
from contextlib import contextmanager

from anyio import to_thread
from qstash import QStash, Receiver
from upstash_workflow.fastapi import Serve

from support_chatbot import observe, policy
from support_chatbot.auth import Identity, reset_identity, set_identity
from support_chatbot.config import settings
from support_chatbot.llm import chat
from support_chatbot.hitl_policy import classify_proposal
from support_chatbot.memory import WorkingMemory


SYSTEM_IDENTITY = Identity(
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "admin@example.com",
    "Workflow System",
    "admin",
)


def _pending_arguments(conversation, pending):
    """Recover arguments from new state or a pre-fix persisted tool request."""
    arguments = dict((pending or {}).get("args") or {})
    key = (pending or {}).get("key") or []
    if len(key) != 2:
        return None, {}
    action, order_id = key
    arguments.setdefault("order_id", order_id)
    if action == "start_return" and not arguments.get("reason"):
        for message in reversed(conversation.history):
            for call in reversed(message.get("tool_calls") or []):
                function = call.get("function") or {}
                if function.get("name") != action:
                    continue
                try:
                    candidate = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    continue
                if candidate.get("order_id") == order_id and candidate.get("reason"):
                    arguments["reason"] = candidate["reason"]
                    break
            if arguments.get("reason"):
                break
    arguments.pop("confirmed", None)
    arguments.pop("thought", None)
    return action, arguments


def _proposal_payload(result):
    return {
        "type": result["action"],
        "arguments": result["arguments"],
        "customer_consequences": result["consequences"],
        "policy_evidence": result["policy_evidence"],
        "order_version": result["order_version"],
        "action_hash": result["action_hash"],
        "customer_confirmed": True,
    }


class EnqueueError(RuntimeError):
    pass


@contextmanager
def system_identity():
    token = set_identity(SYSTEM_IDENTITY)
    try:
        yield
    finally:
        reset_identity(token)


class UpstashDispatcher:
    """Start a Workflow run; the endpoint returns before its first step executes."""

    def __init__(self, url=None):
        self.url = url or settings.workflow_url

    def enqueue(self, request_id, phase):
        if not settings.workflow_enabled:
            raise EnqueueError("Upstash Workflow credentials are not configured")
        try:
            response = QStash(settings.qstash_token).message.publish_json(
                url=self.url,
                body={"request_id": str(request_id), "phase": phase},
                method="POST",
                retries=settings.workflow_retries,
                deduplication_id=f"{request_id}-{phase}",
            )
            return response.message_id
        except Exception as error:
            raise EnqueueError(type(error).__name__) from error


class LocalWorkflowDispatcher:
    """Process durable workflow deliveries off-request for local Docker use."""

    def __init__(self, workers=None, queue_max=None):
        worker_count = workers if workers is not None else settings.local_workflow_workers
        queue_limit = queue_max if queue_max is not None else settings.local_workflow_queue_max
        if worker_count < 1 or queue_limit < 1:
            raise ValueError("Local workflow workers and queue size must be positive")
        self.messages = queue.Queue(maxsize=queue_limit)
        self.coordinator = None
        self.closed = threading.Event()
        self.workers = [
            threading.Thread(
                target=self._run,
                name=f"ami-local-workflow-{index + 1}",
                daemon=True,
            )
            for index in range(worker_count)
        ]
        for worker in self.workers:
            worker.start()

    def bind(self, coordinator):
        self.coordinator = coordinator

    def enqueue(self, request_id, phase):
        run_id = f"local-{uuid.uuid4().hex}"
        try:
            self.messages.put_nowait((str(request_id), phase, run_id))
        except queue.Full as error:
            raise EnqueueError("local_workflow_queue_full") from error
        return run_id

    def _run(self):
        while not self.closed.is_set():
            try:
                request_id, phase, _run_id = self.messages.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self.coordinator is None:
                    raise RuntimeError("Local workflow dispatcher is not bound")
                if phase == "execute":
                    self.coordinator.execute(request_id)
                else:
                    self.coordinator.draft(request_id)
            except Exception as error:
                if self.coordinator is not None:
                    try:
                        self.coordinator.reschedule_local_failure(request_id, phase, error)
                    except Exception as recovery_error:
                        # A temporary database outage must not permanently kill
                        # a local worker. The durable lease will become eligible
                        # for the periodic recovery scan after it expires.
                        observe.log(
                            "error",
                            where="local_workflow_reschedule",
                            error=type(recovery_error).__name__,
                        )
                observe.log("error", where="local_workflow", error=type(error).__name__)
            finally:
                self.messages.task_done()

    def close(self):
        self.closed.set()
        for worker in self.workers:
            worker.join(timeout=2)


class WorkflowCoordinator:
    def __init__(self, repository, dispatcher=None, draft_generator=None, hitl_router=None):
        self.repository = repository
        self.dispatcher = dispatcher or (
            UpstashDispatcher() if settings.workflow_enabled else LocalWorkflowDispatcher()
        )
        self.draft_generator = draft_generator or self._generate_draft
        self.hitl_router = hitl_router or classify_proposal
        if isinstance(self.dispatcher, LocalWorkflowDispatcher):
            self.dispatcher.bind(self)
        self.absence_closed = threading.Event()
        self.absence_worker = threading.Thread(
            target=self._monitor_absence, name="ami-absence-policy", daemon=True
        )
        self.absence_worker.start()
        self.recovery_closed = threading.Event()
        self.recovery_worker = threading.Thread(
            target=self._monitor_recovery, name="ami-workflow-recovery", daemon=True
        )
        self.recovery_worker.start()

    def close(self):
        self.absence_closed.set()
        self.absence_worker.join(timeout=2)
        self.recovery_closed.set()
        self.recovery_worker.join(timeout=2)
        close = getattr(self.dispatcher, "close", None)
        if close:
            close()

    def _monitor_absence(self):
        interval = max(1, settings.absence_scan_seconds)
        while not self.absence_closed.wait(interval):
            try:
                self.apply_absence_policy()
            except Exception as error:
                observe.log("error", where="absence_policy", error=type(error).__name__)

    def _monitor_recovery(self):
        interval = max(1, settings.workflow_recovery_scan_seconds)
        while not self.recovery_closed.wait(interval):
            try:
                self.recover()
            except Exception as error:
                observe.log("error", where="workflow_recovery", error=type(error).__name__)

    def apply_absence_policy(self):
        with system_identity():
            return self.repository.process_absence_policy()

    def _generate_draft(self, request):
        from support_chatbot import agent_profile, plan_execute, planner
        from support_chatbot.memory import ConversationMemory, LongTermMemory

        with system_identity():
            context = self.repository.get_request_agent_context(request["id"])
        direct = policy.direct_response(request["summary"])
        if direct:
            if context and context["session_id"]:
                from support_chatbot import agent_profile
                from support_chatbot.memory import ConversationMemory
                token = set_identity(context["identity"])
                try:
                    persisted = self.repository.load_session(context["session_id"]) or {"history": [], "work": {}}
                    conversation = ConversationMemory.from_dict(
                        agent_profile.system_prompt(), {"history": persisted["history"]}
                    )
                    conversation.add_assistant({"role": "assistant", "content": direct})
                    self.repository.save_session(
                        context["session_id"], conversation.history,
                        persisted["work"], context["planner"],
                    )
                finally:
                    reset_identity(token)
            return direct, {"type": "send_resolution", "arguments": {}, "policy_evidence": []}
        if not context or not context["session_id"]:
            safe_text, note = policy.check_input(request["summary"])
            prompt = [{"role": "system", "content": agent_profile.system_prompt()},
                      {"role": "user", "content": safe_text + (f"\nSafety note: {note}" if note else "")}]
            raw = chat(prompt, temperature=0.1)
            return policy.check_output(raw, WorkingMemory(), safe_text), {"type": "send_resolution"}

        token = set_identity(context["identity"])
        try:
            persisted = self.repository.load_session(context["session_id"]) or {"history": [], "work": {}}
            conversation = ConversationMemory.from_dict(
                agent_profile.system_prompt(), {"history": persisted["history"]}
            )
            work = WorkingMemory.from_dict(persisted["work"])
            work.session_id = context["session_id"]
            work.request_id = request["id"]
            safe_text, note = policy.check_input(request["summary"])
            decision = policy.confirmation_decision(safe_text) if work.pending else None
            if work.pending and decision is not None:
                action, arguments = _pending_arguments(conversation, work.pending)
                if decision is False:
                    work.pending = None
                    safe = "Understood — I won’t proceed. No account action was taken."
                    proposed = {"type": "send_resolution", "arguments": {}, "policy_evidence": []}
                elif not action or (action == "start_return" and not arguments.get("reason")):
                    work.pending = None
                    safe = ("I couldn’t safely recover the return details, so no action was taken. "
                            "Please tell me which order you want to return and why.")
                    proposed = {"type": "send_resolution", "arguments": {}, "policy_evidence": []}
                else:
                    result = policy.guarded_run(action, {**arguments, "confirmed": True}, work)
                    work.record(action, arguments, result)
                    if result.get("proposal"):
                        label = "return" if action == "start_return" else "cancellation"
                        safe = (f"Thanks — I submitted the {label} proposal for human review. "
                                "No account action has been taken yet.")
                        proposed = _proposal_payload(result)
                    else:
                        safe = result.get("error") or (
                            "I couldn’t create the proposal, so no account action was taken."
                        )
                        proposed = {"type": "send_resolution", "arguments": {}, "policy_evidence": []}
                conversation.add_assistant({"role": "assistant", "content": safe})
                self.repository.save_session(
                    context["session_id"], conversation.history, work.to_dict(), context["planner"]
                )
                self.repository.remember(work.to_dict(), context["session_id"])
                return safe, proposed
            work._confirmation_verified = False if work.pending else None
            engine = plan_execute.plan_execute if context["planner"] == "plan" else planner.react
            steps = []
            raw = engine(
                conversation, work, trace=False, steps=steps,
                longterm=LongTermMemory(self.repository), extra=note,
            )
            safe = policy.check_output(raw, work, safe_text)
            work.update_focus_from_reply(safe)
            conversation.persist_safe_reply(raw, safe)
            self.repository.save_session(
                context["session_id"], conversation.history, work.to_dict(), context["planner"]
            )
            self.repository.remember(work.to_dict(), context["session_id"])
            evidence = []
            proposed = None
            for step in steps:
                observation = step.get("observation") or {}
                if step.get("tool") == "search_knowledge":
                    evidence.extend(observation.get("passages") or [])
                if observation.get("proposal"):
                    proposed = {
                        "type": observation["action"],
                        "arguments": observation["arguments"],
                        "customer_consequences": observation["consequences"],
                        "policy_evidence": observation["policy_evidence"],
                        "order_version": observation["order_version"],
                        "action_hash": observation["action_hash"],
                        "customer_confirmed": True,
                    }
            return safe, proposed or {
                "type": "send_resolution", "arguments": {},
                "policy_evidence": evidence,
            }
        finally:
            reset_identity(token)

    def enqueue(self, request):
        phase = "execute" if request["status"] in {"APPROVED", "EXECUTING"} else "draft"
        try:
            run_id = self.dispatcher.enqueue(request["id"], phase)
        except EnqueueError as error:
            with system_identity():
                failed = self.repository.mark_enqueue_failed(
                    request["id"], error, settings.workflow_enqueue_max_attempts
                )
                if failed and failed.get("status") == "COMPLETED_WITHOUT_ACTION":
                    self.repository.record_dead_letter(
                        request["id"], None, 503, "enqueue_retry_exhausted",
                        {"attempts": failed.get("enqueue_attempts")},
                    )
            return False, None
        with system_identity():
            self.repository.mark_enqueued(request["id"], run_id)
        return True, run_id

    def recover(self, limit=100):
        with system_identity():
            requests = self.repository.pending_enqueues(limit)
        result = {"attempted": len(requests), "enqueued": 0, "pending": 0}
        for request in requests:
            success, _ = self.enqueue(request)
            result["enqueued" if success else "pending"] += 1
        return result

    def reschedule_local_failure(self, request_id, phase, error):
        """Release a failed local delivery back to durable retry state."""
        with system_identity():
            row = self.repository.reschedule_processing_failure(
                request_id,
                phase,
                type(error).__name__,
                settings.workflow_enqueue_max_attempts,
            )
            if row and row.get("status") == "COMPLETED_WITHOUT_ACTION":
                self.repository.record_dead_letter(
                    request_id,
                    None,
                    503,
                    "processing_retry_exhausted",
                    {"attempts": row.get("enqueue_attempts"), "phase": phase},
                )
            return row

    def draft(self, request_id):
        with system_identity():
            request = self.repository.claim_drafting(request_id, settings.workflow_lease_seconds)
        if not request:
            return {"duplicate": True}
        content, proposed_action = self.draft_generator(request)
        routing = self.hitl_router(proposed_action)
        with system_identity():
            saved = self.repository.save_resolution_draft(
                request_id, content, proposed_action, settings.approval_deadlines,
                requires_hitl=routing.requires_hitl,
                routing_reason=routing.reason,
            )
        return {"duplicate": False, "state": saved["status"]}

    def execute(self, request_id):
        with system_identity():
            claimed = self.repository.claim_execution(request_id, settings.workflow_lease_seconds)
            if not claimed:
                existing = self.repository.get_support_request(request_id)
                return {"duplicate": True, "state": existing["status"] if existing else None}
            completed = self.repository.complete_execution(request_id)
        return {"duplicate": False, "state": completed["status"]}

    def record_failure(self, request_id, workflow_run_id, status, body, headers):
        with system_identity():
            self.repository.record_dead_letter(
                request_id, workflow_run_id, status, body, headers
            )


def configure_upstash_workflow(app):
    """Register the signed callback endpoint only when all Upstash keys exist."""
    if not settings.workflow_enabled:
        return
    receiver = Receiver(
        current_signing_key=settings.qstash_current_signing_key,
        next_signing_key=settings.qstash_next_signing_key,
    )
    serve = Serve(app)

    async def failure(context, fail_status, fail_response, fail_headers):
        payload = context.request_payload
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        if request_id:
            await to_thread.run_sync(
                app.state.runtime.workflow.record_failure,
                request_id,
                context.workflow_run_id,
                fail_status,
                fail_response,
                fail_headers,
            )

    @serve.post(
        "/workflow/requests",
        receiver=receiver,
        base_url=settings.public_base_url,
        env={
            "QSTASH_TOKEN": settings.qstash_token,
            "QSTASH_CURRENT_SIGNING_KEY": settings.qstash_current_signing_key,
            "QSTASH_NEXT_SIGNING_KEY": settings.qstash_next_signing_key,
        },
        retries=settings.workflow_retries,
        initial_payload_parser=json.loads,
        failure_function=failure,
    )
    async def support_request_workflow(context):
        payload = context.request_payload
        request_id = payload["request_id"]
        phase = payload.get("phase", "draft")
        if phase == "execute":
            await context.run(
                f"execute-{request_id}",
                lambda: to_thread.run_sync(app.state.runtime.workflow.execute, request_id),
            )
        else:
            await context.run(
                f"draft-{request_id}",
                lambda: to_thread.run_sync(app.state.runtime.workflow.draft, request_id),
            )
