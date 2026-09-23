"""PostgreSQL schema and repository smoke tests (enabled when DATABASE_URL is set)."""

import os
import uuid

import pytest

from support_chatbot.auth import Identity, reset_identity, set_identity
from support_chatbot.persistence import PostgresRepository
from support_chatbot.workflow import system_identity


pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ,
    reason="PostgreSQL integration tests require DATABASE_URL",
)


def test_all_phase_two_tables_exist():
    repository = PostgresRepository(os.environ.get("DATABASE_URL"))
    required = {
        "profiles", "orders", "order_events", "conversations", "messages",
        "memory_facts", "memory_summaries", "support_requests",
        "resolution_drafts", "approval_tasks", "action_executions",
        "audit_events", "evaluation_runs", "evaluation_results",
        "auth_sessions", "password_reset_tokens",
        "workflow_dead_letters",
    }
    try:
        with repository.pool.connection() as connection:
            rows = connection.execute(
                "select tablename from pg_tables where schemaname='public'"
            ).fetchall()
        assert required <= {row["tablename"] for row in rows}
    finally:
        repository.close()


def test_customer_resources_require_owners_and_rls_is_enabled():
    repository = PostgresRepository(os.environ.get("DATABASE_URL"))
    try:
        with repository.pool.connection() as connection:
            nullable = connection.execute("""select table_name from information_schema.columns
                where table_schema='public' and column_name='user_id'
                and table_name in ('orders','conversations','memory_facts','memory_summaries',
                    'support_requests','action_executions','audit_events')
                and is_nullable <> 'NO'""").fetchall()
            rls = connection.execute("""select relname,relrowsecurity from pg_class
                join pg_namespace on pg_namespace.oid=pg_class.relnamespace
                where nspname='public' and relname in
                ('profiles','orders','conversations','messages','support_requests',
                 'auth_sessions','password_reset_tokens')""").fetchall()
        assert nullable == []
        assert len(rls) == 7
        assert all(row["relrowsecurity"] for row in rls)
    finally:
        repository.close()


def test_postgres_round_trips_a_conversation():
    repository = PostgresRepository(os.environ.get("DATABASE_URL"))
    sid = f"integration-{uuid.uuid4().hex}"
    identity_token = set_identity(Identity(
        "11111111-1111-4111-8111-111111111111",
        "raj@example.com", "Raj", "customer",
    ))
    try:
        repository.create_session(sid, {})
        repository.save_session(
            sid,
            [{"role": "user", "content": "hello"},
             {"role": "assistant", "content": "Hi, how can I help?"}],
            {"turn": 1},
        )
        loaded = repository.load_session(sid)
        assert loaded["history"][-1]["content"] == "Hi, how can I help?"
        assert loaded["work"]["turn"] == 1
    finally:
        reset_identity(identity_token)
        with repository.pool.connection() as connection:
            connection.execute(
                "delete from public.conversations where browser_session_id=%s", (sid,)
            )
        repository.close()


def test_postgres_rls_hides_meis_order_from_raj():
    repository = PostgresRepository(os.environ.get("DATABASE_URL"))
    identity, session_token = repository.login("raj@example.com", "RajDemo!2026")
    identity_token = set_identity(identity)
    try:
        assert repository.get_order("112-1111111-1111111") is not None
        assert repository.get_order("112-3333333-3333333") is None
        assert repository.list_orders("mei@example.com") == []
    finally:
        reset_identity(identity_token)
        repository.logout(session_token)
        repository.close()


def test_postgres_workflow_is_idempotent_and_resumable():
    repository = PostgresRepository(os.environ.get("DATABASE_URL"))
    key = f"integration-{uuid.uuid4().hex}"
    identity, session_token = repository.login("raj@example.com", "RajDemo!2026")
    identity_token = set_identity(identity)
    try:
        request, created = repository.create_support_request("Please help", "react", key)
        duplicate, duplicate_created = repository.create_support_request("Please help", "react", key)
        assert created is True and duplicate_created is False
        assert duplicate["id"] == request["id"]
    finally:
        reset_identity(identity_token)
    try:
        with system_identity():
            repository.mark_enqueued(request["id"], "msg-integration")
            assert repository.claim_drafting(request["id"], 30) is not None
            assert repository.claim_drafting(request["id"], 30) is None
            repository.save_resolution_draft(
                request["id"], "Proposed response", {"type": "send_resolution"}, 24
            )
            repository.decide_support_request(
                request["id"], True, "integration test", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            )
            assert repository.claim_execution(request["id"], 30) is not None
            first = repository.complete_execution(request["id"])
            second = repository.complete_execution(request["id"])
            assert first["status"] == second["status"] == "COMPLETED"
            with repository.connection() as connection:
                count = connection.execute(
                    "select count(*) as n from public.action_executions where idempotency_key=%s",
                    (f"support-request:{request['id']}:execute",),
                ).fetchone()["n"]
            assert count == 1
    finally:
        with repository.pool.connection() as connection:
            connection.execute("delete from public.support_requests where id=%s", (request["id"],))
        repository.logout(session_token)
        repository.close()
