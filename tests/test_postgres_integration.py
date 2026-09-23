"""PostgreSQL schema and repository smoke tests (enabled when DATABASE_URL is set)."""

import os
import uuid

import pytest

from support_chatbot.persistence import PostgresRepository


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
    }
    try:
        with repository.pool.connection() as connection:
            rows = connection.execute(
                "select tablename from pg_tables where schemaname='public'"
            ).fetchall()
        assert required <= {row["tablename"] for row in rows}
    finally:
        repository.close()


def test_postgres_round_trips_a_conversation():
    repository = PostgresRepository(os.environ.get("DATABASE_URL"))
    sid = f"integration-{uuid.uuid4().hex}"
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
        with repository.pool.connection() as connection:
            connection.execute(
                "delete from public.conversations where browser_session_id=%s", (sid,)
            )
        repository.close()
