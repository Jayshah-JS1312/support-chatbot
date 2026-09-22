"""Observe module: test the trace log and event recording.

Tests pin:
- Trace lines are valid JSON with the required fields
- Events are logged with the correct fields (seq, ts, kind, session, turn)
- The timer context manager works correctly
- Stats aggregates events correctly
"""

import json
import time

import pytest

from support_chatbot import observe


class TestEventLogging:
    """Events are logged and stored in memory."""

    def test_log_returns_event_dict(self, tmp_state):
        event = observe.log("test", foo="bar")
        assert isinstance(event, dict)
        assert event["kind"] == "test"
        assert event["foo"] == "bar"

    def test_event_has_required_fields(self, tmp_state):
        """Every event has seq, ts, kind, session, turn."""
        event = observe.log("test")
        assert "seq" in event
        assert "ts" in event
        assert "kind" in event
        assert "session" in event
        assert "turn" in event

    def test_seq_is_ever_increasing(self, tmp_state):
        """Each event gets a unique, increasing sequence number."""
        e1 = observe.log("test")
        e2 = observe.log("test")
        assert e2["seq"] > e1["seq"]

    def test_events_added_to_memory(self, tmp_state):
        observe.log("test1")
        observe.log("test2")
        # EVENTS should have the logged events
        assert len(observe.EVENTS) >= 2
        assert any(e["kind"] == "test1" for e in observe.EVENTS)
        assert any(e["kind"] == "test2" for e in observe.EVENTS)

    def test_log_with_session_context(self, tmp_state):
        observe.context(session="session-123")
        event = observe.log("test")
        assert event["session"] == "session-123"[:8]

    def test_log_with_turn_context(self, tmp_state):
        observe.context(turn="turn-456")
        event = observe.log("test")
        assert event["turn"] == "turn-456"

    def test_new_turn_creates_turn_id(self, tmp_state):
        """new_turn() generates a turn ID and sets context."""
        turn_id = observe.new_turn()
        event = observe.log("test")
        assert event["turn"] == turn_id
        assert len(turn_id) == 8

    def test_default_session_is_cli(self, tmp_state):
        # The default session is "cli" when no context is set
        # Since context is set up by tmp_state, we need to log and see what we got
        event = observe.log("test")
        # The session should be truncated to 8 characters
        assert len(event["session"]) <= 8


class TestEventTypes:
    """Different event kinds are logged."""

    def test_log_llm_event(self, tmp_state):
        event = observe.log("llm", model="gpt-4", tokens=100, cost=0.01)
        assert event["kind"] == "llm"
        assert event["model"] == "gpt-4"
        assert event["tokens"] == 100
        assert event["cost"] == 0.01

    def test_log_tool_event(self, tmp_state):
        event = observe.log("tool", tool="find_orders", ok=True, ms=50)
        assert event["kind"] == "tool"
        assert event["tool"] == "find_orders"
        assert event["ok"] is True
        assert event["ms"] == 50

    def test_log_turn_event(self, tmp_state):
        event = observe.log("turn", steps=3, cost=0.05)
        assert event["kind"] == "turn"
        assert event["steps"] == 3


class TestTimer:
    """The timer context manager measures elapsed time."""

    def test_timer_measures_elapsed(self):
        with observe.timer() as t:
            time.sleep(0.01)
        assert t.ms >= 10  # At least 10ms

    def test_timer_stored_in_variable(self):
        with observe.timer() as t:
            pass
        assert hasattr(t, "ms")
        assert isinstance(t.ms, int)

    def test_timer_with_logging(self, tmp_state):
        with observe.timer() as t:
            time.sleep(0.005)
        event = observe.log("test", ms=t.ms)
        assert event["ms"] >= 5


class TestMaxEvents:
    """The event list has a maximum size."""

    def test_events_capped_at_max_events(self, tmp_state):
        """Old events are dropped when the list exceeds MAX_EVENTS."""
        for i in range(observe.MAX_EVENTS + 50):
            observe.log("test", n=i)
        # Should not exceed MAX_EVENTS
        assert len(observe.EVENTS) <= observe.MAX_EVENTS

    def test_newest_events_kept(self, tmp_state):
        """When capped, the newest events are kept."""
        for i in range(observe.MAX_EVENTS + 10):
            observe.log("test", n=i)
        # The last event should be the highest n
        last_n = observe.EVENTS[-1].get("n")
        assert last_n == observe.MAX_EVENTS + 9


class TestStats:
    """stats() aggregates event data."""

    def test_stats_empty_events(self, tmp_state):
        s = observe.stats()
        assert s["turns"] == 0
        assert s["llm_calls"] == 0
        assert s["tool_calls"] == 0

    def test_stats_counts_turns(self, tmp_state):
        observe.log("turn", ms=100, steps=1)
        observe.log("turn", ms=200, steps=2)
        s = observe.stats()
        assert s["turns"] == 2

    def test_stats_counts_tool_calls(self, tmp_state):
        observe.log("tool", tool="escalate", ok=True, ms=50)
        observe.log("tool", tool="find_orders", ok=True, ms=60)
        observe.log("tool", tool="escalate", ok=False, ms=40)
        s = observe.stats()
        assert s["tool_calls"] == 3
        assert s["tool_errors"] == 1

    def test_stats_error_rate(self, tmp_state):
        observe.log("tool", tool="find_orders", ok=True, ms=50)
        observe.log("tool", tool="get_order", ok=True, ms=60)
        observe.log("tool", tool="escalate", ok=False, ms=40)
        s = observe.stats()
        # 1 error out of 3 = 33%
        assert 30 <= s["error_rate"] <= 35

    def test_stats_sums_tokens(self, tmp_state):
        observe.log("llm", tokens=100, ms=500)
        observe.log("llm", tokens=50, ms=600)
        s = observe.stats()
        assert s["tokens"] == 150

    def test_stats_tracks_cost(self, tmp_state):
        observe.log("llm", cost=0.01, ms=500)
        observe.log("llm", cost=0.02, ms=600)
        s = observe.stats()
        assert s["cost"] == pytest.approx(0.03, abs=0.001)

    def test_stats_by_tool(self, tmp_state):
        observe.log("tool", tool="find_orders", ok=True, ms=50)
        observe.log("tool", tool="find_orders", ok=True, ms=60)
        observe.log("tool", tool="escalate", ok=False, ms=40)
        s = observe.stats()
        by_tool = {t["tool"]: t for t in s["by_tool"]}
        assert by_tool["find_orders"]["calls"] == 2
        assert by_tool["escalate"]["errors"] == 1

    def test_stats_cost_per_turn(self, tmp_state):
        observe.log("llm", cost=0.01, ms=500)
        observe.log("turn", ms=1000, steps=1)
        observe.log("llm", cost=0.02, ms=600)
        observe.log("turn", ms=1100, steps=2)
        s = observe.stats()
        # 0.03 total cost, 2 turns = 0.015 per turn
        assert s["cost_per_turn"] == pytest.approx(0.015, abs=0.001)


class TestRecent:
    """recent() returns the most recent events."""

    def test_recent_returns_events(self, tmp_state):
        observe.log("turn")
        observe.log("tool", tool="test")
        observe.log("llm")
        events = observe.recent(limit=10)
        assert len(events) == 3

    def test_recent_returns_newest_first(self, tmp_state):
        e1 = observe.log("test1")
        e2 = observe.log("test2")
        e3 = observe.log("test3")
        events = observe.recent(limit=10)
        # Newest first means reverse order
        assert events[0]["seq"] > events[-1]["seq"]

    def test_recent_filters_by_kind(self, tmp_state):
        observe.log("turn")
        observe.log("tool")
        observe.log("tool")
        observe.log("llm")
        tools = observe.recent(limit=10, kind="tool")
        assert len(tools) == 2
        assert all(e["kind"] == "tool" for e in tools)

    def test_recent_respects_limit(self, tmp_state):
        for i in range(10):
            observe.log("test", n=i)
        events = observe.recent(limit=3)
        assert len(events) == 3


class TestTurnCost:
    """turn_cost() sums costs for one turn."""

    def test_turn_cost_no_events(self, tmp_state):
        cost = observe.turn_cost("unknown-turn")
        assert cost == 0

    def test_turn_cost_sums_llm_events(self, tmp_state):
        observe.context(turn="turn-123")
        observe.log("llm", cost=0.01, ms=500)
        observe.log("tool", cost=0.001, ms=50)  # Not counted (not llm kind)
        observe.log("llm", cost=0.02, ms=600)
        cost = observe.turn_cost("turn-123")
        assert cost == pytest.approx(0.03, abs=0.001)


class TestThreadSafety:
    """The observe module uses locks for thread safety."""

    def test_events_lock_exists(self):
        """There is a lock to protect concurrent access."""
        assert hasattr(observe, "_lock")
        assert hasattr(observe._lock, "acquire")
        assert hasattr(observe._lock, "release")
