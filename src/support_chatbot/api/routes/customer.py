"""Customer-facing asynchronous requests, session, reset, and feedback."""

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from support_chatbot.api.dependencies import (
    require_user,
    runtime,
    session,
    set_session_cookie,
)
from support_chatbot.api.models import (
    ActionConfirmationRequest,
    ActionPreviewRequest,
    ChatRequest,
    FeedbackRequest,
    ResetRequest,
)
from support_chatbot.api.runtime import CHAT_PAGE
from support_chatbot.persistence import ActionProposalError, IdempotencyConflictError


router = APIRouter(tags=["customer"])


@router.get("/", response_class=HTMLResponse)
@router.get("/index.html", response_class=HTMLResponse)
def chat_page(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)
    context = runtime(request).get_session(request.cookies.get("sid"), user.user_id)
    response = HTMLResponse(CHAT_PAGE)
    set_session_cookie(response, context)
    return response


@router.get("/state")
def current_state(request: Request, context=Depends(session)):
    app_runtime = runtime(request)
    with app_runtime.get_turn_lock(context.sid):
        refreshed = app_runtime.reload_session(context.sid, context.session.get("user_id"))
        return {**app_runtime.public_state(refreshed), "stale": context.stale}


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
    result = {
        "request_id": row["id"],
        "reference": row["reference_number"],
        "state": row["status"],
        "created": created,
    }
    if enqueued is not None:
        result["enqueue_status"] = "queued" if enqueued else "pending_recovery"
    if row["status"] == "COMPLETED":
        result["reply"] = row.get("draft_content")
    elif row["status"] == "COMPLETED_WITHOUT_ACTION":
        result["reply"] = "This request expired without an approved action. Please submit it again."
    return result


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
    if created:
        with app_runtime.get_turn_lock(context.sid):
            context.session["convo"].add_user(text)
            context.session["work"].turn += 1
            app_runtime.save_sessions(context.sid)
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
def preview_action(payload: ActionPreviewRequest, request: Request, _=Depends(require_user)):
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
    _=Depends(require_user),
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
