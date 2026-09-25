"""Customer-facing asynchronous requests, session, reset, and feedback."""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from support_chatbot.api.dependencies import (
    require_customer,
    runtime,
    session,
    set_session_cookie,
)
from support_chatbot.api.models import (
    ActionConfirmationRequest,
    ActionPreviewRequest,
    ChatRequest,
    FeedbackRequest,
    RenameConversationRequest,
    ResetRequest,
)
from support_chatbot.api.runtime import CHAT_PAGE
from support_chatbot import UI_DIR
from support_chatbot.config import settings
from support_chatbot.persistence import (
    ActionProposalError,
    ConversationBusyError,
    IdempotencyConflictError,
)


router = APIRouter(tags=["customer"])
CUSTOMER_TICKETS_PAGE = (UI_DIR / "customer_tickets.html").read_text()


@router.get("/", response_class=HTMLResponse)
@router.get("/index.html", response_class=HTMLResponse)
def chat_page(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.role != "customer":
        return RedirectResponse("/admin/approvals", status_code=303)
    context = runtime(request).get_session(
        request.cookies.get("sid"), user.user_id, user.email, restore_latest=False
    )
    response = HTMLResponse(CHAT_PAGE)
    set_session_cookie(response, context)
    return response


@router.get("/state")
def current_state(request: Request, context=Depends(session)):
    app_runtime = runtime(request)
    with app_runtime.get_turn_lock(context.sid):
        refreshed = app_runtime.reload_session(context.sid, context.session.get("user_id"))
        user = request.state.user
        return {
            **app_runtime.public_state(refreshed), "stale": context.stale,
            "user": user.public(), "conversation_id": context.sid,
        }


@router.get("/conversations")
def conversations(request: Request, _=Depends(require_customer)):
    return {"items": runtime(request).repository.list_conversations()}


@router.get("/tickets", response_class=HTMLResponse)
def customer_tickets_page(_=Depends(require_customer)):
    return HTMLResponse(CUSTOMER_TICKETS_PAGE)


@router.get("/tickets.json")
def customer_tickets_data(request: Request, _=Depends(require_customer)):
    return {"items": runtime(request).repository.list_tickets("all")}


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str, payload: RenameConversationRequest,
    request: Request, _=Depends(require_customer),
):
    if not runtime(request).repository.rename_conversation(conversation_id, payload.title):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"ok": True, "conversation_id": conversation_id, "title": payload.title}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str, request: Request, response: Response,
    user=Depends(require_customer),
):
    app_runtime = runtime(request)
    try:
        deleted = app_runtime.repository.delete_conversation(conversation_id)
    except ConversationBusyError as error:
        raise HTTPException(
            status_code=409,
            detail="This conversation has an active request. Wait for it to finish before deleting it.",
        ) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    app_runtime.discard_session(conversation_id)
    replacement_id = None
    if request.cookies.get("sid") == conversation_id:
        replacement = app_runtime.create_session(user.user_id, user.email)
        replacement_id = replacement.sid
        set_session_cookie(response, replacement)
    return {"ok": True, "conversation_id": replacement_id}


@router.post("/conversations", status_code=201)
def new_conversation(request: Request, response: Response, user=Depends(require_customer)):
    context = runtime(request).create_session(user.user_id, user.email)
    set_session_cookie(response, context)
    return {"conversation_id": context.sid, **runtime(request).public_state(context.session)}


@router.post("/conversations/{conversation_id}/activate")
def activate_conversation(
    conversation_id: str, request: Request, response: Response,
    user=Depends(require_customer),
):
    if not runtime(request).repository.load_session(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    context = runtime(request).get_session(
        conversation_id, user.user_id, user.email, restore_latest=False
    )
    if context.stale:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Selecting an existing thread must update the browser even though the
    # context itself was not newly created.
    response.set_cookie(
        "sid", context.sid, path="/", httponly=True, samesite="lax",
        secure=settings.secure_cookies,
    )
    return {"conversation_id": context.sid, **runtime(request).public_state(context.session)}


@router.post("/reset")
def reset_conversation(
    request: Request,
    payload: ResetRequest | None = None,
    context=Depends(session),
):
    del payload
    app_runtime = runtime(request)
    with app_runtime.get_turn_lock(context.sid):
        fresh = app_runtime.reset_session(context.sid)
        app_runtime.save_sessions(context.sid)
        return {"ok": True, **app_runtime.public_state(fresh)}


@router.post("/feedback")
def submit_feedback(
    payload: FeedbackRequest,
    request: Request,
    context=Depends(session),
):
    del context
    runtime(request).save_feedback(
        payload.golden_row_id,
        payload.original_response,
        payload.corrected_response,
        payload.reason,
    )
    return {"ok": True, "message": "Feedback saved"}


def _customer_request(row, *, created=False, enqueued=None):
    approval_status = row.get("approval_status")
    if approval_status == "rejected":
        public_state = "REJECTED"
    elif approval_status == "expired":
        public_state = "EXPIRED"
    else:
        public_state = row["status"]
    proposed_action = row.get("proposed_action") or {}
    action_name = row.get("action_name") or proposed_action.get("type") or "send_resolution"
    action_required = action_name != "send_resolution"
    result = {
        "request_id": row["id"],
        "conversation_id": row.get("browser_session_id") or row.get("conversation_id"),
        "reference": row["reference_number"],
        "message": row.get("summary"),
        "state": public_state,
        "raw_state": row["status"],
        "created": created,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "action": {
            "required": action_required,
            "name": action_name if action_required else None,
            "pending": action_required and public_state not in {"COMPLETED", "REJECTED", "EXPIRED"},
            "complete": action_required and public_state == "COMPLETED",
        },
    }
    status_message = (row.get("metadata") or {}).get("customer_status")
    if status_message:
        result["status_message"] = status_message
    if enqueued is not None:
        result["enqueue_status"] = "queued" if enqueued else "pending_recovery"
    if row.get("last_error") and row["status"] in {"RECEIVED", "QUEUED"}:
        result["retrying"] = True
    if public_state == "COMPLETED":
        result["reply"] = row.get("draft_content")
    elif public_state == "REJECTED":
        result["reply"] = "A support specialist rejected this resolution. No action was taken."
    elif public_state == "EXPIRED" or row["status"] == "COMPLETED_WITHOUT_ACTION":
        result["reply"] = status_message or "This request expired without an approved action. Please submit it again."
    return result


@router.get("/requests")
def recent_requests(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    conversation_id: str | None = Query(None, min_length=1, max_length=64),
    context=Depends(session),
):
    del context
    rows = runtime(request).repository.list_support_requests(limit)
    if conversation_id:
        rows = [row for row in rows if (
            row.get("browser_session_id") or row.get("conversation_id")
        ) == conversation_id]
    return {"items": [_customer_request(row) for row in rows]}


@router.post("/chat", status_code=202)
def submit_request(
    payload: ChatRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    context=Depends(session),
):
    app_runtime = runtime(request)
    text = payload.message.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Message must not be blank")
    try:
        row, created = app_runtime.repository.create_support_request(
            text, payload.planner, idempotency_key, context.sid
        )
    except IdempotencyConflictError as error:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used with a different request",
        ) from error
    except ConversationBusyError as error:
        raise HTTPException(
            status_code=409,
            detail="Wait for Ami to finish the current response before sending another message",
        ) from error
    if created:
        # The async worker may have persisted an assistant reply since this
        # browser context was cached. Always append to the durable transcript.
        app_runtime.append_user_turn(
            context.sid, context.session.get("user_id"), text,
        )
    should_enqueue = created or (row["status"] in {"RECEIVED", "APPROVED"} and not row.get("enqueued_at"))
    enqueued = None
    if should_enqueue:
        enqueued, _ = app_runtime.workflow.enqueue(row)
        row = app_runtime.repository.get_support_request(row["id"]) or row
    return _customer_request(row, created=created, enqueued=enqueued)


@router.get("/requests/{request_id}")
def request_status(request_id: str, request: Request, context=Depends(session)):
    del context
    row = runtime(request).repository.get_support_request(request_id)
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    return _customer_request(row)


@router.post("/actions/preview")
def preview_action(payload: ActionPreviewRequest, request: Request, _=Depends(require_customer)):
    arguments = {"order_id": payload.order_id}
    if payload.reason is not None:
        arguments["reason"] = payload.reason
    try:
        return runtime(request).repository.create_action_proposal(payload.action, arguments)
    except ActionProposalError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/actions/proposals/{proposal_id}/confirm", status_code=202)
def confirm_action(
    proposal_id: str,
    payload: ActionConfirmationRequest,
    request: Request,
    _=Depends(require_customer),
):
    try:
        row, created = runtime(request).repository.confirm_action_proposal(
            proposal_id, payload.action_hash, payload.idempotency_key
        )
    except IdempotencyConflictError as error:
        raise HTTPException(status_code=409, detail="Idempotency key conflict") from error
    except ActionProposalError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _customer_request(row, created=created)
