"""Durable PostgreSQL repositories and a deterministic test repository."""

from __future__ import annotations

import copy
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from support_chatbot.config import settings
from support_chatbot.auth import (
    Identity,
    current_identity,
    hash_password,
    new_token,
    token_digest,
    verify_password,
)


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


class AuthenticationError(RuntimeError):
    pass


class DuplicateEmailError(RuntimeError):
    pass


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

    @contextmanager
    def connection(self, *, auth_lookup=False, allow_signup=False, password_reset=False):
        """Use the restricted DB role and apply request identity to PostgreSQL RLS."""
        identity = current_identity()
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("set local role app_backend")
            conn.execute(
                "select set_config('app.current_user_id',%s,true),"
                "set_config('app.current_role',%s,true),"
                "set_config('app.auth_lookup',%s,true),"
                "set_config('app.allow_signup',%s,true),"
                "set_config('app.password_reset',%s,true)",
                (
                    identity.user_id if identity else "",
                    identity.role if identity else "",
                    "on" if auth_lookup else "off",
                    "on" if allow_signup else "off",
                    "on" if password_reset else "off",
                ),
            )
            yield conn

    @staticmethod
    def _identity(row):
        return Identity(
            str(row["id"]), row["email"], row["display_name"], row["role"],
            str(row["session_id"]) if row.get("session_id") else None,
        )

    def _issue_session(self, conn, user_id):
        token = new_token()
        row = conn.execute(
            """insert into public.auth_sessions(user_id,token_hash,expires_at)
            values(%s,%s,%s) returning id""",
            (user_id, token_digest(token), datetime.now(timezone.utc) + timedelta(hours=settings.auth_session_hours)),
        ).fetchone()
        return token, str(row["id"])

    def signup(self, email, display_name, password):
        email = email.strip().lower()
        with self.connection(auth_lookup=True, allow_signup=True) as conn:
            if conn.execute("select 1 from public.profiles where email=%s", (email,)).fetchone():
                raise DuplicateEmailError(email)
            user_id = uuid.uuid4()
            row = conn.execute(
                """insert into public.profiles(id,email,display_name,role,password_hash)
                values(%s,%s,%s,'customer',%s) returning id,email,display_name,role""",
                (user_id, email, display_name.strip(), hash_password(password)),
            ).fetchone()
            token, session_id = self._issue_session(conn, user_id)
            row["session_id"] = session_id
        return self._identity(row), token

    def login(self, email, password):
        with self.connection(auth_lookup=True) as conn:
            row = conn.execute(
                "select id,email,display_name,role,password_hash from public.profiles where email=%s",
                (email.strip().lower(),),
            ).fetchone()
            if not row or not verify_password(password, row["password_hash"]):
                raise AuthenticationError("Invalid email or password")
            token, session_id = self._issue_session(conn, row["id"])
            row["session_id"] = session_id
        return self._identity(row), token

    def authenticate(self, token):
        if not token:
            return None
        with self.connection(auth_lookup=True) as conn:
            row = conn.execute(
                """select p.id,p.email,p.display_name,p.role,s.id as session_id
                from public.auth_sessions s join public.profiles p on p.id=s.user_id
                where s.token_hash=%s and s.revoked_at is null and s.expires_at>now()""",
                (token_digest(token),),
            ).fetchone()
        return self._identity(row) if row else None

    def logout(self, token):
        if not token:
            return
        with self.connection(auth_lookup=True) as conn:
            conn.execute(
                "update public.auth_sessions set revoked_at=now() where token_hash=%s and revoked_at is null",
                (token_digest(token),),
            )

    def refresh_session(self, token):
        identity = self.authenticate(token)
        if not identity:
            raise AuthenticationError("Session is invalid or expired")
        with self.connection(auth_lookup=True) as conn:
            updated = conn.execute(
                """update public.auth_sessions set revoked_at=now(),refreshed_at=now()
                where token_hash=%s and revoked_at is null returning user_id""",
                (token_digest(token),),
            ).fetchone()
            if not updated:
                raise AuthenticationError("Session is invalid or expired")
            new_value, session_id = self._issue_session(conn, updated["user_id"])
        return Identity(identity.user_id, identity.email, identity.display_name, identity.role, session_id), new_value

    def request_password_reset(self, email):
        with self.connection(auth_lookup=True) as conn:
            user = conn.execute("select id from public.profiles where email=%s", (email.strip().lower(),)).fetchone()
            if not user:
                return None
            token = new_token()
            conn.execute(
                """insert into public.password_reset_tokens(user_id,token_hash,expires_at)
                values(%s,%s,now()+interval '30 minutes')""",
                (user["id"], token_digest(token)),
            )
        return token

    def reset_password(self, token, password):
        with self.connection(auth_lookup=True, password_reset=True) as conn:
            reset = conn.execute(
                """update public.password_reset_tokens set used_at=now()
                where token_hash=%s and used_at is null and expires_at>now() returning user_id""",
                (token_digest(token),),
            ).fetchone()
            if not reset:
                raise AuthenticationError("Reset token is invalid or expired")
            conn.execute("update public.profiles set password_hash=%s,updated_at=now() where id=%s", (hash_password(password), reset["user_id"]))
            conn.execute("update public.auth_sessions set revoked_at=now() where user_id=%s and revoked_at is null", (reset["user_id"],))

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
                   join public.profiles p on p.id=o.user_id"""
        params = ()
        if email:
            query += " where p.email = %s"
            params = (email.strip().lower(),)
        query += " order by o.ordered_on desc, o.order_number"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._order(row) for row in rows]

    def get_order(self, order_id):
        with self.connection() as conn:
            row = conn.execute("""select o.*, p.email from public.orders o
                join public.profiles p on p.id=o.user_id where o.order_number=%s""",
                (order_id.strip(),)).fetchone()
            if not row:
                return None
            events = conn.execute("""select occurred_at::date as event_date, detail
                from public.order_events where order_id=%s order by occurred_at, id""",
                (row["id"],)).fetchall()
        return self._order(row, [(event["event_date"].isoformat(), event["detail"]) for event in events])

    def cancel_order(self, order_id, expected_version):
        with self.connection() as conn:
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
                (idempotency_key,user_id,order_id,action_type,status,request_payload,result_payload,finished_at)
                values(%s,%s,%s,'cancel_order','succeeded',%s,%s,now())""",
                (uuid.uuid4().hex, row["user_id"], row["id"], Jsonb({"expected_version": expected_version}), Jsonb({"version": row["version"]})))
            return row["version"]

    def start_return(self, order_id, reason, expected_version):
        with self.connection() as conn:
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
                (reference_number,user_id,order_id,request_type,summary,status,metadata)
                values(%s,%s,%s,'return',%s,'open',%s) returning id""",
                (reference, row["user_id"], row["id"], reason, Jsonb({"reason": reason}))).fetchone()
            conn.execute("insert into public.order_events(order_id,event_type,detail,metadata) values(%s,'return_started','Return started',%s)", (row["id"], Jsonb({"rma": reference, "reason": reason})))
            conn.execute("""insert into public.action_executions
                (idempotency_key,user_id,support_request_id,order_id,action_type,status,request_payload,result_payload,finished_at)
                values(%s,%s,%s,%s,'start_return','succeeded',%s,%s,now())""",
                (uuid.uuid4().hex, row["user_id"], request["id"], row["id"], Jsonb({"reason": reason, "expected_version": expected_version}), Jsonb({"rma": reference, "version": row["version"]})))
            return reference

    def create_escalation(self, summary):
        identity = current_identity()
        with self.connection() as conn:
            ref = conn.execute("select 'ESC-' || nextval('public.support_reference_seq') as reference").fetchone()["reference"]
            conn.execute("""insert into public.support_requests(reference_number,user_id,request_type,summary,status)
                values(%s,%s,'escalation',%s,'open')""", (ref, identity.user_id, summary))
        return ref

    def session_exists(self, sid):
        with self.connection() as conn:
            return conn.execute("select exists(select 1 from public.conversations where browser_session_id=%s) as found", (sid,)).fetchone()["found"]

    def create_session(self, sid, work, user_id=None):
        identity = current_identity()
        owner = user_id or (identity.user_id if identity else None)
        with self.connection() as conn:
            conn.execute("insert into public.conversations(browser_session_id,user_id,working_memory) values(%s,%s,%s) on conflict(browser_session_id) do nothing", (sid, owner, Jsonb(work)))

    def load_session(self, sid):
        with self.connection() as conn:
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
        identity = current_identity()
        with self.connection() as conn:
            conversation = conn.execute("""insert into public.conversations(browser_session_id,user_id,planner,working_memory)
                values(%s,%s,%s,%s) on conflict(browser_session_id) do update set
                planner=excluded.planner,
                working_memory=excluded.working_memory, updated_at=now() returning id""",
                (sid, identity.user_id, planner, Jsonb(work))).fetchone()
            for sequence, message in enumerate(history):
                payload = {key: value for key, value in message.items() if key not in {"role", "content"}}
                conn.execute("""insert into public.messages(conversation_id,sequence_number,role,content,payload)
                    values(%s,%s,%s,%s,%s) on conflict(conversation_id,sequence_number) do update
                    set role=excluded.role,content=excluded.content,payload=excluded.payload""",
                    (conversation["id"], sequence, message.get("role"), message.get("content"), Jsonb(payload)))
            conn.execute("delete from public.messages where conversation_id=%s and sequence_number >= %s", (conversation["id"], len(history)))

    def reset_session(self, sid, work):
        with self.connection() as conn:
            row = conn.execute("update public.conversations set working_memory=%s,status='open',updated_at=now() where browser_session_id=%s returning id", (Jsonb(work), sid)).fetchone()
            if row:
                conn.execute("delete from public.messages where conversation_id=%s", (row["id"],))

    def remember(self, work, session_id):
        email = work.get("customer_email")
        if not email:
            return
        with self.connection() as conn:
            profile = conn.execute("select id from public.profiles where email=%s", (email.lower(),)).fetchone()
            if not profile:
                return
            current = conn.execute("select summary from public.memory_summaries where user_id=%s", (profile["id"],)).fetchone()
            summary = dict(current["summary"]) if current else {"first_seen": date.today().isoformat(), "sessions": [], "orders_discussed": [], "actions": [], "escalations": [], "refusals": 0}
            summary["last_seen"] = date.today().isoformat()
            for key, values in (("sessions", [session_id]), ("orders_discussed", list(work.get("orders", {}))), ("actions", work.get("actions", [])), ("escalations", [work["escalation"]] if work.get("escalation") else [])):
                summary.setdefault(key, [])
                summary[key].extend(value for value in values if value not in summary[key])
            summary["refusals"] = max(summary.get("refusals", 0), len(work.get("failures", [])))
            conn.execute("""insert into public.memory_summaries(user_id,summary) values(%s,%s)
                on conflict(user_id) do update set summary=excluded.summary,version=memory_summaries.version+1,updated_at=now()""", (profile["id"], Jsonb(summary)))
            for order_id, value in work.get("orders", {}).items():
                conn.execute("""insert into public.memory_facts(user_id,fact_type,fact_key,value)
                    values(%s,'order',%s,%s) on conflict(user_id,fact_type,fact_key)
                    do update set value=excluded.value,updated_at=now()""", (profile["id"], order_id, Jsonb(value)))

    def recall(self, email):
        if not email:
            return None
        with self.connection() as conn:
            row = conn.execute("""select ms.summary from public.memory_summaries ms
                join public.profiles p on p.id=ms.user_id where p.email=%s""", (email.lower(),)).fetchone()
        return dict(row["summary"]) if row else None

    def save_feedback(self, entry):
        identity = current_identity()
        with self.connection() as conn:
            conn.execute("insert into public.audit_events(user_id,actor_type,event_type,resource_type,detail) values(%s,'customer','feedback_submitted','evaluation_feedback',%s)", (identity.user_id, Jsonb(entry)))


class InMemoryRepository:
    """Deterministic repository used only by unit tests, never by app startup."""
    def __init__(self, orders=None, enforce_auth=False):
        self.enforce_auth = enforce_auth
        self.profiles = {
            "raj@example.com": {"id": "11111111-1111-4111-8111-111111111111", "email": "raj@example.com", "display_name": "Raj", "role": "customer", "password_hash": "$2b$12$j13U2WsnUkP44jE7HB3R..FBQ5WIDLaEer7.byeDjvm.DQxuLxxs2"},
            "mei@example.com": {"id": "22222222-2222-4222-8222-222222222222", "email": "mei@example.com", "display_name": "Mei", "role": "customer", "password_hash": "$2b$12$r.D51P.Rae2IpFCjg9igTOFGO1LqQ2f4raOGPOEifB3.myJzqObo6"},
            "admin@example.com": {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "email": "admin@example.com", "display_name": "Ami Admin", "role": "admin", "password_hash": "$2b$12$K48ubTO4gn8sQQagt0Ow6uDsGybOEpXMu7B4yvZPEq5bBvSvj2Ol6"},
        }
        self.orders = copy.deepcopy(orders or demo_orders())
        for order in self.orders.values():
            profile = self.profiles.get(order["email"])
            order["user_id"] = profile["id"] if profile else None
        self.returns = {}
        self.sessions = {}
        self.auth_sessions = {}
        self.reset_tokens = {}
        self.memories = {}
        self.feedback = []

    def healthcheck(self): return True
    def _visible(self, owner):
        identity = current_identity()
        return not self.enforce_auth or bool(identity and (identity.role == "admin" or identity.user_id == owner))
    def signup(self, email, display_name, password):
        email = email.strip().lower()
        if email in self.profiles: raise DuplicateEmailError(email)
        profile = {"id": str(uuid.uuid4()), "email": email, "display_name": display_name.strip(), "role": "customer", "password_hash": hash_password(password)}
        self.profiles[email] = profile
        return self._login_profile(profile)
    def _login_profile(self, profile):
        token, session_id = new_token(), str(uuid.uuid4())
        self.auth_sessions[token_digest(token)] = {"id": session_id, "user_id": profile["id"], "active": True}
        return Identity(profile["id"], profile["email"], profile["display_name"], profile["role"], session_id), token
    def login(self, email, password):
        profile = self.profiles.get(email.strip().lower())
        if not profile or not verify_password(password, profile["password_hash"]): raise AuthenticationError("Invalid email or password")
        return self._login_profile(profile)
    def authenticate(self, token):
        session = self.auth_sessions.get(token_digest(token or ""))
        if not session or not session["active"]: return None
        profile = next(p for p in self.profiles.values() if p["id"] == session["user_id"])
        return Identity(profile["id"], profile["email"], profile["display_name"], profile["role"], session["id"])
    def logout(self, token):
        session = self.auth_sessions.get(token_digest(token or ""))
        if session: session["active"] = False
    def refresh_session(self, token):
        identity = self.authenticate(token)
        if not identity: raise AuthenticationError("Session is invalid or expired")
        self.logout(token)
        return self._login_profile(self.profiles[identity.email])
    def request_password_reset(self, email):
        profile = self.profiles.get(email.strip().lower())
        if not profile: return None
        token = new_token(); self.reset_tokens[token_digest(token)] = profile["id"]
        return token
    def reset_password(self, token, password):
        user_id = self.reset_tokens.pop(token_digest(token), None)
        if not user_id: raise AuthenticationError("Reset token is invalid or expired")
        profile = next(p for p in self.profiles.values() if p["id"] == user_id)
        profile["password_hash"] = hash_password(password)
        for session in self.auth_sessions.values():
            if session["user_id"] == user_id: session["active"] = False
    def list_orders(self, email=None):
        values = self.orders.values()
        return [copy.deepcopy(o) for o in values if self._visible(o.get("user_id")) and (not email or o["email"].lower() == email.strip().lower())]
    def get_order(self, order_id):
        order = self.orders.get(order_id.strip())
        return copy.deepcopy(order) if order and self._visible(order.get("user_id")) else None
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
    def create_session(self, sid, work, user_id=None):
        identity = current_identity(); owner = user_id or (identity.user_id if identity else None)
        self.sessions.setdefault(sid, {"history": [], "work": copy.deepcopy(work), "user_id": owner})
    def load_session(self, sid):
        value = self.sessions.get(sid)
        return copy.deepcopy(value) if value and self._visible(value.get("user_id")) else None
    def save_session(self, sid, history, work, planner="react"):
        identity = current_identity(); previous = self.sessions.get(sid)
        if previous and not self._visible(previous.get("user_id")): return
        self.sessions[sid] = {"history": copy.deepcopy(history), "work": copy.deepcopy(work), "planner": planner, "user_id": identity.user_id if identity else None}
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
