"""Regression tests for the live behavioral-evaluation harness."""

from evaluations import behavioral
from support_chatbot.persistence import InMemoryRepository


def test_behavioral_eval_memory_is_isolated_from_customer_database():
    longterm = behavioral.isolated_longterm_memory()

    assert isinstance(longterm.repository, InMemoryRepository)
    assert longterm.recall("raj@example.com", "eval-session") is None
