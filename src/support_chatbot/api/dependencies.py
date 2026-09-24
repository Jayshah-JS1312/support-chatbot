"""Shared FastAPI dependencies and cookie handling."""

from fastapi import HTTPException, Request, Response

from support_chatbot.config import settings
from support_chatbot.api.auth_middleware import AUTH_COOKIE


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


def require_user(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin(request: Request):
    user = require_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


def require_customer(request: Request):
    user = require_user(request)
    if user.role != "customer":
        raise HTTPException(status_code=403, detail="Customer access required")
    return user


def require_internal_ui(request: Request):
    user = require_admin(request)
    if not settings.expose_internal_ui:
        raise HTTPException(status_code=404, detail="Not found")
    return user


def set_auth_cookie(response, token):
    response.set_cookie(
        AUTH_COOKIE,
        token,
        max_age=settings.auth_session_hours * 3600,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
    )


def session(request: Request, response: Response):
    user = require_customer(request)
    context = runtime(request).get_session(request.cookies.get("sid"), user.user_id)
    set_session_cookie(response, context)
    return context
