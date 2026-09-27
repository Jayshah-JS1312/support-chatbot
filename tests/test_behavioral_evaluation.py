"""Regression tests for the live behavioral-evaluation harness."""

from evaluations import behavioral
from support_chatbot.persistence import InMemoryRepository


def test_behavioral_eval_memory_is_isolated_from_customer_database():
    longterm = behavioral.isolated_longterm_memory()

    assert isinstance(longterm.repository, InMemoryRepository)
    assert longterm.recall("raj@example.com", "eval-session") is None


def test_behavioral_eval_can_share_its_isolated_case_repository():
    repository = InMemoryRepository()

    longterm = behavioral.isolated_longterm_memory(repository)

    assert longterm.repository is repository
