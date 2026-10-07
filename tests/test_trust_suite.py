"""Frozen trust-study contract validation."""

from evaluations.trust_suite import (
    FROZEN_SHA256,
    REQUIRED_JOURNEYS,
    checksum,
    load,
    validate,
)


def test_frozen_trust_suite_is_complete_and_unchanged():
    rows = load()

    assert validate(rows) == []
    assert checksum() == FROZEN_SHA256
    assert len(rows) == 20
    assert REQUIRED_JOURNEYS.issubset({row["id"] for row in rows})


def test_frozen_suite_reports_hitl_and_non_hitl_journeys():
    rows = load()

    assert any(row["requires_hitl"] for row in rows)
    assert any(not row["requires_hitl"] for row in rows)
    assert {row["account"] for row in rows}.issuperset({
        "raj@example.com", "priya@example.com", "noah@example.com", "admin@example.com",
    })


def test_every_journey_has_measurable_expected_customer_behavior():
    for row in load():
        assert row["expected_response"]
        assert row["expected_state"]
        assert row["customer_visible_result"]
        assert row["prohibited_behavior"]
        assert row["max_response_time_ms"] > 0
