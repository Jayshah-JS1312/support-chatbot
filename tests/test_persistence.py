"""Durability contracts shared by PostgreSQL and the deterministic test repository."""

import pytest

from support_chatbot.api.runtime import RuntimeState
from support_chatbot.persistence import ConcurrentUpdateError, InMemoryRepository


def test_conversation_is_restored_by_a_new_runtime_instance():
    repository = InMemoryRepository()
    first = RuntimeState(repository)
    context = first.get_session()
    context.session["convo"].add_user("Where is my order?")
    context.session["convo"].add_assistant(
        {"role": "assistant", "content": "I can check that."}
    )
    first.save_sessions()

    restarted = RuntimeState(repository)
    restored = restarted.get_session(context.sid)

    assert restored.stale is False
    assert restored.created is False
    assert restored.session["convo"].public_transcript()[-1]["content"] == "I can check that."


def test_optimistic_version_rejects_a_stale_order_update():
    repository = InMemoryRepository()
    order_id = "112-3333333-3333333"
    stale_version = repository.get_order(order_id)["version"]

    repository.cancel_order(order_id, stale_version)

    with pytest.raises(ConcurrentUpdateError):
        repository.cancel_order(order_id, stale_version)
