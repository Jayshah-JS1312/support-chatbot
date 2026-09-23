"""Store module: test seed data, status transitions, and guardrails.

The store.py module provides a fake order database with seed data and
manages returns. Tests pin the seed state and verify that status transitions
work correctly.
"""

from datetime import date, timedelta

import pytest

from support_chatbot import store, tools


class TestSeedData:
    """The initial orders in store.ORDERS."""

    def test_has_four_seed_orders(self, fresh_store):
        assert len(store.ORDERS) == 4

    def test_delivered_order_is_complete(self, fresh_store):
        """Order 112-1111111-1111111 (raj, Sony headphones, delivered)."""
        order = store.ORDERS["112-1111111-1111111"]
        assert order["email"] == "raj@example.com"
        assert "Sony WH-1000XM5" in order["item"]
        assert order["status"] == "delivered"
        assert order["delivered_on"] is not None

    def test_shipped_order_has_eta(self, fresh_store):
        """Order 112-2222222-2222222 (raj, Instant Pot, shipped)."""
        order = store.ORDERS["112-2222222-2222222"]
        assert order["status"] == "shipped"
        assert order["delivered_on"] is None
        assert "eta" in order

    def test_preparing_order_is_cancellable(self, fresh_store):
        """Order 112-3333333-3333333 (mei, Kindle, preparing)."""
        order = store.ORDERS["112-3333333-3333333"]
        assert order["status"] == "preparing"
        assert order["delivered_on"] is None

    def test_old_order_is_past_return_window(self, fresh_store):
        """Order 112-4444444-4444444 (mei, Logitech, delivered 64 days ago)."""
        order = store.ORDERS["112-4444444-4444444"]
        assert order["status"] == "delivered"
        days_since = (date.today() - date.fromisoformat(order["delivered_on"])).days
        assert days_since > store.RETURN_WINDOW_DAYS


class TestCancelGuardrail:
    """Cancellation has strict guardrails: only before it ships."""

    def test_cancel_preparing_order_creates_preview(self, fresh_store):
        """A preparing order can be previewed without changing its state."""
        result = tools.cancel_order("112-3333333-3333333")
        assert result["proposal"] is True
        assert result["action"] == "cancel_order"
        assert result["consequences"]["refund_amount"] == 149.99
        assert store.ORDERS["112-3333333-3333333"]["status"] == "preparing"

    def test_cancel_shipped_order_is_refused(self, fresh_store):
        """A shipped order cannot be cancelled — it must be returned."""
        result = tools.cancel_order("112-2222222-2222222")
        assert "error" in result
        assert "already shipped" in result["error"]
        # Verify the order status did not change
        assert store.ORDERS["112-2222222-2222222"]["status"] == "shipped"

    def test_cancel_delivered_order_is_refused(self, fresh_store):
        """A delivered order cannot be cancelled."""
        result = tools.cancel_order("112-1111111-1111111")
        assert "error" in result
        assert "already delivered" in result["error"]

    def test_cancel_nonexistent_order_is_refused(self, fresh_store):
        """Asking for a nonexistent order returns an error."""
        result = tools.cancel_order("111-9999999-9999999")
        assert "error" in result
        assert "No order found" in result["error"]

    def test_cancel_already_cancelled_order_is_refused(self, fresh_store):
        """Once cancelled, it cannot be cancelled again."""
        fresh_store.orders["112-3333333-3333333"]["status"] = "cancelled"
        result = tools.cancel_order("112-3333333-3333333")
        assert "error" in result
        assert "already cancelled" in result["error"]


class TestReturnGuardrail:
    """Returns need a delivered order inside the 30-day window."""

    def test_return_delivered_order_creates_preview(self, fresh_store):
        """An eligible return can be previewed without changing its state."""
        result = tools.start_return("112-1111111-1111111", "item is broken")
        assert result["proposal"] is True
        assert result["action"] == "start_return"
        assert result["consequences"]["refund_amount"] == 348.00
        assert store.ORDERS["112-1111111-1111111"]["status"] == "delivered"

    def test_return_nondelivered_order_is_refused(self, fresh_store):
        """A shipped (not delivered) order cannot be returned."""
        result = tools.start_return("112-2222222-2222222", "wrong size")
        assert "error" in result
        assert "not delivered" in result["error"]

    def test_return_preparing_order_is_refused(self, fresh_store):
        """A preparing order cannot be returned."""
        result = tools.start_return("112-3333333-3333333", "changed mind")
        assert "error" in result
        assert "not delivered" in result["error"]

    def test_return_past_window_is_refused(self, fresh_store):
        """A delivered order outside the 30-day window is refused."""
        result = tools.start_return("112-4444444-4444444", "never worked")
        assert "error" in result
        assert "past the" in result["error"]
        assert "30-day" in result["error"]

    def test_return_nonexistent_order_is_refused(self, fresh_store):
        """Asking for a nonexistent order returns an error."""
        result = tools.start_return("111-9999999-9999999", "test")
        assert "error" in result
        assert "No order found" in result["error"]

    def test_repeated_return_previews_have_stable_hash(self, fresh_store):
        """Equivalent previews are sealed to the same canonical action."""
        result1 = tools.start_return("112-1111111-1111111", "broken")
        result2 = tools.start_return("112-1111111-1111111", "broken")
        assert result1["action_hash"] == result2["action_hash"]
        assert store.RETURNS == {}


class TestReturnWindowDays:
    """The RETURN_WINDOW_DAYS constant is checked."""

    def test_return_window_is_thirty_days(self, fresh_store):
        assert store.RETURN_WINDOW_DAYS == 30


class TestReturnsTracking:
    """A preview does not create execution state."""

    def test_return_preview_is_not_recorded_as_execution(self, fresh_store):
        tools.start_return("112-1111111-1111111", "not as described")
        assert store.RETURNS == {}
