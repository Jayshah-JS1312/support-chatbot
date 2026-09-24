"""Unit contracts for deterministic, fail-closed human-review routing."""

import pytest

from support_chatbot.hitl_policy import (
    PRIVILEGED_ACTIONS,
    SAFE_ACTIONS,
    classify_action,
    classify_proposal,
)


@pytest.mark.parametrize("action", sorted(PRIVILEGED_ACTIONS))
def test_privileged_actions_require_review(action):
    decision = classify_action(action)
    assert decision.requires_hitl is True
    assert decision.reason == "privileged_or_irreversible_action"


@pytest.mark.parametrize("action", sorted(SAFE_ACTIONS))
def test_read_only_answers_and_deterministic_denials_skip_review(action):
    decision = classify_action(action)
    assert decision.requires_hitl is False
    assert decision.reason == "read_only_or_deterministic_denial"


@pytest.mark.parametrize("proposal", [None, {}, {"type": ""}, {"type": "new_tool"}, "bad"])
def test_unknown_or_malformed_actions_fail_closed(proposal):
    decision = classify_proposal(proposal)
    assert decision.requires_hitl is True
    assert decision.reason == "unknown_action_fail_closed"
