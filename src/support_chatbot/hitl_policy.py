"""Deterministic human-review routing at the structured action boundary.

Free-form customer text and model prose never grant authority. Routing uses the
validated action proposed by the tool layer. Unknown actions fail closed.
"""

from dataclasses import dataclass


PRIVILEGED_ACTIONS = frozenset({
    "cancel_order",
    "start_return",
    "return_override",
    "refund_override",
    "update_delivery_address",
    "delete_customer_data",
    "escalate_to_human",
})

SAFE_ACTIONS = frozenset({
    "send_resolution",
    "track_order",
    "list_orders",
    "search_knowledge",
    "deny_cross_user_access",
    "refuse_sensitive_disclosure",
    "refuse_unsafe_tool_request",
})


@dataclass(frozen=True)
class RoutingDecision:
    requires_hitl: bool
    reason: str


def classify_action(action_name):
    """Classify a validated action; unknown/malformed actions fail closed."""
    action = str(action_name or "").strip()
    if action in SAFE_ACTIONS:
        return RoutingDecision(False, "read_only_or_deterministic_denial")
    if action in PRIVILEGED_ACTIONS:
        return RoutingDecision(True, "privileged_or_irreversible_action")
    return RoutingDecision(True, "unknown_action_fail_closed")


def classify_proposal(proposed_action):
    proposal = proposed_action if isinstance(proposed_action, dict) else {}
    return classify_action(proposal.get("type"))
