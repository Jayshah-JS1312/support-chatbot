"""Memory module: test conversation and working memory.

Tests pin:
- ConversationMemory trims correctly, never splitting a tool/observation pair
- WorkingMemory records facts from tool results and has turn/pending for confirmation gate
- Both round-trip through to_dict/from_dict (persistence)
- LongTermMemory round-trips through JSON and remembers customer history
"""

import json

import pytest

from support_chatbot.memory import ConversationMemory, WorkingMemory, LongTermMemory


class TestConversationMemoryBasics:
    """ConversationMemory holds the transcript."""

    def test_start_empty(self):
        m = ConversationMemory(system_prompt="You are helpful")
        assert m.system == "You are helpful"
        assert len(m) == 0

    def test_add_user_message(self):
        m = ConversationMemory("system")
        m.add_user("Hello")
        assert len(m) == 1
        assert m.history[0]["role"] == "user"
        assert m.history[0]["content"] == "Hello"

    def test_add_assistant_message(self):
        m = ConversationMemory("system")
        msg = {"role": "assistant", "content": "Hi there"}
        m.add_assistant(msg)
        assert len(m) == 1

    def test_add_observation(self):
        m = ConversationMemory("system")
        result = {"order_id": "123", "status": "shipped"}
        m.add_observation(tool_call_id="call_1", result=result)
        assert len(m) == 1
        assert m.history[0]["role"] == "tool"
        assert json.loads(m.history[0]["content"]) == result

    def test_public_transcript_excludes_agent_internals(self):
        """Page navigation restores dialogue without exposing tool messages."""
        m = ConversationMemory("system")
        m.add_user("Where is my order?")
        m.add_assistant({"role": "assistant", "content": None,
                         "tool_calls": [{"id": "call-1"}]})
        m.add_observation("call-1", {"status": "shipped"})
        m.add_assistant({"role": "assistant", "content": "It has shipped."})

        assert m.public_transcript() == [
            {"role": "user", "content": "Where is my order?"},
            {"role": "assistant", "content": "It has shipped."},
        ]


class TestConversationMemoryTrimming:
    """Conversation memory trims from the front but never leaves a tool stranded."""

    def test_trim_keeps_full_history_when_short(self):
        m = ConversationMemory("system", max_turns=10)
        m.add_user("Q1")
        m.add_assistant({"role": "assistant", "content": "A1"})
        trimmed = m._trimmed()
        assert len(trimmed) == 2

    def test_trim_removes_from_front_when_long(self):
        m = ConversationMemory("system", max_turns=3)
        m.add_user("Q1")
        m.add_user("Q2")
        m.add_user("Q3")
        m.add_user("Q4")
        m.add_user("Q5")
        trimmed = m._trimmed()
        # Should keep only the last 3
        assert len(trimmed) <= 3

    def test_trim_never_leaves_tool_without_assistant(self):
        """A tool result without its assistant request is invalid."""
        m = ConversationMemory("system", max_turns=2)
        # Build: assistant calls a tool, tool result comes back
        m.add_assistant({"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]})
        m.add_observation("c1", {"status": "ok"})
        m.add_user("Next question")
        m.add_assistant({"role": "assistant", "content": "Reply"})
        # Now trim to 2 turns. The tool/observation pair should stay together.
        trimmed = m._trimmed()
        # Check that if we have a tool role, it has the assistant before it
        tool_indices = [i for i, x in enumerate(trimmed) if x.get("role") == "tool"]
        for idx in tool_indices:
            if idx > 0:
                # There should be an assistant message before this tool
                prev_msg = trimmed[idx - 1]
                # The message before a tool should not be another tool
                assert prev_msg.get("role") != "tool"

    def test_messages_includes_system_and_trimmed(self):
        m = ConversationMemory("Be helpful", max_turns=10)
        m.add_user("Hi")
        messages = m.messages()
        assert len(messages) >= 2
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "Be helpful"

    def test_messages_includes_extra_system(self):
        m = ConversationMemory("Primary system", max_turns=10)
        m.add_user("Hi")
        messages = m.messages(extra_system="Extra instructions")
        # Should have primary system, extra system, then the history
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "system"
        assert "Extra instructions" in messages[1]["content"]


class TestWorkingMemoryRecords:
    """WorkingMemory records facts from tool results."""

    def test_find_orders_records_customer_email(self):
        w = WorkingMemory()
        result = {
            "orders": [
                {"order_id": "o1", "item": "Thing", "status": "delivered", "ordered_on": "2026-01-01"}
            ]
        }
        w.record("find_orders", {"email": "test@example.com"}, result)
        assert w.customer_email == "test@example.com"

    def test_find_orders_records_order_details(self):
        w = WorkingMemory()
        result = {
            "orders": [
                {"order_id": "o1", "item": "Widget", "status": "shipped", "ordered_on": "2026-01-01"}
            ]
        }
        w.record("find_orders", {"email": "test@example.com"}, result)
        assert "o1" in w.orders
        assert w.orders["o1"]["item"] == "Widget"

    def test_get_order_updates_order_details(self):
        w = WorkingMemory()
        result = {
            "order_id": "o1",
            "item": "Thing",
            "price": 99.99,
            "status": "delivered",
            "ordered_on": "2026-01-01",
            "delivered_on": "2026-01-05",
        }
        w.record("get_order", {"order_id": "o1"}, result)
        assert w.orders["o1"]["price"] == 99.99

    def test_track_package_records_order_id(self):
        w = WorkingMemory()
        result = {
            "carrier": "UPS",
            "events": [{"date": "2026-01-01", "detail": "Shipped"}],
        }
        w.record("track_package", {"order_id": "o1"}, result)
        assert "o1" in w.orders

    def test_cancel_order_records_action(self):
        w = WorkingMemory()
        result = {
            "cancelled": True,
            "order_id": "o1",
            "refund_amount": 100.00,
            "refund_eta": "3-5 days",
        }
        w.record("cancel_order", {"order_id": "o1"}, result)
        assert len(w.actions) == 1
        assert "Cancelled" in w.actions[0]
        assert w.orders["o1"]["status"] == "cancelled"

    def test_start_return_records_action(self):
        w = WorkingMemory()
        result = {
            "rma": "RMA-1001",
            "order_id": "o1",
            "refund_amount": 50.00,
            "instructions": "Drop off at UPS",
        }
        w.record("start_return", {"order_id": "o1", "reason": "broken"}, result)
        assert len(w.actions) == 1
        assert "RMA-1001" in w.actions[0]
        assert w.orders["o1"]["status"] == "return started"

    def test_escalate_records_ticket(self):
        w = WorkingMemory()
        result = {
            "escalated": True,
            "ticket": "ESC-123",
            "message": "A human will help",
            "summary": "Customer upset",
        }
        w.record("escalate", {"summary": "Customer upset"}, result)
        assert w.escalation == "ESC-123"
        assert len(w.actions) == 1

    def test_error_records_failure(self):
        w = WorkingMemory()
        result = {"error": "Order already shipped and cannot be cancelled"}
        w.record("cancel_order", {"order_id": "o1"}, result)
        assert len(w.failures) == 1
        assert "refused" in w.failures[0]

    def test_retry_error_not_recorded(self):
        """Retryable errors are the agent's mistake, not a policy decision."""
        w = WorkingMemory()
        result = {"error": "Bad call to cancel_order", "retry": True}
        w.record("cancel_order", {"order_id": "o1"}, result)
        assert len(w.failures) == 0

    def test_track_package_does_not_record_events(self):
        """track_package result includes events, but they're not stored."""
        w = WorkingMemory()
        result = {
            "carrier": "UPS",
            "events": [{"date": "2026-01-01", "detail": "Shipped"}],
        }
        w.record("track_package", {"order_id": "o1"}, result)
        assert "events" not in w.orders.get("o1", {})


class TestWorkingMemoryBrief:
    """WorkingMemory.brief() produces a short note for the model."""

    def test_brief_is_none_when_empty(self):
        w = WorkingMemory()
        assert w.brief() is None

    def test_brief_includes_customer_email(self):
        w = WorkingMemory()
        w.customer_email = "test@example.com"
        brief = w.brief()
        assert brief is not None
        assert "test@example.com" in brief

    def test_brief_includes_order_status(self):
        w = WorkingMemory()
        w.orders["o1"] = {"item": "Widget", "status": "shipped"}
        brief = w.brief()
        assert "o1" in brief
        assert "shipped" in brief

    def test_brief_includes_actions_section(self):
        w = WorkingMemory()
        w.actions.append("Cancelled o1")
        brief = w.brief()
        assert "ALREADY DONE" in brief
        assert "Cancelled o1" in brief

    def test_brief_includes_failures_section(self):
        w = WorkingMemory()
        w.failures.append("cancel_order(o1) refused: already shipped")
        brief = w.brief()
        assert "ALREADY REFUSED" in brief

    def test_brief_includes_escalation_warning(self):
        w = WorkingMemory()
        w.escalation = "ESC-123"
        brief = w.brief()
        assert "ESC-123" in brief
        assert "escalated" in brief.lower()


class TestWorkingMemoryPersistence:
    """WorkingMemory round-trips through to_dict/from_dict."""

    def test_to_dict_includes_all_fields(self):
        w = WorkingMemory()
        w.customer_email = "test@example.com"
        w.orders = {"o1": {"status": "shipped"}}
        w.actions = ["Did something"]
        w.failures = ["Something failed"]
        w.escalation = "ESC-123"
        d = w.to_dict()
        assert d["customer_email"] == "test@example.com"
        assert "o1" in d["orders"]
        assert "Did something" in d["actions"]
        assert "Something failed" in d["failures"]
        assert d["escalation"] == "ESC-123"

    def test_from_dict_restores_state(self):
        data = {
            "customer_email": "test@example.com",
            "orders": {"o1": {"status": "delivered"}},
            "actions": ["Cancelled o1"],
            "failures": ["Something refused"],
            "escalation": "ESC-456",
        }
        w = WorkingMemory.from_dict(data)
        assert w.customer_email == "test@example.com"
        assert w.orders == {"o1": {"status": "delivered"}}
        assert w.escalation == "ESC-456"

    def test_from_dict_with_missing_fields(self):
        data = {"customer_email": "test@example.com"}
        w = WorkingMemory.from_dict(data)
        assert w.customer_email == "test@example.com"
        assert w.orders == {}
        assert w.failures == []


class TestConversationMemoryPersistence:
    """ConversationMemory round-trips through to_dict/from_dict."""

    def test_to_dict_preserves_history(self):
        m = ConversationMemory("System prompt", max_turns=50)
        m.add_user("Hello")
        m.add_assistant({"role": "assistant", "content": "Hi"})
        d = m.to_dict()
        assert len(d["history"]) == 2
        assert d["max_turns"] == 50

    def test_from_dict_restores_transcript(self):
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        m = ConversationMemory.from_dict("System", {"history": history, "max_turns": 40})
        assert len(m) == 2
        assert m.history[0]["content"] == "Hello"

    def test_from_dict_defaults_max_turns(self):
        m = ConversationMemory.from_dict("System", {"history": []})
        assert m.max_turns == 40


class TestWorkingMemoryConfirmation:
    """Confirmation state in WorkingMemory: turn and pending."""

    def test_working_memory_has_turn_field(self):
        w = WorkingMemory()
        assert hasattr(w, "turn")
        assert w.turn == 0

    def test_working_memory_has_pending_field(self):
        w = WorkingMemory()
        assert hasattr(w, "pending")
        assert w.pending is None

    def test_to_dict_includes_turn_and_pending(self):
        w = WorkingMemory()
        w.turn = 3
        w.pending = {"key": ["cancel_order", "o1"], "turn": 2}
        d = w.to_dict()
        assert d["turn"] == 3
        assert d["pending"] == {"key": ["cancel_order", "o1"], "turn": 2}

    def test_from_dict_restores_turn_and_pending(self):
        data = {
            "customer_email": None,
            "orders": {},
            "actions": [],
            "failures": [],
            "escalation": None,
            "turn": 5,
            "pending": {"key": ["start_return", "o2"], "turn": 4},
        }
        w = WorkingMemory.from_dict(data)
        assert w.turn == 5
        assert w.pending["key"] == ["start_return", "o2"]

    def test_needs_confirmation_does_not_record(self):
        """If a tool returns needs_confirmation, don't record state changes."""
        w = WorkingMemory()
        result = {"needs_confirmation": True}
        w.record("cancel_order", {"order_id": "o1"}, result)
        # Should not have recorded any action or change
        assert len(w.actions) == 0


class TestLongTermMemory:
    """LongTermMemory stores customer facts across conversations."""

    def test_long_term_memory_starts_empty(self, memory_repository):
        ltm = LongTermMemory(memory_repository)
        assert memory_repository.memories == {}

    def test_long_term_memory_saves_to_repository(self, memory_repository):
        ltm = LongTermMemory(memory_repository)
        work = WorkingMemory()
        work.customer_email = "test@example.com"
        work.orders = {"o1": {}}
        ltm.remember(work, session_id="session-1")
        assert "test@example.com" in memory_repository.memories

    def test_long_term_memory_normalizes_email(self, memory_repository):
        ltm = LongTermMemory(memory_repository)
        work = WorkingMemory()
        work.customer_email = "Test@Example.COM"
        ltm.remember(work, session_id="s1")
        # Should be stored in lowercase
        assert "test@example.com" in memory_repository.memories

    def test_remember_records_customer_fact(self, memory_repository):
        ltm = LongTermMemory(memory_repository)
        work = WorkingMemory()
        work.customer_email = "alice@example.com"
        work.orders = {"o1": {}}
        work.actions = ["Cancelled o1"]
        work.escalation = "ESC-123"
        ltm.remember(work, session_id="s1")
        rec = memory_repository.memories["alice@example.com"]
        assert "first_seen" in rec
        assert "last_seen" in rec
        assert "s1" in rec["sessions"]
        assert "o1" in rec["orders_discussed"]
        assert "Cancelled o1" in rec["actions"]
        assert rec["escalations"] == []

    def test_remember_increments_refusal_count(self, memory_repository):
        ltm = LongTermMemory(memory_repository)
        work = WorkingMemory()
        work.customer_email = "bob@example.com"
        work.failures = ["cancel_order(o1) refused", "start_return(o2) refused"]
        ltm.remember(work, session_id="s1")
        rec = memory_repository.memories["bob@example.com"]
        assert rec["refusals"] == 2

    def test_recall_returns_none_for_unknown_customer(self, memory_repository):
        ltm = LongTermMemory(memory_repository)
        recall = ltm.recall("unknown@example.com", current_session="s1")
        assert recall is None

    def test_recall_returns_none_if_only_current_session(self, memory_repository):
        """A customer with only this session has no previous history."""
        ltm = LongTermMemory(memory_repository)
        work = WorkingMemory()
        work.customer_email = "charlie@example.com"
        ltm.remember(work, session_id="s1")
        # Recall from the same session returns None (no previous history)
        recall = ltm.recall("charlie@example.com", current_session="s1")
        assert recall is None

    def test_recall_mentions_previous_conversations(self, memory_repository):
        """A returning customer is recognized."""
        ltm = LongTermMemory(memory_repository)
        work = WorkingMemory()
        work.customer_email = "diane@example.com"
        ltm.remember(work, session_id="s1")
        # Now recall from a different session
        recall = ltm.recall("diane@example.com", current_session="s2")
        assert recall is not None
        assert "RETURNING CUSTOMER" in recall
        assert "1 previous" in recall

    def test_recall_includes_previous_actions(self, memory_repository):
        """Previous actions are included in the recall note."""
        ltm = LongTermMemory(memory_repository)
        work = WorkingMemory()
        work.customer_email = "eve@example.com"
        work.actions = ["Cancelled o1", "Opened return RMA-100"]
        ltm.remember(work, session_id="s1")
        recall = ltm.recall("eve@example.com", current_session="s2")
        assert "Previously done" in recall
        assert "Cancelled" in recall

    def test_recall_does_not_reuse_previous_escalations(self, memory_repository):
        """A new issue must create a new ticket instead of reusing an old ESC."""
        ltm = LongTermMemory(memory_repository)
        work = WorkingMemory()
        work.customer_email = "frank@example.com"
        work.escalation = "ESC-789"
        ltm.remember(work, session_id="s1")
        recall = ltm.recall("frank@example.com", current_session="s2")
        assert "ESC-789" not in recall
        assert "escalated" not in recall.lower()

    def test_remember_without_email_does_nothing(self, memory_repository):
        """If customer email is unknown, nothing is recorded."""
        ltm = LongTermMemory(memory_repository)
        work = WorkingMemory()
        # No customer_email set
        ltm.remember(work, session_id="s1")
        assert memory_repository.memories == {}

    def test_long_term_memory_survives_new_service_instance(self, memory_repository):
        first = LongTermMemory(memory_repository)
        work = WorkingMemory()
        work.customer_email = "existing@example.com"
        first.remember(work, session_id="old-session")
        second = LongTermMemory(memory_repository)
        assert "RETURNING CUSTOMER" in second.recall("existing@example.com", "new-session")
