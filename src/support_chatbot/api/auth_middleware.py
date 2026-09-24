"""Resolve opaque authentication cookies before route authorization runs."""

from http.cookies import SimpleCookie

from anyio import to_thread

from support_chatbot.auth import reset_identity, set_identity


CUSTOMER_AUTH_COOKIE = "ami_customer_session"
ADMIN_AUTH_COOKIE = "ami_admin_session"
# Compatibility name for callers that only need to identify the customer cookie.
AUTH_COOKIE = CUSTOMER_AUTH_COOKIE


def auth_cookie_for_scope(scope):
    """Keep customer and operator identities isolated in the same browser."""
    path = scope.get("path", "")
    query = scope.get("query_string", b"").decode("latin-1")
    admin_path = path.startswith((
        "/admin", "/monitoring", "/evals", "/metrics", "/logs", "/trace",
    ))
    admin_auth_action = (path.startswith("/auth/") or path == "/login") and "role=admin" in query
    return ADMIN_AUTH_COOKIE if admin_path or admin_auth_action else CUSTOMER_AUTH_COOKIE


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
        cookie_name = auth_cookie_for_scope(scope)
        identity = None
        path = scope.get("path", "")
        protected_admin_path = path.startswith((
            "/admin", "/monitoring", "/evals", "/metrics", "/logs", "/trace",
        ))
        # Customer routes never inherit an admin identity. On a protected
        # admin route only, a customer cookie may be inspected after the admin
        # cookie so authorization returns 403 rather than pretending the
        # authenticated customer is anonymous.
        candidates = ((ADMIN_AUTH_COOKIE, CUSTOMER_AUTH_COOKIE)
                      if protected_admin_path else (cookie_name,))
        for candidate in candidates:
            morsel = cookie.get(candidate)
            if not morsel:
                continue
            try:
                identity = await to_thread.run_sync(
                    scope["app"].state.runtime.repository.authenticate,
                    morsel.value,
                )
            except Exception:
                identity = None
            if identity:
                break
        scope.setdefault("state", {})["user"] = identity
        token = set_identity(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_identity(token)
