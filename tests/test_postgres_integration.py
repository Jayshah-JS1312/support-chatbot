"""PostgreSQL schema and repository smoke tests (enabled when DATABASE_URL is set)."""

import os
import uuid

import pytest

from support_chatbot.auth import Identity, reset_identity, set_identity
from support_chatbot.config import settings
from support_chatbot.persistence import InvalidWorkflowTransition, PostgresRepository
from support_chatbot import hitl_evals
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
        "workflow_dead_letters", "action_proposals",
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
                 'auth_sessions','password_reset_tokens','action_proposals')""").fetchall()
        assert nullable == []
        assert len(rls) == 8
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
        assert repository.latest_session_id("11111111-1111-4111-8111-111111111111") == sid
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
        assert request["id"] in {row["id"] for row in repository.list_support_requests()}
    finally:
        reset_identity(identity_token)
    try:
        with system_identity():
            repository.mark_enqueued(request["id"], "msg-integration")
            assert repository.claim_drafting(request["id"], 30) is not None
            assert repository.claim_drafting(request["id"], 30) is None
            repository.save_resolution_draft(
                request["id"], "Proposed response", {"type": "send_resolution"},
                settings.approval_deadlines,
            )
            repository.decide_support_request(
                request["id"], True, "integration test",
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "Edited proposed response", True,
            )
            detail = repository.get_approval_detail(request["id"])
            assert detail["proposed_response"] == "Edited proposed response"
            assert detail["review_necessary"] is True
            assert {event["event_type"] for event in detail["audit_history"]} >= {
                "approval_created", "approval_approved",
            }
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
            connection.execute(
                "delete from public.audit_events where resource_type='support_request' and resource_id=%s",
                (str(request["id"]),),
            )
            connection.execute("delete from public.support_requests where id=%s", (request["id"],))
        repository.logout(session_token)
        repository.close()


def test_postgres_absence_policy_is_durable_and_late_approval_fails_closed():
    repository = PostgresRepository(os.environ.get("DATABASE_URL"))
    key = f"absence-{uuid.uuid4().hex}"
    request = None
    identity, session_token = repository.login("raj@example.com", "RajDemo!2026")
    identity_token = set_identity(identity)
    try:
        request, _ = repository.create_support_request("Please review this", "react", key)
    finally:
        reset_identity(identity_token)
    try:
        with system_identity():
            repository.mark_enqueued(request["id"], "absence-integration")
            repository.claim_drafting(request["id"], 30)
            repository.save_resolution_draft(
                request["id"], "Safe draft", {"type": "send_resolution"},
                settings.approval_deadlines,
            )
            with repository.connection() as connection:
                connection.execute("""update public.approval_tasks a set
                    reminder_at=now()-interval '1 second',
                    escalation_at=now()+interval '1 hour',expires_at=now()+interval '2 hours'
                    from public.resolution_drafts d where a.resolution_draft_id=d.id
                    and d.support_request_id=%s""", (request["id"],))
            assert repository.process_absence_policy()["reminded"] == [request["id"]]
            with repository.connection() as connection:
                connection.execute("""update public.approval_tasks a set
                    escalation_at=now()-interval '1 second'
                    from public.resolution_drafts d where a.resolution_draft_id=d.id
                    and d.support_request_id=%s""", (request["id"],))
            assert repository.process_absence_policy()["escalated"] == [request["id"]]
            detail = repository.get_approval_detail(request["id"])
            assert detail["queue_name"] == "supervisor"
            with repository.connection() as connection:
                connection.execute("""update public.approval_tasks a set expires_at=now()-interval '1 second'
                    from public.resolution_drafts d where a.resolution_draft_id=d.id
                    and d.support_request_id=%s""", (request["id"],))
            expiry = repository.process_absence_policy()
            # A concurrently running application instance may win the same
            # idempotent database sweep before this test process does.
            assert expiry["expired"] in ([], [request["id"]])
            assert repository.get_support_request(request["id"])["status"] == "COMPLETED_WITHOUT_ACTION"
            with pytest.raises(InvalidWorkflowTransition):
                repository.decide_support_request(
                    request["id"], True, "late", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    review_necessary=True,
                )
    finally:
        if request:
            with repository.pool.connection() as connection:
                connection.execute(
                    "delete from public.audit_events where resource_type='support_request' and resource_id=%s",
                    (str(request["id"]),),
                )
                connection.execute("delete from public.support_requests where id=%s", (request["id"],))
        repository.logout(session_token)
        repository.close()


def test_postgres_sealed_action_executes_once_after_both_approvals():
    repository = PostgresRepository(os.environ.get("DATABASE_URL"))
    key = f"sealed-{uuid.uuid4().hex}"
    request = proposal = None
    identity, session_token = repository.login("mei@example.com", "MeiDemo!2026")
    identity_token = set_identity(identity)
    try:
        proposal = repository.create_action_proposal(
            "cancel_order", {"order_id": "112-3333333-3333333"}
        )
        request, created = repository.confirm_action_proposal(
            proposal["proposal_id"], proposal["action_hash"], key
        )
        assert created is True
        assert request["status"] == "AWAITING_APPROVAL"
        assert repository.get_order("112-3333333-3333333")["status"] == "preparing"
    finally:
        reset_identity(identity_token)
    try:
        with system_identity():
            repository.decide_support_request(
                request["id"], True, "sealed action verified",
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                review_necessary=True,
            )
            assert repository.claim_execution(request["id"], 30) is not None
            first = repository.complete_execution(request["id"])
            second = repository.complete_execution(request["id"])
            assert first["status"] == second["status"] == "COMPLETED"
            assert repository.get_order("112-3333333-3333333")["status"] == "cancelled"
            with repository.connection() as connection:
                count = connection.execute(
                    "select count(*) as n from public.action_executions where idempotency_key=%s",
                    (f"support-request:{request['id']}:execute",),
                ).fetchone()["n"]
            assert count == 1
    finally:
        with repository.pool.connection() as connection:
            if request:
                connection.execute(
                    "delete from public.audit_events where resource_type='support_request' and resource_id=%s",
                    (str(request["id"]),),
                )
                connection.execute("delete from public.support_requests where id=%s", (request["id"],))
            if proposal:
                connection.execute("delete from public.action_proposals where id=%s", (proposal["proposal_id"],))
            connection.execute(
                """delete from public.order_events where order_id=(
                    select id from public.orders where order_number='112-3333333-3333333'
                ) and metadata->>'support_request_id'=%s""",
                (request["id"] if request else "",),
            )
            connection.execute(
                "update public.orders set status='preparing',version=1 where order_number='112-3333333-3333333'"
            )
        repository.logout(session_token)
        repository.close()


def test_postgres_persists_hitl_evaluation_and_reports_queue_metrics():
    repository = PostgresRepository(os.environ.get("DATABASE_URL"))
    run_id = None
    admin = Identity(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "admin@example.com",
        "Ami Admin", "admin",
    )
    identity_token = set_identity(admin)
    try:
        saved = repository.save_hitl_evaluation(hitl_evals.run())
        run_id = saved["run_id"]
        latest = repository.latest_hitl_evaluation()
        assert latest["run_id"] == run_id
        assert latest["summary"]["hitl_recall"] == 100.0
        assert len(latest["results"]) == len(hitl_evals.load_cases())
        metrics = repository.hitl_operational_metrics()
        assert {"decided", "marked_necessary", "pending", "expired",
                "escalation_precision"} <= metrics.keys()
    finally:
        if run_id:
            with repository.pool.connection() as connection:
                connection.execute("delete from public.evaluation_runs where id=%s", (run_id,))
        reset_identity(identity_token)
        repository.close()
