"""Resolve opaque authentication cookies before route authorization runs."""

from http.cookies import SimpleCookie

from anyio import to_thread

from support_chatbot.auth import reset_identity, set_identity


AUTH_COOKIE = "ami_session"


class AuthenticationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        cookie = SimpleCookie()
        cookie.load(headers.get(b"cookie", b"").decode("latin-1"))
        morsel = cookie.get(AUTH_COOKIE)
        identity = None
        if morsel:
            try:
                identity = await to_thread.run_sync(
                    scope["app"].state.runtime.repository.authenticate,
                    morsel.value,
                )
            except Exception:
                identity = None
        scope.setdefault("state", {})["user"] = identity
        token = set_identity(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_identity(token)
