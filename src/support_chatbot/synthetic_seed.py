"""Deterministic, development-only synthetic business-data generator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import psycopg
from psycopg.types.json import Jsonb

from support_chatbot.config import settings


DATASET_ID = "synthetic-dev-phase12-v1"
GENERATOR_VERSION = 1
CONFIRMATION = "SYNTHETIC_DATA_ONLY"
NAMESPACE = uuid.UUID("43ecb866-aeaa-5ca5-862b-6a933a4db253")
ANCHOR = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
SYNTHETIC_PASSWORD_HASH = "$2b$12$UFSfGvu8eWCZRe8FaJckJ.Rb7awyN3YS2dftyP9cGq5.S7cgunECq"
CORE_COUNTS = {"orders": 3000, "order_events": 4000,
               "support_requests": 2000, "support_tickets": 1000}


class UnsafeSyntheticSeed(RuntimeError):
    """Raised when any production-safety gate is not satisfied."""


def stable_id(kind: str, number: int) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{DATASET_ID}:{kind}:{number}")


def validate_seed_environment(*, environment: str, enabled: str,
                              expected_database: str, database_url: str,
                              confirmation: str) -> str:
    """Require three explicit opt-ins and reject recognisably hosted targets."""
    if environment.strip().lower() not in {"local", "development", "test"}:
        raise UnsafeSyntheticSeed("Synthetic seed requires SUPPORT_CHATBOT_ENVIRONMENT=development (or local/test).")
    if enabled.strip().lower() not in {"1", "true", "yes", "on"}:
        raise UnsafeSyntheticSeed("Synthetic seed is disabled; set SUPPORT_CHATBOT_ENABLE_SYNTHETIC_SEED=true.")
    if confirmation != CONFIRMATION:
        raise UnsafeSyntheticSeed(f"Pass --confirm {CONFIRMATION} to acknowledge destructive synthetic-data replacement.")
    parsed = urlparse(database_url)
    database = parsed.path.removeprefix("/")
    host = (parsed.hostname or "").lower()
    if not expected_database or database != expected_database:
        raise UnsafeSyntheticSeed("SUPPORT_CHATBOT_SYNTHETIC_SEED_DATABASE must exactly match the target database name.")
    if any(marker in host for marker in ("supabase.co", "render.com", "amazonaws.com", "neon.tech")):
        raise UnsafeSyntheticSeed(f"Refusing hosted database target: {host}")
    return database


@dataclass(frozen=True)
class SyntheticData:
    profiles: list[tuple]
    conversations: list[tuple]
    messages: list[tuple]
    orders: list[tuple]
    order_events: list[tuple]
    support_requests: list[tuple]
    drafts: list[tuple]
    approvals: list[tuple]
    action_executions: list[tuple]
    support_tickets: list[tuple]

    @property
    def counts(self) -> dict[str, int]:
        return {name: len(getattr(self, name)) for name in self.__dataclass_fields__}

    @property
    def core_count(self) -> int:
        return sum(self.counts[name] for name in CORE_COUNTS)


def build_dataset() -> SyntheticData:
    """Build stable identities, timestamps, ownership and scenario distributions."""
    profiles = []
    conversations = []
    messages = []
    orders = []
    order_events = []
    requests = []
    drafts = []
    approvals = []
    action_executions = []
    tickets = []
    statuses = ("preparing", "shipped", "delivered", "cancelled", "return started", "returned")
    products = ("Wireless Headphones", "Air Fryer", "E-reader", "Mechanical Keyboard",
                "USB-C Hub", "Smart Speaker", "Robot Vacuum", "Coffee Maker")
    carriers = ("Amazon Logistics", "UPS", "USPS", "FedEx")

    for customer in range(12):
        user_id = stable_id("profile", customer)
        profiles.append((user_id, f"synthetic+customer{customer + 1:02d}@example.invalid",
                         f"Synthetic Customer {customer + 1:02d}", SYNTHETIC_PASSWORD_HASH))
        for conversation in range(20):
            n = customer * 20 + conversation
            conversation_id = stable_id("conversation", n)
            created = ANCHOR + timedelta(minutes=n)
            conversations.append((conversation_id, f"synthetic-phase12-{n:04d}", user_id,
                                  "react" if n % 2 == 0 else "plan",
                                  f"[SYNTHETIC] Scenario conversation {n + 1}", created, created))
            messages.extend([
                (conversation_id, 0, "user", "[SYNTHETIC] Please help with my order.", created),
                (conversation_id, 1, "assistant", "[SYNTHETIC] This is a support response.", created + timedelta(seconds=2)),
            ])

    for n in range(CORE_COUNTS["orders"]):
        user_id = profiles[n % len(profiles)][0]
        order_id = stable_id("order", n)
        status = statuses[n % len(statuses)]
        ordered_on = date(2026, 1, 1) + timedelta(days=n % 220)
        delivered_on = ordered_on + timedelta(days=4) if status in {"delivered", "return started", "returned"} else None
        estimated = ordered_on + timedelta(days=5) if status in {"preparing", "shipped"} else None
        order_number = f"990-{n + 1:07d}-{(n * 7919) % 10_000_000:07d}"
        created = ANCHOR + timedelta(seconds=n)
        orders.append((order_id, order_number, user_id, f"[SYNTHETIC] {products[n % len(products)]}",
                       f"{9.99 + (n % 49000) / 100:.2f}", status, ordered_on,
                       delivered_on, estimated, carriers[n % len(carriers)], 1 + n % 4, created, created))
        order_events.append((order_id, "placed", "[SYNTHETIC] Order placed", created,
                             Jsonb({"synthetic": True, "dataset_id": DATASET_ID})))
        if n < 1000:
            event_type = "delivered" if status in {"delivered", "return started", "returned"} else status
            order_events.append((order_id, event_type, f"[SYNTHETIC] Order is {event_type}",
                                 created + timedelta(hours=1),
                                 Jsonb({"synthetic": True, "dataset_id": DATASET_ID})))

    request_types = ("tracking", "knowledge", "cancellation", "return",
                     "refund_override", "address_change", "privacy_deletion")
    for n in range(CORE_COUNTS["support_requests"]):
        request_id = stable_id("request", n)
        user_id = profiles[n % len(profiles)][0]
        customer = n % len(profiles)
        conversation_id = conversations[customer * 20 + ((n // len(profiles)) % 20)][0]
        order_id = orders[n % len(orders)][0]
        created = ANCHOR + timedelta(minutes=n)
        request_type = request_types[n % len(request_types)]
        edge_case = None
        if request_type == "return":
            edge_case = ("expired_return_window", "damaged_item", "already_returned")[n % 3]
        elif request_type == "refund_override":
            edge_case = ("high_value_override", "partial_refund", "original_payment_unavailable")[n % 3]
        approval_case = n < 600
        variant = n % 6
        if approval_case and variant in (0, 1):
            status, completed_at = "AWAITING_APPROVAL", None
        elif approval_case and variant == 2:
            status, completed_at = "COMPLETED", created + timedelta(minutes=8)
        elif approval_case and variant == 3:
            status, completed_at = "REJECTED", created + timedelta(minutes=7)
        elif approval_case and variant == 4:
            status, completed_at = "COMPLETED_WITHOUT_ACTION", created + timedelta(minutes=5)
        else:
            status, completed_at = "COMPLETED", created + timedelta(seconds=4)
        requests.append((request_id, f"SYN-REQ-{n + 1:05d}", user_id, conversation_id, order_id,
                         request_type, f"[SYNTHETIC] {request_type.replace('_', ' ')} scenario {n + 1}",
                         status, Jsonb({"synthetic": True, "dataset_id": DATASET_ID,
                                       "scenario": request_type, "edge_case": edge_case}),
                         f"synthetic:{DATASET_ID}:{n}", hashlib.sha256(f"request:{n}".encode()).hexdigest(),
                         created, created, completed_at))
        if approval_case:
            draft_id = stable_id("draft", n)
            action = "cancel_order" if n % 2 == 0 else "start_return"
            if variant in (0, 1):
                draft_status = "pending_approval"
            elif variant == 2:
                draft_status = "completed"
            elif variant == 3:
                draft_status = "rejected"
            elif variant == 4:
                draft_status = "expired"
            else:
                draft_status = "completed"
            order_version = orders[n % len(orders)][10]
            action_hash = hashlib.sha256(
                f"{action}:{orders[n % len(orders)][1]}:v{order_version}".encode()
            ).hexdigest()
            drafts.append((draft_id, request_id, "[SYNTHETIC] Proposed customer resolution.",
                           Jsonb({"type": action}), draft_status, action,
                           Jsonb({"order_id": orders[n % len(orders)][1]}),
                           Jsonb({"refund_amount": str(orders[n % len(orders)][4])}),
                           Jsonb({"policy": "synthetic-policy-evidence"}), order_version, action_hash,
                           created - timedelta(minutes=1), True, "Synthetic privileged action", created, created))
            if variant != 5:
                approval_id = stable_id("approval", n)
                task_status = {0: "pending", 1: "pending", 2: "approved", 3: "rejected", 4: "expired"}[variant]
                decided = created + timedelta(minutes=6) if task_status != "pending" else None
                queue = "supervisor" if variant == 1 else "review"
                if task_status == "pending" and variant == 0:
                    reminder = datetime(2034, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=n)
                    escalation = datetime(2034, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=n)
                    expiry = datetime(2035, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=n)
                else:
                    reminder = created + timedelta(seconds=60)
                    escalation = created + timedelta(seconds=120)
                    expiry = (datetime(2035, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=n)
                              if task_status == "pending" else created + timedelta(minutes=5))
                approvals.append((approval_id, draft_id, task_status,
                                  "Synthetic reviewer decision" if decided else None,
                                  expiry, decided, created, variant == 2,
                                  reminder, reminder if variant in (1, 4) else None,
                                  escalation, escalation if variant in (1, 4) else None, queue))
                if variant == 2:
                    action_executions.append((
                        stable_id("execution", n), f"synthetic-execution:{n}", request_id,
                        conversation_id, order_id, user_id, action, "succeeded",
                        Jsonb({"synthetic": True, "dataset_id": DATASET_ID}),
                        Jsonb({"synthetic": True, "outcome": "completed"}),
                        created + timedelta(minutes=7), created + timedelta(minutes=8),
                    ))

    ticket_statuses = ("open", "in_progress", "resolved", "closed")
    for n in range(CORE_COUNTS["support_tickets"]):
        created = ANCHOR + timedelta(minutes=n)
        status = ticket_statuses[n % len(ticket_statuses)]
        resolution = "[SYNTHETIC] Customer was contacted and the issue was resolved." if status in {"resolved", "closed"} else None
        tickets.append((stable_id("ticket", n), f"SYN-ESC-{n + 1:05d}",
                        profiles[n % len(profiles)][0], conversations[n % len(conversations)][0],
                        requests[n % len(requests)][0], f"[SYNTHETIC] Human-support scenario {n + 1}",
                        status, resolution, created, created + timedelta(minutes=15),
                        created + timedelta(minutes=15) if resolution else None))

    data = SyntheticData(profiles, conversations, messages, orders, order_events,
                         requests, drafts, approvals, action_executions, tickets)
    assert data.core_count == 10_000
    assert data.counts["order_events"] == CORE_COUNTS["order_events"]
    return data


def fingerprint(data: SyntheticData) -> str:
    payload = {"dataset_id": DATASET_ID, "version": GENERATOR_VERSION,
               "counts": data.counts,
               "first_profile": str(data.profiles[0][0]),
               "last_ticket": str(data.support_tickets[-1][0])}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _chunks(rows: list[tuple], size: int = 1000):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def seed(database_url: str, data: SyntheticData) -> dict:
    started = time.perf_counter()
    profile_ids = [row[0] for row in data.profiles]
    with psycopg.connect(database_url) as conn:
        conn.execute("select pg_advisory_xact_lock(%s)", (7152202612,))
        if conn.execute("select to_regclass('public.synthetic_seed_runs')").fetchone()[0] is None:
            raise RuntimeError("Apply database migrations before running the synthetic seed.")
        # Replace only the fixed Phase-12 identities. User-created and demo rows are untouched.
        conn.execute("delete from public.support_tickets where user_id=any(%s)", (profile_ids,))
        conn.execute("delete from public.audit_events where user_id=any(%s)", (profile_ids,))
        conn.execute("delete from public.action_executions where user_id=any(%s)", (profile_ids,))
        conn.execute("delete from public.support_requests where user_id=any(%s)", (profile_ids,))
        conn.execute("delete from public.conversations where user_id=any(%s)", (profile_ids,))
        conn.execute("delete from public.orders where user_id=any(%s)", (profile_ids,))
        conn.execute("delete from public.profiles where id=any(%s)", (profile_ids,))

        statements = (
            ("insert into public.profiles(id,email,display_name,role,password_hash) values(%s,%s,%s,'customer',%s)", data.profiles),
            ("insert into public.conversations(id,browser_session_id,user_id,planner,title,created_at,updated_at) values(%s,%s,%s,%s,%s,%s,%s)", data.conversations),
            ("insert into public.messages(conversation_id,sequence_number,role,content,created_at) values(%s,%s,%s,%s,%s)", data.messages),
            ("""insert into public.orders(id,order_number,user_id,item_name,price,status,ordered_on,delivered_on,
                 estimated_delivery,carrier,version,created_at,updated_at) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", data.orders),
            ("insert into public.order_events(order_id,event_type,detail,occurred_at,metadata) values(%s,%s,%s,%s,%s)", data.order_events),
            ("""insert into public.support_requests(id,reference_number,user_id,conversation_id,order_id,request_type,
                 summary,status,metadata,idempotency_key,payload_hash,created_at,updated_at,completed_at)
                 values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", data.support_requests),
            ("""insert into public.resolution_drafts(id,support_request_id,content,proposed_action,status,action_name,
                 action_arguments,customer_consequences,policy_evidence,order_version,action_hash,customer_confirmed_at,
                 requires_hitl,routing_reason,created_at,updated_at) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", data.drafts),
            ("""insert into public.approval_tasks(id,resolution_draft_id,status,decision_reason,expires_at,decided_at,
                 created_at,review_necessary,reminder_at,reminded_at,escalation_at,escalated_at,queue_name)
                 values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", data.approvals),
            ("""insert into public.action_executions(id,idempotency_key,support_request_id,conversation_id,order_id,
                 user_id,action_type,status,request_payload,result_payload,started_at,finished_at)
                 values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", data.action_executions),
            ("""insert into public.support_tickets(id,reference_number,user_id,conversation_id,source_request_id,summary,
                 status,resolution,created_at,updated_at,resolved_at) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", data.support_tickets),
        )
        with conn.cursor() as cursor:
            for statement, rows in statements:
                for batch in _chunks(rows):
                    cursor.executemany(statement, batch)
        counts = data.counts | {"core_business_records": data.core_count}
        run_fingerprint = fingerprint(data)
        conn.execute("""insert into public.synthetic_seed_runs
            (dataset_id,generator_version,record_counts,content_fingerprint,generated_at)
            values(%s,%s,%s,%s,%s) on conflict(dataset_id) do update set
            generator_version=excluded.generator_version,record_counts=excluded.record_counts,
            content_fingerprint=excluded.content_fingerprint,generated_at=excluded.generated_at""",
            (DATASET_ID, GENERATOR_VERSION, Jsonb(counts), run_fingerprint, ANCHOR))
    return {"dataset_id": DATASET_ID, "fingerprint": fingerprint(data), "counts": counts,
            "elapsed_seconds": round(time.perf_counter() - started, 3)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", default="", help=f"Required literal: {CONFIRMATION}")
    args = parser.parse_args(argv)
    database_url = settings.database_url
    validate_seed_environment(
        environment=os.getenv("SUPPORT_CHATBOT_ENVIRONMENT", ""),
        enabled=os.getenv("SUPPORT_CHATBOT_ENABLE_SYNTHETIC_SEED", ""),
        expected_database=os.getenv("SUPPORT_CHATBOT_SYNTHETIC_SEED_DATABASE", ""),
        database_url=database_url,
        confirmation=args.confirm,
    )
    report = seed(database_url, build_dataset())
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
