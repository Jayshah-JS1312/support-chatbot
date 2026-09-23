"""Durable PostgreSQL repositories and a deterministic test repository."""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import date, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from support_chatbot.config import settings


RETURN_WINDOW_DAYS = 30


def demo_orders(today=None):
    """The reproducible scenarios mirrored by the versioned SQL seed."""
    today = today or date.today()
    ago = lambda days: (today - timedelta(days=days)).isoformat()
    return {
        "112-1111111-1111111": {"order_id": "112-1111111-1111111", "email": "raj@example.com", "item": "Sony WH-1000XM5 Headphones", "price": 348.00, "status": "delivered", "ordered_on": ago(9), "delivered_on": ago(4), "eta": None, "carrier": "UPS", "version": 1, "tracking": [(ago(8), "Shipped from Newark, NJ"), (ago(6), "Arrived at facility, Columbus, OH"), (ago(4), "Delivered — left at front door")]},
        "112-2222222-2222222": {"order_id": "112-2222222-2222222", "email": "raj@example.com", "item": "Instant Pot Duo 6qt", "price": 89.99, "status": "shipped", "ordered_on": ago(2), "delivered_on": None, "eta": (today + timedelta(days=1)).isoformat(), "carrier": "Amazon Logistics", "version": 1, "tracking": [(ago(2), "Order placed"), (ago(1), "Shipped from Edison, NJ")]},
        "112-3333333-3333333": {"order_id": "112-3333333-3333333", "email": "mei@example.com", "item": "Kindle Paperwhite 16GB", "price": 149.99, "status": "preparing", "ordered_on": ago(0), "delivered_on": None, "eta": (today + timedelta(days=4)).isoformat(), "carrier": None, "version": 1, "tracking": [(ago(0), "Order placed")]},
        "112-4444444-4444444": {"order_id": "112-4444444-4444444", "email": "mei@example.com", "item": "Logitech MX Master 3S", "price": 99.99, "status": "delivered", "ordered_on": ago(70), "delivered_on": ago(64), "eta": None, "carrier": "USPS", "version": 1, "tracking": [(ago(64), "Delivered — handed to resident")]},
    }


class ConcurrentUpdateError(RuntimeError):
    """The order changed after it was read and before it was updated."""


class PostgresRepository:
    def __init__(self, database_url=None):
        self.database_url = database_url or settings.database_url
        self._pool = None
        self._lock = threading.Lock()

    @property
    def pool(self):
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    pool = ConnectionPool(
                        self.database_url,
                        min_size=settings.database_pool_min,
                        max_size=settings.database_pool_max,
                        kwargs={"row_factory": dict_row},
                        open=False,
                    )
                    pool.open(wait=True)
                    self._pool = pool
        return self._pool

    def close(self):
        if self._pool is not None:
            self._pool.close()

    def healthcheck(self):
        with self.pool.connection() as conn:
            return conn.execute("select 1 as ok").fetchone()["ok"] == 1

    @staticmethod
    def _order(row, events=None):
        if not row:
            return None
        return {
            "order_id": row["order_number"], "email": row["email"],
            "item": row["item_name"], "price": float(row["price"]),
            "status": row["status"], "ordered_on": row["ordered_on"].isoformat(),
            "delivered_on": row["delivered_on"].isoformat() if row["delivered_on"] else None,
            "eta": row["estimated_delivery"].isoformat() if row["estimated_delivery"] else None,
            "carrier": row["carrier"], "version": row["version"],
            "tracking": events or [],
        }

    def list_orders(self, email=None):
        query = """select o.*, p.email from public.orders o
                   join public.profiles p on p.id=o.profile_id"""
        params = ()
        if email:
            query += " where p.email = %s"
            params = (email.strip().lower(),)
        query += " order by o.ordered_on desc, o.order_number"
        with self.pool.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._order(row) for row in rows]

    def get_order(self, order_id):
        with self.pool.connection() as conn:
            row = conn.execute("""select o.*, p.email from public.orders o
                join public.profiles p on p.id=o.profile_id where o.order_number=%s""",
                (order_id.strip(),)).fetchone()
            if not row:
                return None
            events = conn.execute("""select occurred_at::date as event_date, detail
                from public.order_events where order_id=%s order by occurred_at, id""",
                (row["id"],)).fetchall()
        return self._order(row, [(event["event_date"].isoformat(), event["detail"]) for event in events])

    def cancel_order(self, order_id, expected_version):
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute("""update public.orders
                set status='cancelled', version=version+1, updated_at=now()
                where order_number=%s and version=%s and status='preparing'
                returning *""", (order_id, expected_version)).fetchone()
            if not row:
                current = conn.execute("select version from public.orders where order_number=%s", (order_id,)).fetchone()
                if current and current["version"] != expected_version:
                    raise ConcurrentUpdateError(order_id)
                return None
            conn.execute("insert into public.order_events(order_id,event_type,detail) values(%s,'cancelled','Order cancelled by customer')", (row["id"],))
            conn.execute("""insert into public.action_executions
                (idempotency_key,order_id,action_type,status,request_payload,result_payload,finished_at)
                values(%s,%s,'cancel_order','succeeded',%s,%s,now())""",
                (uuid.uuid4().hex, row["id"], Jsonb({"expected_version": expected_version}), Jsonb({"version": row["version"]})))
            return row["version"]

    def start_return(self, order_id, reason, expected_version):
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute("""update public.orders
                set status='return started', version=version+1, updated_at=now()
                where order_number=%s and version=%s and status='delivered'
                returning *""", (order_id, expected_version)).fetchone()
            if not row:
                current = conn.execute("select version from public.orders where order_number=%s", (order_id,)).fetchone()
                if current and current["version"] != expected_version:
                    raise ConcurrentUpdateError(order_id)
                return None
            reference = conn.execute("select 'RMA-' || nextval('public.support_reference_seq') as reference").fetchone()["reference"]
            request = conn.execute("""insert into public.support_requests
                (reference_number,profile_id,order_id,request_type,summary,status,metadata)
                values(%s,%s,%s,'return',%s,'open',%s) returning id""",
                (reference, row["profile_id"], row["id"], reason, Jsonb({"reason": reason}))).fetchone()
            conn.execute("insert into public.order_events(order_id,event_type,detail,metadata) values(%s,'return_started','Return started',%s)", (row["id"], Jsonb({"rma": reference, "reason": reason})))
            conn.execute("""insert into public.action_executions
                (idempotency_key,support_request_id,order_id,action_type,status,request_payload,result_payload,finished_at)
                values(%s,%s,%s,'start_return','succeeded',%s,%s,now())""",
                (uuid.uuid4().hex, request["id"], row["id"], Jsonb({"reason": reason, "expected_version": expected_version}), Jsonb({"rma": reference, "version": row["version"]})))
            return reference

    def create_escalation(self, summary):
        with self.pool.connection() as conn, conn.transaction():
            ref = conn.execute("select 'ESC-' || nextval('public.support_reference_seq') as reference").fetchone()["reference"]
            conn.execute("""insert into public.support_requests(reference_number,request_type,summary,status)
                values(%s,'escalation',%s,'open')""", (ref, summary))
        return ref

    def session_exists(self, sid):
        with self.pool.connection() as conn:
            return conn.execute("select exists(select 1 from public.conversations where browser_session_id=%s) as found", (sid,)).fetchone()["found"]

    def create_session(self, sid, work):
        with self.pool.connection() as conn:
            conn.execute("insert into public.conversations(browser_session_id,working_memory) values(%s,%s) on conflict(browser_session_id) do nothing", (sid, Jsonb(work)))

    def load_session(self, sid):
        with self.pool.connection() as conn:
            conversation = conn.execute("select id,working_memory from public.conversations where browser_session_id=%s", (sid,)).fetchone()
            if not conversation:
                return None
            rows = conn.execute("select role,content,payload from public.messages where conversation_id=%s order by sequence_number", (conversation["id"],)).fetchall()
        history = []
        for row in rows:
            message = dict(row["payload"] or {})
            message["role"] = row["role"]
            message["content"] = row["content"]
            history.append(message)
        return {"history": history, "work": conversation["working_memory"] or {}}

    def save_session(self, sid, history, work, planner="react"):
        with self.pool.connection() as conn, conn.transaction():
            profile_id = None
            email = work.get("customer_email")
            if email:
                profile = conn.execute("select id from public.profiles where email=%s", (email.lower(),)).fetchone()
                profile_id = profile["id"] if profile else None
            conversation = conn.execute("""insert into public.conversations(browser_session_id,profile_id,planner,working_memory)
                values(%s,%s,%s,%s) on conflict(browser_session_id) do update set
                profile_id=coalesce(excluded.profile_id,public.conversations.profile_id), planner=excluded.planner,
                working_memory=excluded.working_memory, updated_at=now() returning id""",
                (sid, profile_id, planner, Jsonb(work))).fetchone()
            for sequence, message in enumerate(history):
                payload = {key: value for key, value in message.items() if key not in {"role", "content"}}
                conn.execute("""insert into public.messages(conversation_id,sequence_number,role,content,payload)
                    values(%s,%s,%s,%s,%s) on conflict(conversation_id,sequence_number) do update
                    set role=excluded.role,content=excluded.content,payload=excluded.payload""",
                    (conversation["id"], sequence, message.get("role"), message.get("content"), Jsonb(payload)))
            conn.execute("delete from public.messages where conversation_id=%s and sequence_number >= %s", (conversation["id"], len(history)))

    def reset_session(self, sid, work):
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute("update public.conversations set profile_id=null,working_memory=%s,status='open',updated_at=now() where browser_session_id=%s returning id", (Jsonb(work), sid)).fetchone()
            if row:
                conn.execute("delete from public.messages where conversation_id=%s", (row["id"],))

    def remember(self, work, session_id):
        email = work.get("customer_email")
        if not email:
            return
        with self.pool.connection() as conn, conn.transaction():
            profile = conn.execute("select id from public.profiles where email=%s", (email.lower(),)).fetchone()
            if not profile:
                return
            current = conn.execute("select summary from public.memory_summaries where profile_id=%s", (profile["id"],)).fetchone()
            summary = dict(current["summary"]) if current else {"first_seen": date.today().isoformat(), "sessions": [], "orders_discussed": [], "actions": [], "escalations": [], "refusals": 0}
            summary["last_seen"] = date.today().isoformat()
            for key, values in (("sessions", [session_id]), ("orders_discussed", list(work.get("orders", {}))), ("actions", work.get("actions", [])), ("escalations", [work["escalation"]] if work.get("escalation") else [])):
                summary.setdefault(key, [])
                summary[key].extend(value for value in values if value not in summary[key])
            summary["refusals"] = max(summary.get("refusals", 0), len(work.get("failures", [])))
            conn.execute("""insert into public.memory_summaries(profile_id,summary) values(%s,%s)
                on conflict(profile_id) do update set summary=excluded.summary,version=memory_summaries.version+1,updated_at=now()""", (profile["id"], Jsonb(summary)))
            for order_id, value in work.get("orders", {}).items():
                conn.execute("""insert into public.memory_facts(profile_id,fact_type,fact_key,value)
                    values(%s,'order',%s,%s) on conflict(profile_id,fact_type,fact_key)
                    do update set value=excluded.value,updated_at=now()""", (profile["id"], order_id, Jsonb(value)))

    def recall(self, email):
        if not email:
            return None
        with self.pool.connection() as conn:
            row = conn.execute("""select ms.summary from public.memory_summaries ms
                join public.profiles p on p.id=ms.profile_id where p.email=%s""", (email.lower(),)).fetchone()
        return dict(row["summary"]) if row else None

    def save_feedback(self, entry):
        with self.pool.connection() as conn:
            conn.execute("insert into public.audit_events(actor_type,event_type,resource_type,detail) values('customer','feedback_submitted','evaluation_feedback',%s)", (Jsonb(entry),))


class InMemoryRepository:
    """Deterministic repository used only by unit tests, never by app startup."""
    def __init__(self, orders=None):
        self.orders = copy.deepcopy(orders or demo_orders())
        self.returns = {}
        self.sessions = {}
        self.memories = {}
        self.feedback = []

    def healthcheck(self): return True
    def list_orders(self, email=None):
        values = self.orders.values()
        return [copy.deepcopy(o) for o in values if not email or o["email"].lower() == email.strip().lower()]
    def get_order(self, order_id): return copy.deepcopy(self.orders.get(order_id.strip()))
    def cancel_order(self, order_id, expected_version):
        order = self.orders.get(order_id)
        if not order: return None
        if order["version"] != expected_version: raise ConcurrentUpdateError(order_id)
        if order["status"] != "preparing": return None
        order["status"], order["version"] = "cancelled", order["version"] + 1
        return order["version"]
    def start_return(self, order_id, reason, expected_version):
        order = self.orders.get(order_id)
        if not order: return None
        if order["version"] != expected_version: raise ConcurrentUpdateError(order_id)
        if order["status"] != "delivered": return None
        reference = f"RMA-{len(self.returns) + 1001}"
        self.returns[reference] = {"order_id": order_id, "reason": reason}
        order["status"], order["version"] = "return started", order["version"] + 1
        return reference
    def create_escalation(self, summary): return f"ESC-{4417 + len(self.feedback)}"
    def session_exists(self, sid): return sid in self.sessions
    def create_session(self, sid, work): self.sessions.setdefault(sid, {"history": [], "work": copy.deepcopy(work)})
    def load_session(self, sid): return copy.deepcopy(self.sessions.get(sid))
    def save_session(self, sid, history, work, planner="react"): self.sessions[sid] = {"history": copy.deepcopy(history), "work": copy.deepcopy(work), "planner": planner}
    def reset_session(self, sid, work): self.sessions[sid] = {"history": [], "work": copy.deepcopy(work)}
    def remember(self, work, session_id):
        email = work.get("customer_email")
        if not email: return
        rec = self.memories.setdefault(email.lower(), {"first_seen": date.today().isoformat(), "sessions": [], "orders_discussed": [], "actions": [], "escalations": [], "refusals": 0})
        rec["last_seen"] = date.today().isoformat()
        for key, values in (("sessions", [session_id]), ("orders_discussed", list(work.get("orders", {}))), ("actions", work.get("actions", [])), ("escalations", [work["escalation"]] if work.get("escalation") else [])):
            rec[key].extend(value for value in values if value not in rec[key])
        rec["refusals"] = max(rec["refusals"], len(work.get("failures", [])))
    def recall(self, email): return copy.deepcopy(self.memories.get((email or "").lower()))
    def save_feedback(self, entry): self.feedback.append(copy.deepcopy(entry))


_repository = PostgresRepository()


def get_repository():
    return _repository


def set_repository(repository):
    global _repository
    previous, _repository = _repository, repository
    return previous
