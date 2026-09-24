"""Operator-only monitoring, event, and trace routes."""

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from support_chatbot import dashboard, observe
from support_chatbot.operational import public_events, sanitized_logfile
from support_chatbot.api.dependencies import require_admin, require_internal_ui, runtime
from support_chatbot.api.models import (
    ApprovalDecisionRequest, ApprovalReassignRequest, TicketUpdateRequest,
)
from support_chatbot.llm import MODEL
from support_chatbot.persistence import ActionProposalError, InvalidWorkflowTransition


router = APIRouter(
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.get("/monitoring", response_class=HTMLResponse)
@router.get("/logs", response_class=HTMLResponse)
@router.get("/admin", response_class=HTMLResponse)
def monitoring_page(_=Depends(require_internal_ui)):
    return HTMLResponse(dashboard.PAGE)


@router.get("/admin/approvals", response_class=HTMLResponse)
def approvals_page():
    return HTMLResponse(dashboard.APPROVAL_PAGE)


@router.get("/admin/tickets", response_class=HTMLResponse)
def tickets_page():
    return HTMLResponse(dashboard.ADMIN_TICKETS_PAGE)


@router.get("/admin/tickets.json")
def tickets_data(request: Request, status: str = "open"):
    if status not in {"open", "in_progress", "resolved", "closed", "all"}:
        raise HTTPException(status_code=422, detail="Unknown ticket filter")
    return {"items": runtime(request).repository.list_tickets(status), "filter": status}


@router.patch("/admin/tickets/{ticket_id}")
def update_ticket(
    ticket_id: str, payload: TicketUpdateRequest, request: Request,
    admin=Depends(require_admin),
):
    row = runtime(request).repository.update_ticket(
        ticket_id, payload.status, payload.resolution.strip(), admin.user_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Support ticket not found")
    return row


@router.get("/admin/approvals.json")
def approvals_data(request: Request, status: str = "pending"):
    if status not in {"pending", "overdue", "approved", "rejected", "expired", "all"}:
        raise HTTPException(status_code=422, detail="Unknown approval filter")
    app_runtime = runtime(request)
    return {
        "items": app_runtime.repository.list_approval_tasks(status),
        "reviewers": app_runtime.repository.list_admin_reviewers(),
        "filter": status,
    }


@router.get("/admin/approvals/{request_id}.json")
def approval_detail(request_id: str, request: Request):
    row = runtime(request).repository.get_approval_detail(request_id)
    if not row:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return row


@router.get("/logs.json")
def monitoring_data(request: Request, _=Depends(require_internal_ui)):
    app_runtime = runtime(request)
    return {
        "stats": observe.stats(),
        "events": public_events(observe.recent(120)),
        "operations": app_runtime.repository.operational_metrics(),
        "requests": app_runtime.repository.operational_requests(100),
        "runtime": {
            "status": "operational",
            "uptime_seconds": round(time.time() - app_runtime.started_at),
            "sessions": len(app_runtime.sessions),
            "model": MODEL,
        },
    }


@router.get("/admin/operations/requests/{request_id}.json")
def operational_request_detail(request_id: str, request: Request,
                               _=Depends(require_internal_ui)):
    result = runtime(request).repository.operational_request_timeline(request_id)
    if not result:
        raise HTTPException(status_code=404, detail="Operational request not found")
    return result


@router.get("/trace.jsonl", response_class=PlainTextResponse)
def trace_log(_=Depends(require_internal_ui)):
    return PlainTextResponse(sanitized_logfile(observe.LOGFILE))


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
            request_id, approve, payload.reason.strip(), admin.user_id,
            payload.edited_response, payload.review_necessary,
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


@router.post("/admin/requests/{request_id}/reassign")
def reassign_request(
    request_id: str,
    payload: ApprovalReassignRequest,
    request: Request,
    admin=Depends(require_admin),
):
    try:
        return runtime(request).repository.reassign_approval(
            request_id, payload.admin_user_id, payload.reason.strip(), admin.user_id
        )
    except (InvalidWorkflowTransition, ActionProposalError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
