"""A pretend Amazon database.

Real support tools would hit a real API. For class we keep a handful of
orders in a dict so the tools have something honest to return.
Everything is dated relative to today, so the examples never go stale.
"""

from datetime import date, timedelta

TODAY = date.today()


def _days_ago(n):
    return (TODAY - timedelta(days=n)).isoformat()


ORDERS = {
    "112-1111111-1111111": {
        "order_id": "112-1111111-1111111",
        "email": "raj@example.com",
        "item": "Sony WH-1000XM5 Headphones",
        "price": 348.00,
        "status": "delivered",
        "ordered_on": _days_ago(9),
        "delivered_on": _days_ago(4),
        "carrier": "UPS",
        "tracking": [
            (_days_ago(8), "Shipped from Newark, NJ"),
            (_days_ago(6), "Arrived at facility, Columbus, OH"),
            (_days_ago(4), "Delivered — left at front door"),
        ],
    },
    "112-2222222-2222222": {
        "order_id": "112-2222222-2222222",
        "email": "raj@example.com",
        "item": "Instant Pot Duo 6qt",
        "price": 89.99,
        "status": "shipped",
        "ordered_on": _days_ago(2),
        "delivered_on": None,
        "eta": (TODAY + timedelta(days=1)).isoformat(),
        "carrier": "Amazon Logistics",
        "tracking": [
            (_days_ago(2), "Order placed"),
            (_days_ago(1), "Shipped from Edison, NJ"),
        ],
    },
    "112-3333333-3333333": {
        "order_id": "112-3333333-3333333",
        "email": "mei@example.com",
        "item": "Kindle Paperwhite 16GB",
        "price": 149.99,
        "status": "preparing",   # not shipped yet — still cancellable
        "ordered_on": _days_ago(0),
        "delivered_on": None,
        "eta": (TODAY + timedelta(days=4)).isoformat(),
        "carrier": None,
        "tracking": [(_days_ago(0), "Order placed")],
    },
    "112-4444444-4444444": {
        "order_id": "112-4444444-4444444",
        "email": "mei@example.com",
        "item": "Logitech MX Master 3S",
        "price": 99.99,
        "status": "delivered",
        "ordered_on": _days_ago(70),   # too old to return
        "delivered_on": _days_ago(64),
        "carrier": "USPS",
        "tracking": [(_days_ago(64), "Delivered — handed to resident")],
    },
}

# Returns created during this session land here.
RETURNS = {}

RETURN_WINDOW_DAYS = 30
