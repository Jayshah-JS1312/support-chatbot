"""Canonical, immutable action proposals for privileged customer operations."""

import hashlib
import json
from datetime import date

from support_chatbot.persistence import RETURN_WINDOW_DAYS


PRIVILEGED_ACTIONS = {"cancel_order", "start_return"}


def action_hash(action, arguments, consequences, policy_evidence, order_version):
    payload = {
        "action": action,
        "arguments": arguments,
        "consequences": consequences,
        "order_version": order_version,
        "policy_evidence": policy_evidence,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_proposal(action, arguments, order):
    if action not in PRIVILEGED_ACTIONS:
        raise ValueError("Unsupported privileged action")
    order_id = arguments.get("order_id", "").strip()
    if not order or order["order_id"] != order_id:
        raise ValueError("Order not found")
    if action == "cancel_order":
        if order["status"] != "preparing":
            raise ValueError("Order is no longer cancellable")
        clean_args = {"order_id": order_id}
        consequences = {
            "new_status": "cancelled",
            "refund_amount": order["price"],
            "refund_destination": "original payment method",
            "refund_eta": "3-5 business days",
        }
        evidence = {
            "rule": "Only preparing orders may be cancelled",
            "observed_status": order["status"],
        }
    else:
        reason = str(arguments.get("reason", "")).strip()
        if not reason:
            raise ValueError("Return reason is required")
        if order["status"] != "delivered":
            raise ValueError("Order is not delivered")
        days = (date.today() - date.fromisoformat(order["delivered_on"])).days
        if days > RETURN_WINDOW_DAYS:
            raise ValueError("Order is outside the return window")
        clean_args = {"order_id": order_id, "reason": reason}
        consequences = {
            "new_status": "return started",
            "refund_amount": order["price"],
            "refund_timing": "after carrier acceptance",
        }
        evidence = {
            "rule": f"Delivered orders may be returned within {RETURN_WINDOW_DAYS} days",
            "days_since_delivery": days,
        }
    proposal = {
        "action": action,
        "arguments": clean_args,
        "consequences": consequences,
        "policy_evidence": evidence,
        "order_version": order["version"],
    }
    proposal["action_hash"] = action_hash(**proposal)
    return proposal


def verify_proposal(proposal):
    expected = action_hash(
        proposal["action"], proposal["arguments"], proposal["consequences"],
        proposal["policy_evidence"], proposal["order_version"],
    )
    return expected == proposal["action_hash"]
