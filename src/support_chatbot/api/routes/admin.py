"""Operator-only monitoring, event, and trace routes."""

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from support_chatbot import dashboard, observe
from support_chatbot.api.dependencies import require_admin, require_internal_ui, runtime
from support_chatbot.api.models import ApprovalDecisionRequest
from support_chatbot.llm import MODEL
from support_chatbot.persistence import InvalidWorkflowTransition


router = APIRouter(
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.get("/monitoring", response_class=HTMLResponse)
@router.get("/logs", response_class=HTMLResponse)
@router.get("/admin", response_class=HTMLResponse)
def monitoring_page(_=Depends(require_internal_ui)):
    return HTMLResponse(dashboard.PAGE)


@router.get("/logs.json")
def monitoring_data(request: Request, _=Depends(require_internal_ui)):
    app_runtime = runtime(request)
    return {
        "stats": observe.stats(),
        "events": observe.recent(120),
        "runtime": {
            "status": "operational",
            "uptime_seconds": round(time.time() - app_runtime.started_at),
            "sessions": len(app_runtime.sessions),
            "model": MODEL,
        },
    }


@router.get("/trace.jsonl", response_class=PlainTextResponse)
def trace_log(_=Depends(require_internal_ui)):
    try:
        return PlainTextResponse(observe.LOGFILE.read_text())
    except OSError:
        return PlainTextResponse("")


@router.post("/admin/requests/{request_id}/decision")
def decide_request(
    request_id: str,
    payload: ApprovalDecisionRequest,
    request: Request,
    admin=Depends(require_admin),
):
    app_runtime = runtime(request)
    approve = payload.decision == "approve"
    try:
        row = app_runtime.repository.decide_support_request(
            request_id, approve, payload.reason.strip(), admin.user_id
        )
    except InvalidWorkflowTransition as error:
        raise HTTPException(status_code=409, detail="Request is not awaiting approval") from error
    enqueued = None
    if approve:
        enqueued, _ = app_runtime.workflow.enqueue(row)
    return {
        "request_id": row["id"],
        "state": row["status"],
        "enqueue_status": "queued" if enqueued else ("pending_recovery" if approve else "not_required"),
    }
