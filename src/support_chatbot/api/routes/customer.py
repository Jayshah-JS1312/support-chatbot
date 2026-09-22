"""Customer-facing chat, session, reset, and feedback routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from support_chatbot import observe, policy
from support_chatbot.api.dependencies import (
    runtime,
    session,
    set_session_cookie,
)
from support_chatbot.api.models import ChatRequest, FeedbackRequest, ResetRequest
from support_chatbot.api.runtime import CHAT_PAGE, PLANNERS, SYSTEM
from support_chatbot.config import settings


router = APIRouter(tags=["customer"])


@router.get("/", response_class=HTMLResponse)
@router.get("/index.html", response_class=HTMLResponse)
def chat_page(request: Request):
    context = runtime(request).get_session(request.cookies.get("sid"))
    response = HTMLResponse(CHAT_PAGE)
    set_session_cookie(response, context)
    return response


@router.get("/state")
def current_state(request: Request, context=Depends(session)):
    app_runtime = runtime(request)
    with app_runtime.get_turn_lock(context.sid):
        return {**app_runtime.public_state(context.session), "stale": context.stale}


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
        app_runtime.save_sessions()
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


@router.post("/chat")
def chat(
    payload: ChatRequest,
    request: Request,
    context=Depends(session),
):
    app_runtime = runtime(request)
    with app_runtime.get_turn_lock(context.sid):
        chat_session = context.session
        text = payload.message.strip()
        if not text:
            return {
                "reply": "",
                "steps": [],
                **app_runtime.public_state(chat_session),
            }

        convo, work = chat_session["convo"], chat_session["work"]
        before = len(work.actions)
        run, rules = PLANNERS[payload.planner]
        convo.system = SYSTEM + rules

        observe.context(session=context.sid)
        turn_id = observe.new_turn()

        text, note = policy.check_input(text)
        work.turn += 1
        work.session_id = context.sid
        convo.add_user(text)

        steps = []
        turn_failed = False
        with observe.timer() as timer:
            try:
                raw_reply = run(
                    convo,
                    work,
                    trace=False,
                    steps=steps,
                    longterm=app_runtime.longterm,
                    extra=note,
                )
            except Exception as error:
                turn_failed = True
                observe.log("error", where="chat", error=type(error).__name__)
                raw_reply = (
                    "Something went wrong while processing your request. "
                    "Please try again."
                )

        safe_reply = policy.check_output(
            raw_reply,
            work,
            text,
            context=app_runtime.longterm.recall(work.customer_email, context.sid) or "",
        )
        convo.persist_safe_reply(raw_reply, safe_reply)
        app_runtime.longterm.remember(work, session_id=context.sid)

        observe.log(
            "turn",
            user=text,
            steps=len(steps),
            ms=timer.ms,
            actions=len(work.actions) - before,
            planner=payload.planner,
            cost=observe.turn_cost(turn_id),
            error=turn_failed,
        )
        app_runtime.save_sessions()

        return {
            "reply": safe_reply,
            "steps": steps if settings.expose_internal_ui else [],
            "new_actions": work.actions[before:],
            **app_runtime.public_state(chat_session),
        }
