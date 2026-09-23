"""Element 4 of the agent: ACTION — the tools, and the guardrails around them.

Two ideas to notice here:

1. Every tool returns a plain dict. On failure it returns
   {"error": ...} instead of raising, so the model can read the problem
   and explain it to the customer.

2. The guardrails live in the TOOLS, not in the prompt. A prompt rule is
   a suggestion the model can talk itself out of. A check inside
   cancel_order() is a rule it cannot get around.

`cancel_order` and `start_return` declare a `confirmed` flag in
their schema, and their descriptions tell the model to preview first.
The tool functions themselves are unchanged — the policy layer strips
the flag and decides whether the call goes through (policy.guarded_run).
"""

from datetime import date

from support_chatbot import knowledge
from support_chatbot import observe
from support_chatbot import store

# --------------------------------------------------------------------------
# The tools themselves
# --------------------------------------------------------------------------


def find_orders(email):
    """Look up a customer's orders when they don't know the order number."""
    hits = [
        {"order_id": o["order_id"], "item": o["item"],
         "status": o["status"], "ordered_on": o["ordered_on"]}
        for o in store.find_orders(email)
    ]
    if not hits:
        return {"error": f"No orders found for {email}."}
    return {"orders": hits}


def get_order(order_id):
    """Full detail for one order."""
    order = store.get_order(order_id.strip())
    if not order:
        return {"error": f"No order found with id {order_id}."}
    return {
        "order_id": order["order_id"],
        "item": order["item"],
        "price": order["price"],
        "status": order["status"],
        "ordered_on": order["ordered_on"],
        "delivered_on": order["delivered_on"],
        "eta": order.get("eta"),
    }


def track_package(order_id):
    """The carrier scan history for an order."""
    order = store.get_order(order_id.strip())
    if not order:
        return {"error": f"No order found with id {order_id}."}
    if not order["tracking"]:
        return {"error": "No tracking events yet for this order."}
    return {
        "carrier": order["carrier"],
        "eta": order.get("eta"),
        "events": [{"date": d, "detail": t} for d, t in order["tracking"]],
    }


def cancel_order(order_id):
    """Cancel an order — only allowed before it ships. GUARDRAIL."""
    order = store.get_order(order_id.strip())
    if not order:
        return {"error": f"No order found with id {order_id}."}

    if order["status"] in ("shipped", "delivered"):
        return {
            "error": f"Order {order_id} already {order['status']} and cannot "
                     f"be cancelled. It can be returned instead."
        }
    if order["status"] == "cancelled":
        return {"error": f"Order {order_id} is already cancelled."}

    try:
        version = store.cancel(order["order_id"], order["version"])
    except store.ConcurrentUpdateError:
        return {"error": "This order changed while cancellation was being processed. Refresh its status and try again.", "retry": True}
    if version is None:
        return {"error": "The order is no longer cancellable. Refresh its status."}
    return {
        "cancelled": True,
        "order_id": order["order_id"],
        "refund_amount": order["price"],
        "refund_eta": "3-5 business days to the original payment method",
    }


def start_return(order_id, reason):
    """Open a return — only for delivered orders inside the return window. GUARDRAIL."""
    order = store.get_order(order_id.strip())
    if not order:
        return {"error": f"No order found with id {order_id}."}

    if order["status"] != "delivered":
        return {
            "error": f"Order {order_id} is '{order['status']}', not delivered "
                     f"yet, so it can't be returned."
        }

    days = (date.today() - date.fromisoformat(order["delivered_on"])).days
    if days > store.RETURN_WINDOW_DAYS:
        return {
            "error": f"Delivered {days} days ago, past the "
                     f"{store.RETURN_WINDOW_DAYS}-day return window. "
                     f"A human agent can review an exception."
        }

    try:
        rma = store.start_return(order["order_id"], reason, order["version"])
    except store.ConcurrentUpdateError:
        return {"error": "This order changed while the return was being started. Refresh its status and try again.", "retry": True}
    if rma is None:
        return {"error": "The order is no longer return-eligible. Refresh its status."}
    return {
        "rma": rma,
        "order_id": order["order_id"],
        "refund_amount": order["price"],
        "instructions": "Drop off at any UPS Store with the QR code emailed "
                        "to you. Refund issues once we scan the item.",
    }


def search_knowledge(question):
    """Look up what is written down. The RAG tool.

    Everything else here reads the order database. This one reads the
    filing cabinet: the rules a human support agent would have been
    trained on, which the agent can now quote instead of guessing at.

    One argument, on purpose — see knowledge.search() for why the
    obvious second one (a category to search within) was removed.
    """
    hits = knowledge.search(question, k=3)
    return {"passages": [{"source": h["source"], "category": h["category"],
                          "policy": h["heading"], "text": h["text"]}
                         for h in hits]}


def escalate(summary):
    """Hand off to a human. The honest answer when no other tool fits."""
    return {
        "escalated": True,
        "ticket": store.create_escalation(summary),
        "message": "A human agent will email you within 24 hours.",
        "summary": summary,
    }


# --------------------------------------------------------------------------
# Descriptions the model reads to decide which tool to call
# --------------------------------------------------------------------------

def _tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


SCHEMAS = [
    _tool("find_orders",
          "Find a customer's orders by email address. Use when the customer "
          "does not know their order number.",
          {"email": {"type": "string", "description": "Customer email address"}},
          ["email"]),

    _tool("get_order",
          "Get the status and details of one order by its order number.",
          {"order_id": {"type": "string",
                        "description": "Order number, e.g. 112-1111111-1111111"}},
          ["order_id"]),

    _tool("track_package",
          "Get carrier tracking events and delivery estimate for an order.",
          {"order_id": {"type": "string", "description": "Order number"}},
          ["order_id"]),

    _tool("cancel_order",
          "Cancel an order that has not shipped yet and refund it. The first "
          "call returns what would happen; ask the customer, then call again "
          "with confirmed=true once they have said yes.",
          {"order_id": {"type": "string", "description": "Order number"},
           "confirmed": {"type": "boolean",
                         "description": "true only after the customer confirmed"}},
          ["order_id"]),

    _tool("start_return",
          "Start a return for a delivered order and issue an RMA number. The "
          "first call returns what would happen; ask the customer, then call "
          "again with confirmed=true once they have said yes.",
          {"order_id": {"type": "string", "description": "Order number"},
           "reason": {"type": "string",
                      "description": "Why the customer is returning it, in their words"},
           "confirmed": {"type": "boolean",
                         "description": "true only after the customer confirmed"}},
          ["order_id", "reason"]),

    _tool("search_knowledge",
          "Look up what is written down, and quote it back rather than "
          "guessing. Covers four kinds of knowledge: what the customer is "
          "entitled to, what you are allowed to do, how to phrase something "
          "difficult, and the law a policy rests on. Use it for any question "
          "about the rules themselves.",
          {"question": {"type": "string",
                        "description": "The question, in plain words"}},
          ["question"]),

    _tool("escalate",
          "Hand the conversation to a human agent. Use when the customer asks "
          "for a human, is very upset, or the request is outside these tools.",
          {"summary": {"type": "string",
                       "description": "One-line summary of the issue for the human agent"}},
          ["summary"]),
]

REGISTRY = {
    "find_orders": find_orders,
    "get_order": get_order,
    "track_package": track_package,
    "cancel_order": cancel_order,
    "start_return": start_return,
    "search_knowledge": search_knowledge,
    "escalate": escalate,
}


def run(name, args):
    """Execute a tool the model asked for. Never raises — errors come back as data."""
    with observe.timer() as t:
        result = _dispatch(name, args)
    observe.log("tool", tool=name, args=args, ms=t.ms,
                ok="error" not in result,
                error=result.get("error"),
                retryable=bool(result.get("retry")))
    return result


def _dispatch(name, args):
    fn = REGISTRY.get(name)
    if not fn:
        return {"error": f"No such tool: {name}"}
    try:
        return fn(**args)
    except TypeError as e:
        # The agent called its own tool wrongly. That is not a policy
        # refusal — it is a mistake it can fix, so say so explicitly.
        return {"error": f"Bad call to {name}: {e}", "retry": True}
