"""Shared FastAPI dependencies and cookie handling."""

from fastapi import HTTPException, Request, Response

from support_chatbot.config import settings


def runtime(request: Request):
    return request.app.state.runtime


def set_session_cookie(response, context):
    if context.created:
        response.set_cookie(
            "sid",
            context.sid,
            path="/",
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
        )


def session(request: Request, response: Response):
    context = runtime(request).get_session(request.cookies.get("sid"))
    set_session_cookie(response, context)
    return context


def require_internal_ui():
    if not settings.expose_internal_ui:
        raise HTTPException(status_code=404, detail="Not found")
