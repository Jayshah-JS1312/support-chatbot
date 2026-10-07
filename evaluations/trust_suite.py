"""Validation and reporting for the frozen Ami trust-journey suite.

This suite is a product contract, not a prompt-only benchmark. It includes
customer chat, admin decisions, durable workflow, failure, security, and UI
recovery journeys. Run ``python -m evaluations.trust_suite --check`` before a
before/after study; changing the JSON requires an explicit checksum update.
"""

import argparse
import hashlib
import json
from pathlib import Path


SUITE = Path(__file__).with_name("trust_suite.json")
FROZEN_SHA256 = "0a47dca233e9ca8e84c98906d9b5074bbb2787edff4cc3deb08f53338cf206fd"
REQUIRED_FIELDS = {
    "id", "title", "category", "account", "starting_database_state",
    "messages", "expected_response", "requires_hitl", "expected_state",
    "customer_visible_result", "prohibited_behavior", "max_response_time_ms",
}
REQUIRED_JOURNEYS = {
    "trust-01-owned-order-list",
    "trust-02-indirect-order-follow-up",
    "trust-03-ambiguous-product",
    "trust-04-ineligible-cancellation",
    "trust-05-eligible-return-approval",
    "trust-06-explicit-human-handoff",
    "trust-07-customer-ticket-status",
    "trust-08-admin-approves-action",
    "trust-09-admin-rejects-action",
    "trust-10-approval-expiry",
    "trust-11-prompt-injection",
    "trust-12-false-supervisor-authority",
    "trust-13-cross-customer-request",
    "trust-14-model-failure",
    "trust-15-retrieval-failure",
    "trust-16-refresh-pending-workflow",
}


def load(path=SUITE):
    return json.loads(Path(path).read_text())


def checksum(path=SUITE):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(rows, *, require_frozen_checksum=True, path=SUITE):
    errors = []
    if len(rows) != 20:
        errors.append(f"suite must contain exactly 20 journeys, found {len(rows)}")
    identifiers = [row.get("id") for row in rows]
    if len(identifiers) != len(set(identifiers)):
        errors.append("journey ids must be unique")
    missing_journeys = REQUIRED_JOURNEYS.difference(identifiers)
    if missing_journeys:
        errors.append("required journeys missing: " + ", ".join(sorted(missing_journeys)))

    for index, row in enumerate(rows, 1):
        label = row.get("id") or f"row {index}"
        missing = REQUIRED_FIELDS.difference(row)
        if missing:
            errors.append(f"{label}: missing fields {', '.join(sorted(missing))}")
        if not isinstance(row.get("messages"), list) or not row.get("messages"):
            errors.append(f"{label}: messages must be a non-empty list")
        if not isinstance(row.get("prohibited_behavior"), list) \
                or not row.get("prohibited_behavior"):
            errors.append(f"{label}: prohibited_behavior must be a non-empty list")
        if not isinstance(row.get("requires_hitl"), bool):
            errors.append(f"{label}: requires_hitl must be boolean")
        budget = row.get("max_response_time_ms")
        if not isinstance(budget, int) or budget <= 0:
            errors.append(f"{label}: max_response_time_ms must be a positive integer")
        for field in REQUIRED_FIELDS.difference({
            "messages", "prohibited_behavior", "requires_hitl", "max_response_time_ms",
        }):
            if not isinstance(row.get(field), str) or not row.get(field, "").strip():
                errors.append(f"{label}: {field} must be non-empty text")

    if require_frozen_checksum and Path(path).resolve() == SUITE.resolve():
        observed = checksum(path)
        if observed != FROZEN_SHA256:
            errors.append(
                f"frozen suite checksum changed: expected {FROZEN_SHA256}, got {observed}"
            )
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the suite contract changed")
    args = parser.parse_args()
    rows = load()
    errors = validate(rows, require_frozen_checksum=args.check)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        raise SystemExit(1)
    hitl = sum(row["requires_hitl"] for row in rows)
    print(f"Ami frozen trust suite: {len(rows)} journeys, {hitl} HITL, "
          f"{len(rows) - hitl} non-HITL")
    print(f"sha256: {checksum()}")


if __name__ == "__main__":
    main()
