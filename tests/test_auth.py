"""Authentication, role authorization, ownership, and session-isolation tests."""

from fastapi.testclient import TestClient

from support_chatbot import tools
from support_chatbot.api.app import create_app
from support_chatbot.api.runtime import RuntimeState
from support_chatbot.auth import Identity, reset_identity, set_identity
from support_chatbot.config import settings
from support_chatbot.persistence import get_repository


def client_for(repository):
    repository.enforce_auth = True
    return TestClient(create_app(RuntimeState(repository)))


def login(client, email="raj@example.com", password="RajDemo!2026"):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_unauthenticated_customer_admin_and_workflow_calls_fail():
    with client_for(get_repository()) as client:
        assert client.get("/state").status_code == 401
        assert client.get("/admin").status_code == 401
        assert client.post("/workflow/recover").status_code == 401


def test_customer_receives_403_on_every_admin_surface():
    with client_for(get_repository()) as client:
        assert login(client).status_code == 200
        for path in ("/admin", "/admin/approvals", "/admin/approvals.json",
                     "/monitoring", "/evals", "/admin/hitl-evals"):
            assert client.get(path).status_code == 403


def test_request_history_is_scoped_to_authenticated_customer():
    repository = get_repository()
    with client_for(repository) as client:
        assert login(client).status_code == 200
        client.get("/")
        created = client.post("/chat", json={"message": "Raj private request", "planner": "react"},
                              headers={"Idempotency-Key": "raj-private-history"})
        request_id = created.json()["request_id"]
        client.post("/auth/logout")
        assert login(client, "mei@example.com", "MeiDemo!2026").status_code == 200
        history = client.get("/requests").json()["items"]
        assert request_id not in {item["request_id"] for item in history}


def test_admin_can_open_monitoring_and_evaluations():
    original = settings.expose_internal_ui
    object.__setattr__(settings, "expose_internal_ui", True)
    try:
        with client_for(get_repository()) as client:
            assert login(client, "admin@example.com", "AdminDemo!2026").status_code == 200
            for path in ("/admin", "/monitoring", "/evals", "/admin/approvals",
                         "/admin/hitl-evals"):
                page = client.get(path)
                assert page.status_code == 200
                assert '>Logout</button>' in page.text
                assert 'href="/">Chat' not in page.text
                assert 'href="/">Back to chat' not in page.text
            chat = client.get("/", follow_redirects=False)
            assert chat.status_code == 303
            assert chat.headers["location"] == "/admin/approvals"
            assert client.get("/state").status_code == 403
            assert client.get("/requests").status_code == 403
            assert client.post("/reset", json={}).status_code == 403
    finally:
        object.__setattr__(settings, "expose_internal_ui", original)


def test_signup_cannot_assign_admin_role():
    with client_for(get_repository()) as client:
        rejected = client.post("/auth/signup", json={
            "email": "attacker@example.com", "display_name": "Attacker",
            "password": "LongEnough!2026", "role": "admin",
        })
        assert rejected.status_code == 422

        created = client.post("/auth/signup", json={
            "email": "new@example.com", "display_name": "New User",
            "password": "LongEnough!2026",
        })
        assert created.status_code == 201
        assert created.json()["user"]["role"] == "customer"


def test_bcrypt_password_byte_limit_is_a_validation_error():
    with client_for(get_repository()) as client:
        oversized = "🙂" * 19
        signup = client.post("/auth/signup", json={
            "email": "large-password@example.com",
            "display_name": "Large Password",
            "password": oversized,
        })
        assert signup.status_code == 422
        assert client.post("/auth/login", json={
            "email": "raj@example.com", "password": oversized,
        }).status_code == 422
        assert client.post("/auth/password-reset/confirm", json={
            "token": "x" * 32, "new_password": oversized,
        }).status_code == 422


def test_logout_revokes_session_and_refresh_rotates_it():
    with client_for(get_repository()) as client:
        assert login(client).status_code == 200
        before = client.cookies.get("ami_session")
        refreshed = client.post("/auth/refresh")
        assert refreshed.status_code == 200
        assert client.cookies.get("ami_session") != before
        assert client.get("/auth/me").status_code == 200
        assert client.post("/auth/logout").status_code == 200
        assert client.get("/auth/me").status_code == 401


def test_password_reset_is_single_use_and_revokes_existing_sessions():
    original = settings.expose_reset_token
    object.__setattr__(settings, "expose_reset_token", True)
    try:
        with client_for(get_repository()) as client:
            assert login(client, "mei@example.com", "MeiDemo!2026").status_code == 200
            requested = client.post(
                "/auth/password-reset/request", json={"email": "mei@example.com"}
            )
            token = requested.json()["reset_token"]
            confirmed = client.post("/auth/password-reset/confirm", json={
                "token": token, "new_password": "NewMeiPass!2026",
            })
            assert confirmed.status_code == 200
            assert client.get("/auth/me").status_code == 401
            assert client.post("/auth/password-reset/confirm", json={
                "token": token, "new_password": "AnotherPass!2026",
            }).status_code == 400
            assert login(client, "mei@example.com", "NewMeiPass!2026").status_code == 200
    finally:
        object.__setattr__(settings, "expose_reset_token", original)


def test_raj_cannot_retrieve_meis_order_by_changing_the_id():
    repository = get_repository()
    repository.enforce_auth = True
    token = set_identity(Identity(
        repository.profiles["raj@example.com"]["id"],
        "raj@example.com", "Raj", "customer",
    ))
    try:
        result = tools.get_order("112-3333333-3333333")
        assert result == {"error": "No order found with id 112-3333333-3333333."}
        assert len(tools.find_orders("mei@example.com").get("orders", [])) == 0
    finally:
        reset_identity(token)


def test_stolen_conversation_cookie_is_replaced_for_a_different_user():
    repository = get_repository()
    repository.enforce_auth = True
    runtime = RuntimeState(repository)
    raj = Identity(repository.profiles["raj@example.com"]["id"], "raj@example.com", "Raj", "customer")
    mei = Identity(repository.profiles["mei@example.com"]["id"], "mei@example.com", "Mei", "customer")
    raj_token = set_identity(raj)
    try:
        raj_session = runtime.get_session(user_id=raj.user_id)
    finally:
        reset_identity(raj_token)
    mei_token = set_identity(mei)
    try:
        attempted = runtime.get_session(raj_session.sid, mei.user_id)
    finally:
        reset_identity(mei_token)
    assert attempted.stale is True
    assert attempted.sid != raj_session.sid
