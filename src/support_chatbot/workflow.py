"""Durable support-request orchestration and Upstash Workflow integration."""

from __future__ import annotations

import json
from contextlib import contextmanager

from anyio import to_thread
from qstash import QStash, Receiver
from upstash_workflow.fastapi import Serve

from support_chatbot import policy
from support_chatbot.auth import Identity, reset_identity, set_identity
from support_chatbot.config import settings
from support_chatbot.llm import chat
from support_chatbot.memory import WorkingMemory


SYSTEM_IDENTITY = Identity(
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "admin@example.com",
    "Workflow System",
    "admin",
)


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


class WorkflowCoordinator:
    def __init__(self, repository, dispatcher=None, draft_generator=None):
        self.repository = repository
        self.dispatcher = dispatcher or UpstashDispatcher()
        self.draft_generator = draft_generator or self._generate_draft

    @staticmethod
    def _generate_draft(request):
        safe_text, note = policy.check_input(request["summary"])
        prompt = [
            {
                "role": "system",
                "content": (
                    "You draft concise Amazon-style customer-support resolutions for human review. "
                    "Do not claim an action happened. Do not invent order facts, refunds, dates, or identifiers. "
                    "State what should be checked or done, and make clear this is a proposed response."
                ),
            },
            {"role": "user", "content": safe_text + (f"\nSafety note: {note}" if note else "")},
        ]
        raw = chat(prompt, temperature=0.1)
        return policy.check_output(raw, WorkingMemory(), safe_text), {"type": "send_resolution"}

    def enqueue(self, request):
        phase = "execute" if request["status"] == "APPROVED" else "draft"
        try:
            run_id = self.dispatcher.enqueue(request["id"], phase)
        except EnqueueError as error:
            with system_identity():
                self.repository.mark_enqueue_failed(request["id"], error)
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

    def draft(self, request_id):
        with system_identity():
            request = self.repository.claim_drafting(request_id, settings.workflow_lease_seconds)
        if not request:
            return {"duplicate": True}
        content, proposed_action = self.draft_generator(request)
        with system_identity():
            saved = self.repository.save_resolution_draft(
                request_id, content, proposed_action, settings.approval_ttl_hours
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
