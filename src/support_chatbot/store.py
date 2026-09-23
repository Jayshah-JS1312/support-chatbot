"""Order-store facade backed by the configured durable repository."""

from collections.abc import Mapping

from support_chatbot.persistence import (
    RETURN_WINDOW_DAYS,
    ConcurrentUpdateError,
    get_repository,
    set_repository,
)


def find_orders(email):
    return get_repository().list_orders(email)


def get_order(order_id):
    return get_repository().get_order(order_id)


def cancel(order_id, expected_version):
    return get_repository().cancel_order(order_id, expected_version)


def start_return(order_id, reason, expected_version):
    return get_repository().start_return(order_id, reason, expected_version)


def create_escalation(summary):
    return get_repository().create_escalation(summary)


class _OrdersView(Mapping):
    """Read-only compatibility view; mutations use atomic repository methods."""
    def __getitem__(self, key):
        value = get_order(key)
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self):
        return iter(order["order_id"] for order in get_repository().list_orders())

    def __len__(self):
        return len(get_repository().list_orders())

    def values(self):
        return get_repository().list_orders()


class _ReturnsView(Mapping):
    def __getitem__(self, key): return get_repository().returns[key]
    def __iter__(self): return iter(getattr(get_repository(), "returns", {}))
    def __len__(self): return len(getattr(get_repository(), "returns", {}))


ORDERS = _OrdersView()
RETURNS = _ReturnsView()

__all__ = [
    "ConcurrentUpdateError", "ORDERS", "RETURNS", "RETURN_WINDOW_DAYS",
    "cancel", "create_escalation", "find_orders", "get_order",
    "set_repository", "start_return",
]
