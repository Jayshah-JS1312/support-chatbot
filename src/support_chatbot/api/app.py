"""FastAPI application factory and transport-level error handling."""

import re
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from support_chatbot import observe
from support_chatbot.api.routes import (
    admin,
    authentication,
    customer,
    observability,
    workflow_callbacks,
)
from support_chatbot.api.runtime import RuntimeState
from support_chatbot.api.auth_middleware import AuthenticationMiddleware
from support_chatbot.config import settings
from support_chatbot.workflow import configure_upstash_workflow


_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def _error(request, status, code, message, details=None):
    body = {
        "error": {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
        }
    }
    if details:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status)


class RequestBoundaryMiddleware:
    """Apply request identifiers and enforce the body limit before routing."""

    def __init__(self, app, max_request_bytes):
        self.app = app
        self.max_request_bytes = max_request_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"x-request-id", b"").decode("ascii", "ignore")
        request_id = supplied if _SAFE_REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (b"x-request-id", request_id.encode("ascii"))
                )
            await send(message)

        declared = headers.get(b"content-length")
        if declared:
            try:
                length = int(declared)
            except ValueError:
                length = -1
            if length < 0:
                response = JSONResponse(
                    {
                        "error": {
                            "code": "invalid_content_length",
                            "message": "Content-Length must be a non-negative integer",
                            "request_id": request_id,
                        }
                    },
                    status_code=400,
                )
                await response(scope, receive, send_with_request_id)
                return
            if length > self.max_request_bytes:
                response = JSONResponse(
                    {
                        "error": {
                            "code": "request_too_large",
                            "message": "Request body exceeds the configured limit",
                            "request_id": request_id,
                        }
                    },
                    status_code=413,
                )
                await response(scope, receive, send_with_request_id)
                return

        if scope.get("method") in {"POST", "PUT", "PATCH"}:
            buffered = []
            total = 0
            while True:
                message = await receive()
                buffered.append(message)
                if message["type"] == "http.disconnect":
                    break
                if message["type"] != "http.request":
                    continue
                total += len(message.get("body", b""))
                if total > self.max_request_bytes:
                    response = JSONResponse(
                        {
                            "error": {
                                "code": "request_too_large",
                                "message": "Request body exceeds the configured limit",
                                "request_id": request_id,
                            }
                        },
                        status_code=413,
                    )
                    await response(scope, receive, send_with_request_id)
                    return
                if not message.get("more_body", False):
                    break

            async def replay_receive():
                if buffered:
                    return buffered.pop(0)
                return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, replay_receive, send_with_request_id)
            return

        await self.app(scope, receive, send_with_request_id)


def create_app(runtime_state=None):
    app = FastAPI(
        title="Ami Customer Support Agent",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )
    app.state.runtime = runtime_state or RuntimeState()
    app.add_middleware(
        RequestBoundaryMiddleware,
        max_request_bytes=settings.max_request_bytes,
    )
    app.add_middleware(AuthenticationMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        details = [
            {
                "location": [str(item) for item in issue["loc"]],
                "message": issue["msg"],
                "type": issue["type"],
            }
            for issue in error.errors()
        ]
        return _error(
            request,
            422,
            "validation_error",
            "Request validation failed",
            details,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        code = "not_found" if error.status_code == 404 else "http_error"
        return _error(request, error.status_code, code, str(error.detail))

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, error: Exception):
        observe.log("error", where="http", error=type(error).__name__)
        return _error(
            request,
            500,
            "internal_error",
            "The request could not be completed",
        )

    app.include_router(customer.router)
    app.include_router(authentication.router)
    app.include_router(admin.router)
    app.include_router(workflow_callbacks.router)
    app.include_router(observability.router)
    app.include_router(observability.evaluation_router)
    configure_upstash_workflow(app)
    return app
