"""Signup, login, logout, refresh, and password-reset routes."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from support_chatbot import UI_DIR
from support_chatbot.api.auth_middleware import AUTH_COOKIE
from support_chatbot.api.dependencies import require_user, runtime, set_auth_cookie
from support_chatbot.api.models import (
    LoginRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    SignupRequest,
)
from support_chatbot.config import settings
from support_chatbot.persistence import AuthenticationError, DuplicateEmailError


router = APIRouter(tags=["authentication"])
LOGIN_PAGE = (UI_DIR / "login.html").read_text()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if getattr(request.state, "user", None):
        return HTMLResponse('<meta http-equiv="refresh" content="0;url=/">')
    return HTMLResponse(LOGIN_PAGE)


@router.post("/auth/signup", status_code=201)
def signup(payload: SignupRequest, request: Request, response: Response):
    try:
        user, token = runtime(request).repository.signup(
            payload.email, payload.display_name, payload.password
        )
    except DuplicateEmailError as error:
        raise HTTPException(status_code=409, detail="An account with this email already exists") from error
    set_auth_cookie(response, token)
    return {"user": user.public()}


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request, response: Response):
    try:
        user, token = runtime(request).repository.login(payload.email, payload.password)
    except AuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    set_auth_cookie(response, token)
    return {"user": user.public()}


@router.post("/auth/logout")
def logout(request: Request, response: Response, user=Depends(require_user)):
    del user
    runtime(request).repository.logout(request.cookies.get(AUTH_COOKIE))
    response.delete_cookie(AUTH_COOKIE, path="/")
    response.delete_cookie("sid", path="/")
    return {"ok": True}


@router.post("/auth/refresh")
def refresh(request: Request, response: Response, user=Depends(require_user)):
    del user
    try:
        identity, token = runtime(request).repository.refresh_session(
            request.cookies.get(AUTH_COOKIE)
        )
    except AuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    set_auth_cookie(response, token)
    return {"user": identity.public()}


@router.get("/auth/me")
def me(user=Depends(require_user)):
    return {"user": user.public()}


@router.post("/auth/password-reset/request")
def request_password_reset(payload: PasswordResetRequest, request: Request):
    token = runtime(request).repository.request_password_reset(payload.email)
    result = {"ok": True, "message": "If the account exists, reset instructions have been created."}
    if settings.expose_reset_token and token:
        result["reset_token"] = token
    return result


@router.post("/auth/password-reset/confirm")
def confirm_password_reset(payload: PasswordResetConfirmRequest, request: Request):
    try:
        runtime(request).repository.reset_password(payload.token, payload.new_password)
    except AuthenticationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "message": "Password updated. Please sign in again."}
