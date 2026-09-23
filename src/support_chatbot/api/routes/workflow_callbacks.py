"""Asynchronous workflow callback route boundary.

Callbacks are intentionally not accepted until the later durable workflow and
authorization phases define signed payloads, replay protection, and idempotency.
"""

from fastapi import APIRouter, Depends, HTTPException

from support_chatbot.api.dependencies import require_admin


router = APIRouter(prefix="/workflow", tags=["workflow-callbacks"], dependencies=[Depends(require_admin)])


@router.post("/callbacks")
def workflow_callback_boundary():
    raise HTTPException(status_code=501, detail="Workflow callbacks are introduced in the asynchronous handoff phase")
