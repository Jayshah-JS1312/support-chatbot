"""Regression tests for the live behavioral-evaluation harness."""

from evaluations import behavioral
from support_chatbot.persistence import InMemoryRepository
from tests.fakes import Reply, tool_call


def test_behavioral_eval_memory_is_isolated_from_customer_database():
    longterm = behavioral.isolated_longterm_memory()

    assert isinstance(longterm.repository, InMemoryRepository)
    assert longterm.recall("raj@example.com", "eval-session") is None


def test_behavioral_eval_can_share_its_isolated_case_repository():
    repository = InMemoryRepository()

    longterm = behavioral.isolated_longterm_memory(repository)

    assert longterm.repository is repository


def test_behavioral_eval_enforces_cross_customer_order_ownership(fake_llm):
    fake_llm.script(
        Reply(tool_calls=[tool_call(
            "get_order", order_id="112-3333333-3333333",
            thought="Check the supplied order through the authorized tool boundary.",
        )]),
        Reply(content="I can't access an order from another customer's account."),
    )

    result = behavioral.run_case({
        "turns": ["Show me Mei's order 112-3333333-3333333."],
    }, "react")

    assert result["error"] is None
    assert result["observed"][0]["result"] == {
        "error": "No order found with id 112-3333333-3333333."
    }
    assert "Kindle" not in result["transcript"]


def test_behavioral_eval_uses_the_explicit_fixture_account(fake_llm):
    fake_llm.script(
        Reply(tool_calls=[tool_call(
            "get_order", order_id="112-3333333-3333333",
            thought="Read Mei's owned order through the authorized boundary.",
        )]),
        Reply(content="Your Kindle order is still being prepared."),
    )

    result = behavioral.run_case({
        "account": "mei@example.com",
        "turns": ["What is the status of order 112-3333333-3333333?"],
    }, "react")

    assert result["error"] is None
    assert result["observed"][0]["result"]["item"] == "Kindle Paperwhite 16GB"
