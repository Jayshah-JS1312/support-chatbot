"""Fixtures shared by every test in this folder.

A unit test here never calls the model, never downloads anything, and never
touches the real state/ or .cache/ folders:

    fresh_store   the fake order database, restored around each test
    tmp_state     state/ and .cache/ redirected to a temp folder (always on)
    fake_llm      a scripted stand-in for llm.complete (see fakes.py)
"""

import sys

import pytest

from support_chatbot import observe, store
from support_chatbot.persistence import InMemoryRepository, get_repository
from tests.fakes import FakeLLM


@pytest.fixture
def fresh_store():
    """The autouse repository fixture already provides a clean seed."""
    yield get_repository()


@pytest.fixture(autouse=True)
def tmp_state(monkeypatch, tmp_path):
    """Point every on-disk path at a temp folder, for every test.

    observe writes state/trace.jsonl, knowledge builds .cache/chroma, and
    Customer/business memory uses the injected repository; only trace and
    retrieval-cache paths are patched here.
    """
    monkeypatch.setattr("support_chatbot.observe.LOGFILE", tmp_path / "state" / "trace.jsonl")
    monkeypatch.setattr("support_chatbot.knowledge.STORE", tmp_path / ".cache" / "chroma")
    observe.EVENTS.clear()
    monkeypatch.setattr("support_chatbot.observe.SEQ", 0)
    repository = InMemoryRepository()
    previous = store.set_repository(repository)
    yield tmp_path
    store.set_repository(previous)


@pytest.fixture
def memory_repository():
    return get_repository()


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace llm.complete everywhere it was imported with a scripted fake."""
    fake = FakeLLM()
    for name, module in list(sys.modules.items()):
        if name == "support_chatbot.llm" or (name.startswith("support_chatbot.") and hasattr(module, "complete")):
            monkeypatch.setattr(module, "complete", fake)
    return fake
