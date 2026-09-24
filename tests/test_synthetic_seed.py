import pytest

from support_chatbot.synthetic_seed import (
    CONFIRMATION,
    UnsafeSyntheticSeed,
    build_dataset,
    fingerprint,
    validate_seed_environment,
)


LOCAL_URL = "postgresql://support_chatbot:secret@postgres:5432/support_chatbot"


def valid_guard(**overrides):
    values = {"environment": "development", "enabled": "true",
              "expected_database": "support_chatbot", "database_url": LOCAL_URL,
              "confirmation": CONFIRMATION}
    values.update(overrides)
    return validate_seed_environment(**values)


def test_dataset_is_deterministic_and_contains_exactly_10k_core_records():
    first = build_dataset()
    second = build_dataset()
    assert first.counts == second.counts
    assert first.core_count == second.core_count == 10_000
    assert fingerprint(first) == fingerprint(second)
    assert first.profiles == second.profiles
    assert first.orders == second.orders
    assert len(first.profiles) == 12
    assert {row[5] for row in first.orders} == {
        "preparing", "shipped", "delivered", "cancelled", "return started", "returned"
    }
    assert {row[5] for row in first.support_requests} >= {
        "tracking", "knowledge", "cancellation", "return", "refund_override",
        "address_change", "privacy_deletion",
    }
    assert {row[6] for row in first.support_tickets} == {"open", "in_progress", "resolved", "closed"}
    assert {row[12] for row in first.approvals} == {"review", "supervisor"}
    assert len(first.action_executions) == 100
    owners = {row[0]: row[2] for row in first.conversations}
    assert all(owners[row[3]] == row[2] for row in first.support_requests)
    edge_cases = {row[8].obj["edge_case"] for row in first.support_requests if row[8].obj["edge_case"]}
    assert {"expired_return_window", "damaged_item", "already_returned"} <= edge_cases
    assert {"high_value_override", "partial_refund", "original_payment_unavailable"} <= edge_cases


@pytest.mark.parametrize("override", [
    {"environment": "production"}, {"enabled": "false"}, {"confirmation": "yes"},
    {"expected_database": "another_database"},
    {"database_url": "postgresql://user:pass@db.supabase.co:5432/support_chatbot"},
    {"database_url": "postgresql://user:pass@oregon-postgres.render.com:5432/support_chatbot"},
])
def test_synthetic_seed_fails_closed(override):
    with pytest.raises(UnsafeSyntheticSeed):
        valid_guard(**override)


def test_synthetic_seed_accepts_only_explicit_local_target():
    assert valid_guard() == "support_chatbot"
