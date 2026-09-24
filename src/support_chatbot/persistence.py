"""Durable PostgreSQL repositories and a deterministic test repository."""

from __future__ import annotations

import copy
import hashlib
import json
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


class IdempotencyConflictError(RuntimeError):
    pass


class ConversationBusyError(RuntimeError):
    pass


class InvalidWorkflowTransition(RuntimeError):
    pass


class ActionProposalError(RuntimeError):
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

    def create_escalation(self, summary, conversation_id=None, source_request_id=None):
        identity = current_identity()
        with self.connection() as conn:
            ref = conn.execute("select 'ESC-' || nextval('public.support_reference_seq') as reference").fetchone()["reference"]
            conversation = conn.execute(
                "select id from public.conversations where browser_session_id=%s",
                (conversation_id,),
            ).fetchone() if conversation_id else None
            ticket = conn.execute(
                """insert into public.support_tickets
                (reference_number,user_id,conversation_id,source_request_id,summary,status)
                values(%s,%s,%s,%s,%s,'open') returning id""",
                (ref, identity.user_id, conversation["id"] if conversation else None,
                 source_request_id, summary),
            ).fetchone()
            conn.execute(
                """insert into public.audit_events
                (user_id,actor_type,actor_id,event_type,resource_type,resource_id,request_id,detail)
                values(%s,'agent','ami','support_ticket_created','support_ticket',%s,%s,%s)""",
                (identity.user_id, str(ticket["id"]), str(source_request_id) if source_request_id else None,
                 Jsonb({"reference_number": ref, "status": "open"})),
            )
        return ref

    def list_tickets(self, status="all", limit=100):
        identity = current_identity()
        with self.connection() as conn:
            rows = conn.execute(
                """select t.*,p.email as customer_email,p.display_name as customer_name,
                reviewer.display_name as assignee_name,c.browser_session_id
                from public.support_tickets t join public.profiles p on p.id=t.user_id
                left join public.profiles reviewer on reviewer.id=t.assigned_to
                left join public.conversations c on c.id=t.conversation_id
                where (%s='all' or t.status=%s)
                  and (%s='admin' or t.user_id=%s)
                order by case when t.status in ('open','in_progress') then 0 else 1 end,
                         t.created_at desc limit %s""",
                (status, status, identity.role, identity.user_id, limit),
            ).fetchall()
        return [self._workflow_request(row) for row in rows]

    def update_ticket(self, ticket_id, status, resolution, admin_user_id):
        if status not in {"in_progress", "resolved", "closed"}:
            raise InvalidWorkflowTransition(status)
        with self.connection() as conn:
            row = conn.execute(
                """update public.support_tickets set status=%s,
                resolution=case when %s='resolved' then %s else resolution end,
                assigned_to=%s,updated_at=now(),
                resolved_at=case when %s='resolved' and resolved_at is null then now()
                                 else resolved_at end
                where id=%s and (
                  (status='open' and %s in ('in_progress','resolved')) or
                  (status='in_progress' and %s='resolved') or
                  (status='resolved' and %s='closed')
                ) returning *""",
                (status, status, resolution or None, admin_user_id, status,
                 ticket_id, status, status, status),
            ).fetchone()
            if not row:
                exists = conn.execute(
                    "select status from public.support_tickets where id=%s", (ticket_id,)
                ).fetchone()
                if exists:
                    raise InvalidWorkflowTransition(
                        f"Ticket cannot move from {exists['status']} to {status}"
                    )
                return None
            conn.execute(
                """insert into public.audit_events
                (user_id,actor_type,actor_id,event_type,resource_type,resource_id,detail)
                values(%s,'admin',%s,'support_ticket_updated','support_ticket',%s,%s)""",
                (row["user_id"], admin_user_id, str(ticket_id),
                 Jsonb({"status": status, "resolution": resolution or None})),
            )
        return self._workflow_request(row)

    def create_support_request(self, summary, planner, idempotency_key, session_id=None):
        identity = current_identity()
        payload_hash = hashlib.sha256(f"{planner}\0{summary}".encode()).hexdigest()
        with self.connection() as conn:
            conversation = None
            if session_id:
                conversation = conn.execute(
                    "select id from public.conversations where browser_session_id=%s for update",
                    (session_id,),
                ).fetchone()
                active = conn.execute(
                    """select 1 from public.support_requests
                    where conversation_id=%s and idempotency_key<>%s
                    and status in ('RECEIVED','QUEUED','DRAFTING')
                    limit 1""", (conversation["id"], idempotency_key),
                ).fetchone() if conversation else None
                if active:
                    raise ConversationBusyError(session_id)
            reference = conn.execute(
                "select 'REQ-' || nextval('public.support_reference_seq') as reference"
            ).fetchone()["reference"]
            row = conn.execute(
                """insert into public.support_requests
                (reference_number,user_id,conversation_id,request_type,summary,status,
                 metadata,idempotency_key,payload_hash)
                values(%s,%s,%s,'customer_message',%s,'RECEIVED',%s,%s,%s)
                on conflict(user_id,idempotency_key) do nothing returning *""",
                (reference, identity.user_id, conversation["id"] if conversation else None,
                 summary, Jsonb({"planner": planner}), idempotency_key, payload_hash),
            ).fetchone()
            created = row is not None
            if not row:
                row = conn.execute(
                    "select * from public.support_requests where user_id=%s and idempotency_key=%s",
                    (identity.user_id, idempotency_key),
                ).fetchone()
                if row["payload_hash"] != payload_hash:
                    raise IdempotencyConflictError(idempotency_key)
            row["browser_session_id"] = session_id
        return self._workflow_request(row), created

    def create_action_proposal(self, action, arguments):
        from support_chatbot.actions import build_proposal
        identity = current_identity()
        order = self.get_order(arguments.get("order_id", ""))
        try:
            proposal = build_proposal(action, arguments, order)
        except ValueError as error:
            raise ActionProposalError(str(error)) from error
        with self.connection() as conn:
            row = conn.execute(
                """insert into public.action_proposals
                (user_id,action_name,action_arguments,customer_consequences,
                 policy_evidence,order_version,action_hash)
                values(%s,%s,%s,%s,%s,%s,%s) returning *""",
                (identity.user_id, action, Jsonb(proposal["arguments"]),
                 Jsonb(proposal["consequences"]), Jsonb(proposal["policy_evidence"]),
                 proposal["order_version"], proposal["action_hash"]),
            ).fetchone()
        result = proposal | {"proposal_id": str(row["id"]), "expires_at": row["expires_at"].isoformat()}
        return result

    def confirm_action_proposal(self, proposal_id, supplied_hash, idempotency_key):
        identity = current_identity()
        with self.connection() as conn:
            proposal = conn.execute(
                "select * from public.action_proposals where id=%s for update", (proposal_id,)
            ).fetchone()
            if not proposal:
                raise ActionProposalError("Proposal not found")
            if proposal["action_hash"] != supplied_hash:
                raise ActionProposalError("Action hash does not match the preview")
            existing = conn.execute(
                "select * from public.support_requests where user_id=%s and idempotency_key=%s",
                (identity.user_id, idempotency_key),
            ).fetchone()
            if existing:
                if (existing["metadata"] or {}).get("action_hash") != supplied_hash:
                    raise IdempotencyConflictError(idempotency_key)
                return self._workflow_request(existing), False
            if proposal["status"] != "previewed" or proposal["expires_at"] <= datetime.now(timezone.utc):
                raise ActionProposalError("Proposal is expired or already confirmed")
            order = conn.execute(
                "select id from public.orders where order_number=%s",
                (proposal["action_arguments"]["order_id"],),
            ).fetchone()
            reference = conn.execute(
                "select 'REQ-' || nextval('public.support_reference_seq') as reference"
            ).fetchone()["reference"]
            payload_hash = hashlib.sha256(supplied_hash.encode()).hexdigest()
            request = conn.execute(
                """insert into public.support_requests
                (reference_number,user_id,order_id,request_type,summary,status,metadata,
                 idempotency_key,payload_hash)
                values(%s,%s,%s,'privileged_action',%s,'AWAITING_APPROVAL',%s,%s,%s)
                returning *""",
                (reference, identity.user_id, order["id"],
                 f"Confirmed proposal to {proposal['action_name']}",
                 Jsonb({"proposal_id": str(proposal["id"]), "action_hash": supplied_hash}),
                 idempotency_key, payload_hash),
            ).fetchone()
            content = (
                f"Customer confirmed {proposal['action_name']} with consequences: "
                f"{json.dumps(proposal['customer_consequences'], sort_keys=True)}"
            )
            draft = conn.execute(
                """insert into public.resolution_drafts
                (support_request_id,content,proposed_action,status,action_name,
                 action_arguments,customer_consequences,policy_evidence,order_version,
                 action_hash,customer_confirmed_at)
                values(%s,%s,%s,'pending_approval',%s,%s,%s,%s,%s,%s,now()) returning id""",
                (request["id"], content,
                 Jsonb({"type": proposal["action_name"], "arguments": proposal["action_arguments"]}),
                 proposal["action_name"], Jsonb(proposal["action_arguments"]),
                 Jsonb(proposal["customer_consequences"]), Jsonb(proposal["policy_evidence"]),
                 proposal["order_version"], supplied_hash),
            ).fetchone()
            reminder_seconds, escalation_seconds, expiry_seconds = settings.approval_deadlines
            conn.execute(
                """insert into public.approval_tasks
                (resolution_draft_id,status,reminder_at,escalation_at,expires_at)
                values(%s,'pending',now()+make_interval(secs => %s),
                now()+make_interval(secs => %s),now()+make_interval(secs => %s))""",
                (draft["id"], reminder_seconds, escalation_seconds, expiry_seconds),
            )
            conn.execute("""insert into public.audit_events
                (user_id,actor_type,actor_id,event_type,resource_type,resource_id,request_id,detail)
                values(%s,'customer',%s,'approval_created','support_request',%s,%s,%s)""",
                (identity.user_id, identity.user_id, str(request["id"]), str(request["id"]),
                 Jsonb({"draft_id": str(draft["id"]), "action": proposal["action_name"],
                        "deadlines_seconds": {
                            "reminder": reminder_seconds,
                            "escalation": escalation_seconds,
                            "expiry": expiry_seconds,
                        }})),)
            conn.execute(
                "update public.action_proposals set status='confirmed',customer_confirmed_at=now() where id=%s",
                (proposal_id,),
            )
        return self._workflow_request(request), True

    @staticmethod
    def _workflow_request(row):
        if not row:
            return None
        result = dict(row)
        for key in ("id", "user_id", "conversation_id", "order_id"):
            if result.get(key) is not None:
                result[key] = str(result[key])
        for key, value in list(result.items()):
            if isinstance(value, datetime):
                result[key] = value.isoformat()
        result["metadata"] = dict(result.get("metadata") or {})
        return result

    def get_support_request(self, request_id):
        identity = current_identity()
        with self.connection() as conn:
            row = conn.execute(
                """select r.*, c.browser_session_id, d.content as draft_content, d.proposed_action,
                a.status as approval_status, a.decision_reason, a.expires_at
                from public.support_requests r
                left join public.conversations c on c.id=r.conversation_id
                left join public.resolution_drafts d on d.support_request_id=r.id
                left join public.approval_tasks a on a.resolution_draft_id=d.id
                where r.id=%s and (%s='admin' or r.user_id=%s)""",
                (request_id, identity.role, identity.user_id),
            ).fetchone()
        return self._workflow_request(row)

    def list_support_requests(self, limit=50):
        identity = current_identity()
        with self.connection() as conn:
            rows = conn.execute(
                """select r.*,c.browser_session_id,d.content as draft_content,d.proposed_action,d.action_name,
                a.status as approval_status,a.decision_reason,a.expires_at
                from public.support_requests r
                left join public.conversations c on c.id=r.conversation_id
                left join public.resolution_drafts d on d.support_request_id=r.id
                left join public.approval_tasks a on a.resolution_draft_id=d.id
                where (%s='admin' or r.user_id=%s)
                order by r.created_at desc limit %s""",
                (identity.role, identity.user_id, limit),
            ).fetchall()
        return [self._workflow_request(row) for row in reversed(rows)]

    def get_request_agent_context(self, request_id):
        with self.connection() as conn:
            row = conn.execute("""select p.id,p.email,p.display_name,p.role,
                c.browser_session_id,r.metadata from public.support_requests r
                join public.profiles p on p.id=r.user_id
                left join public.conversations c on c.id=r.conversation_id
                where r.id=%s""", (request_id,)).fetchone()
        if not row:
            return None
        return {
            "identity": Identity(str(row["id"]), row["email"], row["display_name"], row["role"]),
            "session_id": row["browser_session_id"],
            "planner": (row["metadata"] or {}).get("planner", "react"),
        }

    def mark_enqueued(self, request_id, workflow_run_id):
        with self.connection() as conn:
            row = conn.execute(
                """update public.support_requests set
                status=case when status='RECEIVED' then 'QUEUED' else status end,
                workflow_run_id=%s,enqueued_at=now(),last_error=null,updated_at=now()
                where id=%s and status in ('RECEIVED','QUEUED','APPROVED') returning *""",
                (workflow_run_id, request_id),
            ).fetchone()
        return self._workflow_request(row)

    def mark_enqueue_failed(self, request_id, error, max_attempts=5):
        with self.connection() as conn:
            row = conn.execute(
                """update public.support_requests set enqueue_attempts=enqueue_attempts+1,
                next_enqueue_at=now() + make_interval(
                  secs => least(300, power(2, least(enqueue_attempts, 8))::int)
                ),
                status=case when enqueue_attempts+1 >= %s then 'COMPLETED_WITHOUT_ACTION' else status end,
                completed_at=case when enqueue_attempts+1 >= %s then now() else completed_at end,
                metadata=case when enqueue_attempts+1 >= %s then metadata ||
                  '{"customer_status":"We could not start this request after repeated attempts. No action was taken."}'::jsonb
                  else metadata end,
                last_error=%s,updated_at=now() where id=%s
                and status in ('RECEIVED','APPROVED') returning *""",
                (max_attempts, max_attempts, max_attempts, str(error)[:500], request_id),
            ).fetchone()
        return self._workflow_request(row)

    def pending_enqueues(self, limit=100):
        with self.connection() as conn:
            rows = conn.execute(
                """select * from public.support_requests
                where status in ('RECEIVED','APPROVED') and next_enqueue_at<=now()
                order by created_at for update skip locked limit %s""", (limit,),
            ).fetchall()
        return [self._workflow_request(row) for row in rows]

    def claim_drafting(self, request_id, lease_seconds):
        with self.connection() as conn:
            row = conn.execute(
                """update public.support_requests set status='DRAFTING',
                processing_lease_until=now()+make_interval(secs => %s),updated_at=now()
                where id=%s and (status in ('RECEIVED','QUEUED') or
                (status='DRAFTING' and processing_lease_until<now())) returning *""",
                (lease_seconds, request_id),
            ).fetchone()
        return self._workflow_request(row)

    def save_resolution_draft(self, request_id, content, proposed_action, approval_deadlines,
                              requires_hitl=True, routing_reason=None):
        with self.connection() as conn:
            request = conn.execute(
                "select * from public.support_requests where id=%s for update", (request_id,)
            ).fetchone()
            if not request:
                return None
            proposed_action = proposed_action or {"type": "send_resolution"}
            action_name = proposed_action.get("type", "send_resolution")
            arguments = proposed_action.get("arguments") or {}
            order = None
            if arguments.get("order_id"):
                order = conn.execute(
                    "select id from public.orders where order_number=%s",
                    (arguments["order_id"],),
                ).fetchone()
            draft = conn.execute(
                """insert into public.resolution_drafts
                (support_request_id,content,proposed_action,status,action_name,
                 action_arguments,customer_consequences,policy_evidence,order_version,
                 action_hash,customer_confirmed_at,requires_hitl,routing_reason)
                values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                  case when %s then now() else null end,%s,%s)
                on conflict(support_request_id) do update set
                content=excluded.content,proposed_action=excluded.proposed_action,
                action_name=excluded.action_name,action_arguments=excluded.action_arguments,
                customer_consequences=excluded.customer_consequences,
                policy_evidence=excluded.policy_evidence,order_version=excluded.order_version,
                action_hash=excluded.action_hash,
                customer_confirmed_at=excluded.customer_confirmed_at,
                requires_hitl=excluded.requires_hitl,routing_reason=excluded.routing_reason,
                status=excluded.status,version=resolution_drafts.version+1,updated_at=now()
                returning id""", (
                    request_id, content, Jsonb(proposed_action),
                    "pending_approval" if requires_hitl else "completed", action_name,
                    Jsonb(arguments), Jsonb(proposed_action.get("customer_consequences") or {}),
                    Jsonb(proposed_action.get("policy_evidence") or {}),
                    proposed_action.get("order_version"), proposed_action.get("action_hash"),
                    bool(proposed_action.get("customer_confirmed")),
                    requires_hitl, routing_reason,
                ),
            ).fetchone()
            if requires_hitl:
                reminder_seconds, escalation_seconds, expiry_seconds = approval_deadlines
                conn.execute(
                    """insert into public.approval_tasks
                    (resolution_draft_id,status,reminder_at,escalation_at,expires_at)
                    values(%s,'pending',now()+make_interval(secs => %s),
                    now()+make_interval(secs => %s),now()+make_interval(secs => %s))
                    on conflict(resolution_draft_id) do update set status='pending',
                    reminder_at=excluded.reminder_at,reminded_at=null,
                    escalation_at=excluded.escalation_at,escalated_at=null,
                    expires_at=excluded.expires_at,queue_name='review',
                    decision_reason=null,decided_at=null""",
                    (draft["id"], reminder_seconds, escalation_seconds, expiry_seconds),
                )
                audit_type = "approval_created"
                audit_detail = {
                    "draft_id": str(draft["id"]), "action": action_name,
                    "routing_reason": routing_reason,
                    "deadlines_seconds": {
                        "reminder": reminder_seconds,
                        "escalation": escalation_seconds,
                        "expiry": expiry_seconds,
                    },
                }
            else:
                audit_type = "resolution_completed_without_review"
                audit_detail = {"draft_id": str(draft["id"]), "action": action_name,
                                "routing_reason": routing_reason}
            conn.execute("""insert into public.audit_events
                (user_id,actor_type,actor_id,event_type,resource_type,resource_id,request_id,detail)
                values(%s,'system','workflow',%s,'support_request',%s,%s,%s)""",
                (request["user_id"], audit_type, str(request_id), str(request_id),
                 Jsonb(audit_detail)),)
            row = conn.execute(
                """update public.support_requests set status=%s,order_id=coalesce(%s,order_id),
                completed_at=case when %s then null else now() end,
                processing_lease_until=null,updated_at=now() where id=%s
                and status='DRAFTING' returning *""",
                ("AWAITING_APPROVAL" if requires_hitl else "COMPLETED",
                 order["id"] if order else None, requires_hitl, request_id),
            ).fetchone()
        return self._workflow_request(row)

    def list_approval_tasks(self, status="pending"):
        clauses = {
            "pending": "a.status='pending' and a.queue_name='review' and a.expires_at>now()",
            "overdue": "a.status='pending' and a.queue_name='supervisor' and a.expires_at>now()",
            "approved": "a.status='approved'",
            "rejected": "a.status='rejected'",
            "expired": "a.status='expired'",
        }
        where = clauses.get(status, "true")
        with self.connection() as conn:
            rows = conn.execute(f"""select a.id,r.id as request_id,r.reference_number,
                r.summary as customer_request,r.status as request_status,a.status,
                a.reminder_at,a.reminded_at,a.escalation_at,a.escalated_at,
                a.expires_at,a.queue_name,a.created_at,a.decided_at,a.review_necessary,
                p.email as customer_email,p.display_name as customer_name,
                reviewer.display_name as reviewer_name,
                extract(epoch from (now()-a.created_at))::int as age_seconds
                from public.approval_tasks a
                join public.resolution_drafts d on d.id=a.resolution_draft_id
                join public.support_requests r on r.id=d.support_request_id
                join public.profiles p on p.id=r.user_id
                left join public.profiles reviewer on reviewer.id=a.assigned_to
                where {where} order by
                case when a.status='pending' then a.expires_at end asc nulls last,
                a.created_at desc""").fetchall()
        return [self._workflow_request(row) for row in rows]

    def hitl_operational_metrics(self):
        """Return reviewer-labelled precision and current queue health.

        Precision only uses approvals a human actually decided. Pending and
        expired work are reported separately because neither contains a human
        judgment about whether review was necessary.
        """
        with self.connection() as conn:
            row = conn.execute("""select
                count(*) filter (where status in ('approved','rejected'))::int as decided,
                count(*) filter (where status in ('approved','rejected')
                    and review_necessary is true)::int as marked_necessary,
                count(*) filter (where status in ('approved','rejected')
                    and review_necessary is null)::int as unlabeled_decisions,
                count(*) filter (where status='pending')::int as pending,
                count(*) filter (where status='expired')::int as expired
                from public.approval_tasks""").fetchone()
        result = dict(row)
        result["escalation_precision"] = round(
            100 * result["marked_necessary"] / result["decided"], 1
        ) if result["decided"] else None
        return result

    def operational_metrics(self):
        """Aggregate durable queue, workflow latency, and decision health."""
        with self.connection() as conn:
            row = conn.execute("""select
              (select count(*)::int from public.support_requests where status in
                ('RECEIVED','QUEUED','DRAFTING','APPROVED','EXECUTING')) as queue_depth,
              (select count(*)::int from public.approval_tasks where status='pending') as pending_approvals,
              (select extract(epoch from (now()-min(created_at)))::int
                from public.approval_tasks where status='pending') as oldest_pending_approval_seconds,
              (select coalesce(sum(greatest(enqueue_attempts-1,0)),0)::int
                from public.support_requests) as retry_count,
              (select count(*)::int from public.workflow_dead_letters) as dead_letter_count,
              (select count(*)::int from public.approval_tasks where status='expired') as expired_approvals,
              (select percentile_cont(.5) within group(order by extract(epoch from (d.created_at-r.created_at))*1000)
                from public.resolution_drafts d join public.support_requests r on r.id=d.support_request_id) as drafting_p50_ms,
              (select percentile_cont(.95) within group(order by extract(epoch from (d.created_at-r.created_at))*1000)
                from public.resolution_drafts d join public.support_requests r on r.id=d.support_request_id) as drafting_p95_ms,
              (select percentile_cont(.5) within group(order by extract(epoch from (a.decided_at-a.created_at))*1000)
                from public.approval_tasks a where a.decided_at is not null) as approval_wait_p50_ms,
              (select percentile_cont(.95) within group(order by extract(epoch from (a.decided_at-a.created_at))*1000)
                from public.approval_tasks a where a.decided_at is not null) as approval_wait_p95_ms,
              (select percentile_cont(.5) within group(order by extract(epoch from (x.started_at-a.decided_at))*1000)
                from public.action_executions x
                join public.resolution_drafts d on d.support_request_id=x.support_request_id
                join public.approval_tasks a on a.resolution_draft_id=d.id
                where a.decided_at is not null) as resume_p50_ms,
              (select percentile_cont(.95) within group(order by extract(epoch from (x.started_at-a.decided_at))*1000)
                from public.action_executions x
                join public.resolution_drafts d on d.support_request_id=x.support_request_id
                join public.approval_tasks a on a.resolution_draft_id=d.id
                where a.decided_at is not null) as resume_p95_ms,
              (select percentile_cont(.5) within group(order by extract(epoch from (completed_at-created_at))*1000)
                from public.support_requests where completed_at is not null) as resolution_p50_ms,
              (select percentile_cont(.95) within group(order by extract(epoch from (completed_at-created_at))*1000)
                from public.support_requests where completed_at is not null) as resolution_p95_ms
            """).fetchone()
            decisions = conn.execute("""select status,count(*)::int as count
                from public.approval_tasks group by status order by status""").fetchall()
        result = dict(row)
        for key, value in result.items():
            if key.endswith("_ms"):
                result[key] = round(float(value), 1) if value is not None else None
        result["decision_breakdown"] = {item["status"]: item["count"] for item in decisions}
        result.update(self.hitl_operational_metrics())
        latest = self.latest_hitl_evaluation()
        summary = (latest or {}).get("summary") or {}
        result["hitl_recall"] = summary.get("hitl_recall")
        result["hitl_recall_numerator"] = summary.get("hitl_recall_numerator", 0)
        result["hitl_recall_denominator"] = summary.get("hitl_recall_denominator", 0)
        return result

    def operational_requests(self, limit=100):
        with self.connection() as conn:
            rows = conn.execute("""select r.id,r.reference_number,r.status,r.created_at,
                r.updated_at,r.enqueued_at,r.completed_at,r.enqueue_attempts,r.last_error,
                d.action_name,d.requires_hitl,d.routing_reason,d.created_at as drafted_at,
                a.status as approval_status,a.created_at as approval_created_at,
                a.decided_at,a.expires_at,
                x.started_at as execution_started_at,x.finished_at as execution_finished_at,
                x.status as execution_status,x.error_code,
                (select count(*)::int from public.workflow_dead_letters w
                 where w.support_request_id=r.id) as dead_letters
                from public.support_requests r
                left join public.resolution_drafts d on d.support_request_id=r.id
                left join public.approval_tasks a on a.resolution_draft_id=d.id
                left join public.action_executions x on x.support_request_id=r.id
                order by r.created_at desc limit %s""", (limit,)).fetchall()
        return [self._operational_request(row) for row in rows]

    def _operational_request(self, row):
        from support_chatbot.operational import explain_state, iso, safe_error_code
        result = {key: iso(value) for key, value in dict(row).items()}
        result["id"] = str(result["id"])
        result["state_explanation"] = explain_state(result["status"])
        if result.get("last_error"):
            result["last_error"] = safe_error_code(result["last_error"])
        return result

    def operational_request_timeline(self, request_id):
        from support_chatbot.operational import safe_audit_detail
        with self.connection() as conn:
            request = conn.execute("""select r.id,r.reference_number,r.status,r.created_at,
                r.updated_at,r.enqueued_at,r.completed_at,r.enqueue_attempts,r.last_error,
                d.action_name,d.requires_hitl,d.routing_reason,d.created_at as drafted_at,
                a.status as approval_status,a.created_at as approval_created_at,a.decided_at,a.expires_at,
                x.started_at as execution_started_at,x.finished_at as execution_finished_at,
                x.status as execution_status,x.error_code
                from public.support_requests r
                left join public.resolution_drafts d on d.support_request_id=r.id
                left join public.approval_tasks a on a.resolution_draft_id=d.id
                left join public.action_executions x on x.support_request_id=r.id
                where r.id=%s""", (request_id,)).fetchone()
            if not request:
                return None
            events = conn.execute("""select event_type,actor_type,created_at,detail
                from public.audit_events where resource_type='support_request'
                and resource_id=%s order by created_at,id""", (str(request_id),)).fetchall()
        result = self._operational_request(request)
        timeline = [
            {"event": event["event_type"], "actor": event["actor_type"],
             "at": event["created_at"].isoformat(),
             "detail": safe_audit_detail(event["detail"])}
            for event in events
        ]
        milestones = (
            ("request_received", request["created_at"]),
            ("request_enqueued", request["enqueued_at"]),
            ("resolution_drafted", request["drafted_at"]),
            ("approval_wait_started", request["approval_created_at"]),
            (f"approval_{request['approval_status']}", request["decided_at"]),
            ("execution_started", request["execution_started_at"]),
            ("request_completed", request["completed_at"]),
        )
        timeline.extend({"event": name, "actor": "system", "at": moment.isoformat(), "detail": {}}
                        for name, moment in milestones if moment)
        timeline.sort(key=lambda event: event["at"])
        result["timeline"] = timeline
        return result

    def save_hitl_evaluation(self, report):
        with self.connection() as conn:
            run = conn.execute("""insert into public.evaluation_runs
                (suite,status,summary,completed_at) values
                ('hitl_blocking','complete',%s,now()) returning id,started_at,completed_at""",
                (Jsonb(report["summary"]),)).fetchone()
            for result in report["results"]:
                conn.execute("""insert into public.evaluation_results
                    (evaluation_run_id,case_id,passed,detail) values (%s,%s,%s,%s)""",
                    (run["id"], result["id"], result["passed"], Jsonb(result)))
        return {**report, "run_id": str(run["id"]),
                "generated_at": run["completed_at"].isoformat()}

    def latest_hitl_evaluation(self):
        with self.connection() as conn:
            run = conn.execute("""select id,status,summary,completed_at from public.evaluation_runs
                where suite='hitl_blocking' and status='complete'
                order by completed_at desc nulls last limit 1""").fetchone()
            if not run:
                return None
            rows = conn.execute("""select detail from public.evaluation_results
                where evaluation_run_id=%s order by case_id""", (run["id"],)).fetchall()
        return {"status": run["status"], "suite": "hitl_blocking",
                "run_id": str(run["id"]),
                "generated_at": run["completed_at"].isoformat(),
                "summary": run["summary"], "results": [row["detail"] for row in rows]}

    def get_approval_detail(self, request_id):
        with self.connection() as conn:
            row = conn.execute("""select a.id as approval_id,a.status as approval_status,
                a.assigned_to,a.decision_reason,a.reminder_at,a.reminded_at,
                a.escalation_at,a.escalated_at,a.expires_at,a.queue_name,
                a.decided_at,a.created_at,
                a.review_necessary,d.content as proposed_response,d.proposed_action,
                d.action_name,d.action_arguments,d.customer_consequences,d.policy_evidence,
                d.order_version,d.action_hash,d.version as draft_version,
                r.id as request_id,r.reference_number,r.summary as customer_request,
                r.status as request_status,r.conversation_id,r.created_at as request_created_at,
                p.email as customer_email,p.display_name as customer_name,
                o.order_number,o.item_name,o.price,o.status as order_status,o.version as current_order_version,
                reviewer.display_name as reviewer_name,reviewer.email as reviewer_email
                from public.approval_tasks a
                join public.resolution_drafts d on d.id=a.resolution_draft_id
                join public.support_requests r on r.id=d.support_request_id
                join public.profiles p on p.id=r.user_id
                left join public.orders o on o.id=r.order_id
                left join public.profiles reviewer on reviewer.id=a.assigned_to
                where r.id=%s""", (request_id,)).fetchone()
            if not row:
                return None
            audits = conn.execute("""select event_type,actor_id,detail,created_at
                from public.audit_events where resource_type='support_request'
                and resource_id=%s order by created_at""", (str(request_id),)).fetchall()
        result = self._workflow_request(row)
        result["audit_history"] = [self._workflow_request(event) for event in audits]
        return result

    def list_admin_reviewers(self):
        with self.connection() as conn:
            rows = conn.execute("""select id,email,display_name from public.profiles
                where role='admin' order by display_name,email""").fetchall()
        return [{**dict(row), "id": str(row["id"])} for row in rows]

    def decide_support_request(self, request_id, approve, reason, admin_user_id,
                               edited_response=None, review_necessary=None):
        # Persist any due expiry before evaluating the decision. A delayed scheduler
        # must never make a late approval valid.
        self.process_absence_policy()
        target = "APPROVED" if approve else "REJECTED"
        approval = "approved" if approve else "rejected"
        with self.connection() as conn:
            draft = conn.execute("""select d.id,d.content,d.version from public.resolution_drafts d
                join public.support_requests r on r.id=d.support_request_id
                join public.approval_tasks a on a.resolution_draft_id=d.id
                where r.id=%s and r.status='AWAITING_APPROVAL'
                and a.status='pending' and a.expires_at>now() for update of r,d,a""",
                (request_id,)).fetchone()
            if not draft:
                existing = conn.execute("select * from public.support_requests where id=%s", (request_id,)).fetchone()
                if existing and existing["status"] == target:
                    return self._workflow_request(existing)
                raise InvalidWorkflowTransition(request_id)
            final_response = edited_response.strip() if edited_response and edited_response.strip() else draft["content"]
            if final_response != draft["content"]:
                conn.execute("""update public.resolution_drafts set content=%s,
                    version=version+1,updated_at=now() where id=%s""",
                    (final_response, draft["id"]),)
                conn.execute("""update public.messages set content=%s
                    where id=(select m.id from public.messages m
                      join public.support_requests r on r.conversation_id=m.conversation_id
                      where r.id=%s and m.role='assistant' and m.content is not null
                      order by m.sequence_number desc limit 1)""",
                    (final_response, request_id),)
            row = conn.execute(
                """update public.support_requests set status=%s,
                enqueued_at=case when %s then null else enqueued_at end,
                next_enqueue_at=case when %s then now() else next_enqueue_at end,
                completed_at=case when %s then null else now() end,updated_at=now()
                where id=%s and status='AWAITING_APPROVAL' returning *""",
                (target, approve, approve, approve, request_id),
            ).fetchone()
            conn.execute(
                """update public.approval_tasks a set status=%s,assigned_to=%s,
                decision_reason=%s,decided_at=now(),review_necessary=%s,
                approved_action_hash=case when %s then d.action_hash else null end
                from public.resolution_drafts d
                where a.resolution_draft_id=d.id and d.support_request_id=%s""",
                (approval, admin_user_id, reason, review_necessary, approve, request_id),
            )
            conn.execute("""insert into public.audit_events
                (user_id,actor_type,actor_id,event_type,resource_type,resource_id,request_id,detail)
                values(%s,'admin',%s,%s,'support_request',%s,%s,%s)""",
                (admin_user_id, str(admin_user_id),
                 "approval_approved" if approve else "approval_rejected",
                 str(request_id), str(request_id), Jsonb({
                     "reason": reason, "review_necessary": review_necessary,
                     "response_edited": final_response != draft["content"],
                     "draft_version": draft["version"] + (1 if final_response != draft["content"] else 0),
                     "original_response_hash": hashlib.sha256(draft["content"].encode()).hexdigest(),
                     "final_response_hash": hashlib.sha256(final_response.encode()).hexdigest(),
                 })),)
            if not approve:
                conn.execute("update public.support_requests set status='COMPLETED' where id=%s", (request_id,))
                row["status"] = "COMPLETED"
        return self._workflow_request(row)

    def reassign_approval(self, request_id, assignee_id, reason, admin_user_id):
        with self.connection() as conn:
            assignee = conn.execute(
                "select id from public.profiles where id=%s and role='admin'", (assignee_id,)
            ).fetchone()
            if not assignee:
                raise ActionProposalError("Assignee must be an administrator")
            row = conn.execute("""update public.approval_tasks a set assigned_to=%s,
                reassigned_at=now() from public.resolution_drafts d
                where a.resolution_draft_id=d.id and d.support_request_id=%s
                and a.status='pending' returning a.id""", (assignee_id, request_id)).fetchone()
            if not row:
                raise InvalidWorkflowTransition(request_id)
            conn.execute("""insert into public.audit_events
                (user_id,actor_type,actor_id,event_type,resource_type,resource_id,request_id,detail)
                values(%s,'admin',%s,'approval_reassigned','support_request',%s,%s,%s)""",
                (admin_user_id, str(admin_user_id), str(request_id), str(request_id),
                 Jsonb({"assigned_to": str(assignee_id), "reason": reason})),)
        return self.get_approval_detail(request_id)

    def claim_execution(self, request_id, lease_seconds):
        with self.connection() as conn:
            late = conn.execute("""select r.user_id,a.id as approval_id from public.support_requests r
                join public.resolution_drafts d on d.support_request_id=r.id
                join public.approval_tasks a on a.resolution_draft_id=d.id
                where r.id=%s and r.status='APPROVED' and a.expires_at<=now()
                for update of r,a""", (request_id,)).fetchone()
            if late:
                conn.execute("update public.approval_tasks set status='expired',decided_at=now() where id=%s",
                             (late["approval_id"],))
                conn.execute("""update public.support_requests set status='COMPLETED_WITHOUT_ACTION',
                    last_error='approval_expired',completed_at=now(),updated_at=now(),
                    metadata=metadata || '{"customer_status":"The approval deadline passed. No action was taken."}'::jsonb
                    where id=%s""", (request_id,))
                conn.execute("""insert into public.audit_events
                    (user_id,actor_type,actor_id,event_type,resource_type,resource_id,request_id,detail)
                    values(%s,'system','absence-policy','expired_before_execution','support_request',%s,%s,%s)""",
                    (late["user_id"], str(request_id), str(request_id),
                     Jsonb({"action_executed": False})))
                return None
            row = conn.execute(
                """update public.support_requests set status='EXECUTING',
                processing_lease_until=now()+make_interval(secs => %s),updated_at=now()
                where id=%s and (status='APPROVED' or
                (status='EXECUTING' and processing_lease_until<now()))
                and exists (select 1 from public.resolution_drafts d
                  join public.approval_tasks a on a.resolution_draft_id=d.id
                  where d.support_request_id=support_requests.id
                  and a.status='approved' and a.expires_at>now()) returning *""",
                (lease_seconds, request_id),
            ).fetchone()
        return self._workflow_request(row)

    def complete_execution(self, request_id):
        from support_chatbot.actions import action_hash
        key = f"support-request:{request_id}:execute"
        with self.connection() as conn:
            request = conn.execute("select * from public.support_requests where id=%s for update", (request_id,)).fetchone()
            if not request:
                return None
            if request["status"] == "COMPLETED":
                return self._workflow_request(request)
            draft = conn.execute(
                """select d.*,a.approved_action_hash,a.expires_at from public.resolution_drafts d
                join public.approval_tasks a on a.resolution_draft_id=d.id
                where d.support_request_id=%s""", (request_id,),
            ).fetchone()
            action_name = draft["action_name"] or "send_resolution"
            if action_name != "send_resolution" and draft["expires_at"] <= datetime.now(timezone.utc):
                conn.execute(
                    """insert into public.action_executions
                    (idempotency_key,user_id,support_request_id,order_id,action_type,
                     status,request_payload,error_code,result_payload,finished_at)
                    values(%s,%s,%s,%s,%s,'refused','{}'::jsonb,
                     'approval_expired','{"error":"Approval deadline passed"}'::jsonb,now())
                    on conflict(idempotency_key) do nothing""",
                    (key, request["user_id"], request_id, request["order_id"], action_name),
                )
                row = conn.execute(
                    """update public.support_requests set status='COMPLETED_WITHOUT_ACTION',
                    last_error='approval_expired',completed_at=now(),
                    processing_lease_until=null,updated_at=now(),
                    metadata=metadata || '{"customer_status":"The approval deadline passed. No action was taken."}'::jsonb
                    where id=%s returning *""", (request_id,),
                ).fetchone()
                return self._workflow_request(row)
            if action_name != "send_resolution":
                recomputed = action_hash(
                    action_name, dict(draft["action_arguments"]),
                    dict(draft["customer_consequences"]), dict(draft["policy_evidence"]),
                    draft["order_version"],
                )
                if not draft["customer_confirmed_at"] or recomputed != draft["action_hash"] \
                        or recomputed != draft["approved_action_hash"]:
                    conn.execute(
                        """insert into public.action_executions
                        (idempotency_key,user_id,support_request_id,order_id,action_type,
                         status,request_payload,error_code,result_payload,finished_at)
                        values(%s,%s,%s,%s,%s,'failed','{}'::jsonb,
                         'action_seal_mismatch','{"error":"Action seal mismatch"}'::jsonb,now())
                        on conflict(idempotency_key) do nothing""",
                        (key, request["user_id"], request_id, request["order_id"], action_name),
                    )
                    row = conn.execute(
                        """update public.support_requests set status='COMPLETED_WITHOUT_ACTION',
                        last_error='action_seal_mismatch',completed_at=now(),
                        processing_lease_until=null,updated_at=now()
                        where id=%s returning *""", (request_id,),
                    ).fetchone()
                    return self._workflow_request(row)
            inserted = conn.execute(
                """insert into public.action_executions
                (idempotency_key,user_id,support_request_id,order_id,action_type,status,request_payload)
                values(%s,%s,%s,%s,%s,'started',%s)
                on conflict(idempotency_key) do nothing returning id""",
                (key, request["user_id"], request_id, request["order_id"], action_name,
                 Jsonb(draft["proposed_action"] or {})),
            ).fetchone()
            if not inserted:
                return self._workflow_request(request)
            result = {"response": draft["content"]}
            stale = False
            if action_name in {"cancel_order", "start_return"}:
                order = conn.execute(
                    "select * from public.orders where id=%s for update", (request["order_id"],)
                ).fetchone()
                expected_status = "preparing" if action_name == "cancel_order" else "delivered"
                stale = not order or order["version"] != draft["order_version"] or order["status"] != expected_status
                if not stale and action_name == "start_return":
                    days = (date.today() - order["delivered_on"]).days
                    stale = days > RETURN_WINDOW_DAYS
                if not stale:
                    new_status = "cancelled" if action_name == "cancel_order" else "return started"
                    conn.execute(
                        "update public.orders set status=%s,version=version+1,updated_at=now() where id=%s",
                        (new_status, order["id"]),
                    )
                    if action_name == "cancel_order":
                        result = {"cancelled": True, "refund_amount": float(order["price"])}
                        detail = "Order cancelled after customer confirmation and human approval"
                    else:
                        rma = conn.execute(
                            "select 'RMA-' || nextval('public.support_reference_seq') as reference"
                        ).fetchone()["reference"]
                        result = {"rma": rma, "refund_amount": float(order["price"])}
                        detail = f"Return started after approval ({rma})"
                    conn.execute(
                        "insert into public.order_events(order_id,event_type,detail,metadata) values(%s,%s,%s,%s)",
                        (order["id"], action_name, detail, Jsonb({"support_request_id": str(request_id)})),
                    )
            if stale:
                conn.execute(
                    """update public.action_executions set status='failed',error_code='stale_order',
                    result_payload=%s,finished_at=now() where id=%s""",
                    (Jsonb({"error": "Order changed after approval; no action executed"}), inserted["id"]),
                )
                row = conn.execute(
                    """update public.support_requests set status='COMPLETED_WITHOUT_ACTION',
                    last_error='stale_order',completed_at=now(),processing_lease_until=null,
                    updated_at=now() where id=%s returning *""", (request_id,),
                ).fetchone()
                return self._workflow_request(row)
            conn.execute(
                """update public.action_executions set status='succeeded',result_payload=%s,
                finished_at=now() where id=%s""", (Jsonb(result), inserted["id"]),
            )
            row = conn.execute(
                """update public.support_requests set status='COMPLETED',completed_at=now(),
                processing_lease_until=null,updated_at=now() where id=%s
                and status in ('EXECUTING','COMPLETED') returning *""", (request_id,),
            ).fetchone()
        return self._workflow_request(row)

    def process_absence_policy(self):
        with self.connection() as conn:
            reminded = conn.execute(
                """update public.approval_tasks a set reminded_at=now()
                from public.resolution_drafts d, public.support_requests r
                where a.resolution_draft_id=d.id and d.support_request_id=r.id
                and a.status='pending' and a.reminded_at is null
                and a.reminder_at<=now() and a.expires_at>now()
                returning r.id,r.user_id"""
            ).fetchall()
            for row in reminded:
                message = "Human review is taking longer than expected. Your request is still pending and no action has been taken."
                conn.execute("""update public.support_requests set
                    metadata=metadata || jsonb_build_object('customer_status',%s::text),updated_at=now()
                    where id=%s""", (message, row["id"]))
                conn.execute("""insert into public.audit_events
                    (user_id,actor_type,actor_id,event_type,resource_type,resource_id,request_id,detail)
                    values(%s,'system','absence-policy','approval_reminder_sent','support_request',%s,%s,%s)""",
                    (row["user_id"], str(row["id"]), str(row["id"]), Jsonb({"customer_notified": True})))

            escalated = conn.execute(
                """update public.approval_tasks a set escalated_at=now(),
                queue_name='supervisor',assigned_to=null,reassigned_at=now()
                from public.resolution_drafts d, public.support_requests r
                where a.resolution_draft_id=d.id and d.support_request_id=r.id
                and a.status='pending' and a.escalated_at is null
                and a.escalation_at<=now() and a.expires_at>now()
                returning r.id,r.user_id"""
            ).fetchall()
            for row in escalated:
                message = "Your request is delayed and has been escalated to the supervisor queue. No action has been taken."
                conn.execute("""update public.support_requests set
                    metadata=metadata || jsonb_build_object('customer_status',%s::text),updated_at=now()
                    where id=%s""", (message, row["id"]))
                conn.execute("""insert into public.audit_events
                    (user_id,actor_type,actor_id,event_type,resource_type,resource_id,request_id,detail)
                    values(%s,'system','absence-policy','approval_escalated','support_request',%s,%s,%s)""",
                    (row["user_id"], str(row["id"]), str(row["id"]),
                     Jsonb({"queue": "supervisor", "customer_notified": True})))

            expired = conn.execute(
                """with expired as (
                  update public.approval_tasks set status='expired',decided_at=now()
                  where status='pending' and expires_at<=now() returning resolution_draft_id
                ) update public.support_requests r set status='COMPLETED_WITHOUT_ACTION',
                  completed_at=now(),updated_at=now(),
                  metadata=metadata || '{"customer_status":"Human approval was not received before the deadline. No action was taken."}'::jsonb
                  from public.resolution_drafts d, expired e
                  where d.id=e.resolution_draft_id and r.id=d.support_request_id
                  and r.status in ('AWAITING_APPROVAL','EXPIRED')
                  returning r.id,r.user_id"""
            ).fetchall()
            for row in expired:
                conn.execute("""insert into public.audit_events
                    (user_id,actor_type,actor_id,event_type,resource_type,resource_id,request_id,detail)
                    values(%s,'system','absence-policy','approval_expired','support_request',%s,%s,%s)""",
                    (row["user_id"], str(row["id"]), str(row["id"]),
                     Jsonb({"customer_notified": True, "action_executed": False})),)
        return {
            "reminded": [str(row["id"]) for row in reminded],
            "escalated": [str(row["id"]) for row in escalated],
            "expired": [str(row["id"]) for row in expired],
        }

    def expire_approvals(self):
        return self.process_absence_policy()["expired"]

    def record_dead_letter(self, request_id, workflow_run_id, status, body, headers):
        with self.connection() as conn:
            conn.execute(
                """insert into public.workflow_dead_letters
                (support_request_id,workflow_run_id,failure_status,failure_body,failure_headers)
                values(%s,%s,%s,%s,%s) on conflict(support_request_id,workflow_run_id)
                do update set failure_status=excluded.failure_status,
                failure_body=excluded.failure_body,failure_headers=excluded.failure_headers""",
                (request_id, workflow_run_id, status, body[:4000], Jsonb(headers)),
            )
            conn.execute(
                """update public.support_requests set last_error=%s,
                status='COMPLETED_WITHOUT_ACTION', completed_at=now(),
                processing_lease_until=null,next_enqueue_at=now(),updated_at=now(),
                metadata=metadata || jsonb_build_object(
                    'customer_status', %s::text, 'failure_closed', true)
                where id=%s and status not in ('COMPLETED','COMPLETED_WITHOUT_ACTION')""",
                (f"workflow dead-letter: {status}",
                 "Ami could not finish this response. No account action was taken. Please try again.",
                 request_id),
            )

    def session_exists(self, sid):
        with self.connection() as conn:
            return conn.execute("select exists(select 1 from public.conversations where browser_session_id=%s) as found", (sid,)).fetchone()["found"]

    def latest_session_id(self, user_id):
        with self.connection() as conn:
            row = conn.execute("""select browser_session_id from public.conversations
                where user_id=%s order by updated_at desc limit 1""", (user_id,)).fetchone()
        return row["browser_session_id"] if row else None

    def list_conversations(self, limit=50):
        identity = current_identity()
        with self.connection() as conn:
            rows = conn.execute(
                """select c.browser_session_id,
                coalesce(nullif(c.title,''),(select left(m.content,80) from public.messages m
                    where m.conversation_id=c.id and m.role='user'
                    order by m.sequence_number limit 1),'New conversation') as title,
                c.created_at,c.updated_at,
                (select count(*) from public.messages m where m.conversation_id=c.id) as message_count
                from public.conversations c
                where c.user_id=%s and exists (
                    select 1 from public.messages present
                    where present.conversation_id=c.id and present.role='user'
                ) order by c.updated_at desc limit %s""",
                (identity.user_id, limit),
            ).fetchall()
        return [{
            "conversation_id": row["browser_session_id"], "title": row["title"],
            "message_count": row["message_count"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        } for row in rows]

    def rename_conversation(self, sid, title):
        identity = current_identity()
        with self.connection() as conn:
            row = conn.execute(
                """update public.conversations set title=%s,updated_at=now()
                where browser_session_id=%s and user_id=%s returning id""",
                (title.strip(), sid, identity.user_id),
            ).fetchone()
        return bool(row)

    @staticmethod
    def _empty_memory_summary():
        return {"first_seen": date.today().isoformat(), "sessions": [],
                "orders_discussed": [], "actions": [], "escalations": [], "refusals": 0}

    def _rebuild_user_memory(self, conn, user_id):
        """Recompute derived memory so deleted transcripts cannot be recalled."""
        rows = conn.execute(
            """select browser_session_id,working_memory from public.conversations
            where user_id=%s order by created_at""", (user_id,),
        ).fetchall()
        conn.execute("delete from public.memory_facts where user_id=%s", (user_id,))
        conn.execute("delete from public.memory_summaries where user_id=%s", (user_id,))
        if not rows:
            return
        summary = self._empty_memory_summary()
        summary["last_seen"] = date.today().isoformat()
        facts = {}
        for row in rows:
            work = dict(row["working_memory"] or {})
            summary["sessions"].append(row["browser_session_id"])
            for key, values in (
                ("orders_discussed", list(work.get("orders", {}))),
                ("actions", [action for action in work.get("actions", [])
                             if not action.startswith("Escalated to a human")]),
                ("escalations", []),
            ):
                summary[key].extend(value for value in values if value not in summary[key])
            summary["refusals"] = max(summary["refusals"], len(work.get("failures", [])))
            facts.update(work.get("orders", {}))
        conn.execute(
            "insert into public.memory_summaries(user_id,summary) values(%s,%s)",
            (user_id, Jsonb(summary)),
        )
        for order_id, value in facts.items():
            conn.execute(
                """insert into public.memory_facts(user_id,fact_type,fact_key,value)
                values(%s,'order',%s,%s)""", (user_id, order_id, Jsonb(value)),
            )

    def delete_conversation(self, sid):
        identity = current_identity()
        with self.connection() as conn:
            conversation = conn.execute(
                """select id from public.conversations
                where browser_session_id=%s and user_id=%s for update""",
                (sid, identity.user_id),
            ).fetchone()
            if not conversation:
                return False
            active = conn.execute(
                """select 1 from public.support_requests where conversation_id=%s
                and status in ('RECEIVED','QUEUED','DRAFTING','AWAITING_APPROVAL','APPROVED','EXECUTING')
                limit 1""", (conversation["id"],),
            ).fetchone()
            if active:
                raise ConversationBusyError(sid)
            # Completed workflow and action records are regulatory/audit data,
            # not chat history. Keep them but remove customer-authored text.
            conn.execute(
                """update public.resolution_drafts set content='[conversation deleted by customer]'
                where support_request_id in
                  (select id from public.support_requests where conversation_id=%s)""",
                (conversation["id"],),
            )
            conn.execute(
                """update public.support_requests
                set summary='[conversation deleted by customer]',conversation_id=null,
                    metadata=metadata - 'customer_message'
                where conversation_id=%s""", (conversation["id"],),
            )
            conn.execute(
                """insert into public.audit_events
                (user_id,actor_type,actor_id,event_type,resource_type,resource_id,detail)
                values(%s,'customer',%s,'conversation_deleted','conversation',%s,
                       '{"transcript_removed":true,"derived_memory_rebuilt":true}'::jsonb)""",
                (identity.user_id, identity.user_id, sid),
            )
            conn.execute("delete from public.conversations where id=%s", (conversation["id"],))
            self._rebuild_user_memory(conn, identity.user_id)
        return True

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
            for key, values in (("sessions", [session_id]), ("orders_discussed", list(work.get("orders", {}))), ("actions", [action for action in work.get("actions", []) if not action.startswith("Escalated to a human")]), ("escalations", [])):
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
        self.support_requests = {}
        self.request_keys = {}
        self.action_executions = {}
        self.dead_letters = []
        self.action_proposals = {}
        self.audit_events = []
        self.hitl_evaluation_runs = []
        self.support_tickets = {}

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
    def create_escalation(self, summary, conversation_id=None, source_request_id=None):
        identity = current_identity(); reference = f"ESC-{4417 + len(self.support_tickets)}"
        ticket_id = str(uuid.uuid4()); now = datetime.now(timezone.utc).isoformat()
        self.support_tickets[ticket_id] = {
            "id": ticket_id, "reference_number": reference,
            "user_id": identity.user_id if identity else None,
            "conversation_id": conversation_id, "browser_session_id": conversation_id,
            "source_request_id": source_request_id, "summary": summary, "status": "open",
            "resolution": None, "assigned_to": None, "assignee_name": None,
            "customer_email": identity.email if identity else None,
            "customer_name": identity.display_name if identity else None,
            "created_at": now, "updated_at": now, "resolved_at": None,
        }
        return reference
    def list_tickets(self, status="all", limit=100):
        identity = current_identity(); rows = []
        for row in reversed(list(self.support_tickets.values())):
            if identity and (identity.role == "admin" or row["user_id"] == identity.user_id):
                if status == "all" or row["status"] == status: rows.append(copy.deepcopy(row))
            if len(rows) >= limit: break
        return rows
    def update_ticket(self, ticket_id, status, resolution, admin_user_id):
        row = self.support_tickets.get(str(ticket_id))
        if not row: return None
        if status not in {"in_progress", "resolved", "closed"}: raise InvalidWorkflowTransition(status)
        allowed = {
            "open": {"in_progress", "resolved"},
            "in_progress": {"resolved"},
            "resolved": {"closed"},
            "closed": set(),
        }
        if status not in allowed.get(row["status"], set()):
            raise InvalidWorkflowTransition(
                f"Ticket cannot move from {row['status']} to {status}"
            )
        row.update({"status": status,
                    "assigned_to": admin_user_id, "updated_at": datetime.now(timezone.utc).isoformat(),
                    "resolved_at": (datetime.now(timezone.utc).isoformat()
                                    if status == "resolved" else row.get("resolved_at"))})
        if status == "resolved":
            row["resolution"] = resolution or None
        return copy.deepcopy(row)
    def create_support_request(self, summary, planner, idempotency_key, session_id=None):
        identity = current_identity()
        payload_hash = hashlib.sha256(f"{planner}\0{summary}".encode()).hexdigest()
        key = (identity.user_id, idempotency_key)
        existing_id = self.request_keys.get(key)
        if existing_id:
            existing = self.support_requests[existing_id]
            if existing["payload_hash"] != payload_hash: raise IdempotencyConflictError(idempotency_key)
            return copy.deepcopy(existing), False
        if session_id and any(
            row.get("conversation_id") == session_id
            and row.get("status") in {"RECEIVED", "QUEUED", "DRAFTING"}
            for row in self.support_requests.values()
        ):
            raise ConversationBusyError(session_id)
        request_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        row = {"id": request_id, "reference_number": f"REQ-{1001 + len(self.support_requests)}",
               "user_id": identity.user_id, "conversation_id": session_id,
               "browser_session_id": session_id,
               "summary": summary, "status": "RECEIVED", "metadata": {"planner": planner},
               "idempotency_key": idempotency_key, "payload_hash": payload_hash,
               "workflow_run_id": None, "enqueue_attempts": 0, "enqueued_at": None,
               "next_enqueue_at": datetime.now(timezone.utc).isoformat(), "last_error": None,
               "processing_lease_until": None, "draft_content": None,
               "proposed_action": None, "approval_status": None,
               "created_at": created_at, "updated_at": created_at, "completed_at": None}
        self.support_requests[request_id] = row; self.request_keys[key] = request_id
        return copy.deepcopy(row), True
    def create_action_proposal(self, action, arguments):
        from support_chatbot.actions import build_proposal
        identity = current_identity(); order = self.get_order(arguments.get("order_id", ""))
        try: proposal = build_proposal(action, arguments, order)
        except ValueError as error: raise ActionProposalError(str(error)) from error
        proposal_id = str(uuid.uuid4())
        row = proposal | {"proposal_id": proposal_id, "user_id": identity.user_id,
                          "status": "previewed", "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()}
        self.action_proposals[proposal_id] = row
        return copy.deepcopy(row)
    def confirm_action_proposal(self, proposal_id, supplied_hash, idempotency_key):
        identity = current_identity(); proposal = self.action_proposals.get(str(proposal_id))
        if not proposal or not self._visible(proposal["user_id"]): raise ActionProposalError("Proposal not found")
        if proposal["action_hash"] != supplied_hash: raise ActionProposalError("Action hash does not match the preview")
        key = (identity.user_id, idempotency_key); existing_id = self.request_keys.get(key)
        if existing_id:
            existing = self.support_requests[existing_id]
            if existing["metadata"].get("action_hash") != supplied_hash: raise IdempotencyConflictError(idempotency_key)
            return copy.deepcopy(existing), False
        if proposal["status"] != "previewed": raise ActionProposalError("Proposal is expired or already confirmed")
        request_id = str(uuid.uuid4())
        reminder_seconds, escalation_seconds, expiry_seconds = settings.approval_deadlines
        now = datetime.now(timezone.utc)
        row = {"id": request_id, "reference_number": f"REQ-{1001 + len(self.support_requests)}",
               "user_id": identity.user_id, "summary": f"Confirmed proposal to {proposal['action']}",
               "status": "AWAITING_APPROVAL", "metadata": {"proposal_id": proposal_id, "action_hash": supplied_hash},
               "idempotency_key": idempotency_key, "payload_hash": hashlib.sha256(supplied_hash.encode()).hexdigest(),
               "workflow_run_id": None, "enqueue_attempts": 0, "enqueued_at": None,
               "next_enqueue_at": datetime.now(timezone.utc).isoformat(), "last_error": None,
               "processing_lease_until": None, "draft_content": "Confirmed privileged action",
               "proposed_action": {"type": proposal["action"], "arguments": proposal["arguments"]},
               "action_name": proposal["action"], "action_arguments": copy.deepcopy(proposal["arguments"]),
               "customer_consequences": copy.deepcopy(proposal["consequences"]),
               "policy_evidence": copy.deepcopy(proposal["policy_evidence"]),
               "order_version": proposal["order_version"], "action_hash": supplied_hash,
               "customer_confirmed_at": datetime.now(timezone.utc).isoformat(),
               "approval_status": "pending", "approved_action_hash": None,
               "reminder_at": (now + timedelta(seconds=reminder_seconds)).isoformat(),
               "escalation_at": (now + timedelta(seconds=escalation_seconds)).isoformat(),
               "expires_at": (now + timedelta(seconds=expiry_seconds)).isoformat(),
               "queue_name": "review", "reminded_at": None, "escalated_at": None}
        row.update({"created_at": now.isoformat(), "updated_at": now.isoformat(),
                    "approval_created_at": now.isoformat(), "completed_at": None})
        self.support_requests[request_id] = row; self.request_keys[key] = request_id
        proposal["status"] = "confirmed"
        self.audit_events.append({"event_type": "approval_created", "actor_id": identity.user_id,
                                  "resource_id": request_id,
                                  "detail": {"action": proposal["action"],
                                             "deadlines_seconds": {
                                                 "reminder": reminder_seconds,
                                                 "escalation": escalation_seconds,
                                                 "expiry": expiry_seconds,
                                             }},
                                  "created_at": datetime.now(timezone.utc).isoformat()})
        return copy.deepcopy(row), True
    def get_support_request(self, request_id):
        row = self.support_requests.get(str(request_id))
        return copy.deepcopy(row) if row and self._visible(row["user_id"]) else None
    def list_support_requests(self, limit=50):
        rows = [row for row in self.support_requests.values() if self._visible(row["user_id"])]
        return copy.deepcopy(rows[-limit:])
    def get_request_agent_context(self, request_id):
        row = self.support_requests.get(str(request_id))
        if not row: return None
        profile = next(p for p in self.profiles.values() if p["id"] == row["user_id"])
        return {"identity": Identity(profile["id"], profile["email"], profile["display_name"], profile["role"]),
                "session_id": row.get("conversation_id"),
                "planner": row.get("metadata", {}).get("planner", "react")}
    def mark_enqueued(self, request_id, workflow_run_id):
        row = self.support_requests.get(str(request_id))
        if not row or row["status"] not in {"RECEIVED", "QUEUED", "APPROVED"}: return None
        if row["status"] == "RECEIVED": row["status"] = "QUEUED"
        row["workflow_run_id"] = workflow_run_id; row["enqueued_at"] = datetime.now(timezone.utc).isoformat(); row["last_error"] = None
        return copy.deepcopy(row)
    def mark_enqueue_failed(self, request_id, error, max_attempts=5):
        row = self.support_requests.get(str(request_id))
        if not row: return None
        row["enqueue_attempts"] += 1; row["last_error"] = str(error)[:500]
        row["next_enqueue_at"] = datetime.now(timezone.utc).isoformat()
        if row["enqueue_attempts"] >= max_attempts:
            row["status"] = "COMPLETED_WITHOUT_ACTION"
            row["completed_at"] = datetime.now(timezone.utc).isoformat()
            row.setdefault("metadata", {})["customer_status"] = (
                "We could not start this request after repeated attempts. No action was taken."
            )
        return copy.deepcopy(row)
    def pending_enqueues(self, limit=100):
        return [copy.deepcopy(row) for row in self.support_requests.values()
                if row["status"] in {"RECEIVED", "APPROVED"}][:limit]
    def claim_drafting(self, request_id, lease_seconds):
        row = self.support_requests.get(str(request_id))
        if not row or row["status"] not in {"RECEIVED", "QUEUED"}: return None
        row["status"] = "DRAFTING"; row["processing_lease_until"] = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        return copy.deepcopy(row)
    def save_resolution_draft(self, request_id, content, proposed_action, approval_deadlines,
                              requires_hitl=True, routing_reason=None):
        row = self.support_requests.get(str(request_id))
        if not row or row["status"] != "DRAFTING": return None
        proposed_action = proposed_action or {"type": "send_resolution"}
        reminder_seconds, escalation_seconds, expiry_seconds = approval_deadlines
        now = datetime.now(timezone.utc)
        row.update({"status": "AWAITING_APPROVAL" if requires_hitl else "COMPLETED",
                    "draft_content": content,
                    "proposed_action": copy.deepcopy(proposed_action),
                    "approval_status": "pending" if requires_hitl else None,
                    "action_name": proposed_action.get("type", "send_resolution"),
                    "action_arguments": copy.deepcopy(proposed_action.get("arguments") or {}),
                    "customer_consequences": copy.deepcopy(proposed_action.get("customer_consequences") or {}),
                    "policy_evidence": copy.deepcopy(proposed_action.get("policy_evidence") or {}),
                    "order_version": proposed_action.get("order_version"),
                    "action_hash": proposed_action.get("action_hash"),
                    "customer_confirmed_at": (datetime.now(timezone.utc).isoformat()
                                              if proposed_action.get("customer_confirmed") else None),
                    "reminder_at": (now + timedelta(seconds=reminder_seconds)).isoformat(),
                    "escalation_at": (now + timedelta(seconds=escalation_seconds)).isoformat(),
                    "expires_at": (now + timedelta(seconds=expiry_seconds)).isoformat(),
                    "requires_hitl": requires_hitl, "routing_reason": routing_reason,
                    "completed_at": (None if requires_hitl else datetime.now(timezone.utc).isoformat()),
                    "queue_name": "review" if requires_hitl else None,
                    "reminded_at": None, "escalated_at": None,
                    "processing_lease_until": None})
        row["drafted_at"] = datetime.now(timezone.utc).isoformat()
        row["updated_at"] = row["drafted_at"]
        if requires_hitl:
            row["approval_created_at"] = row["drafted_at"]
        self.audit_events.append({"event_type": ("approval_created" if requires_hitl
                                                   else "resolution_completed_without_review"),
                                  "actor_id": "workflow",
                                  "resource_id": str(request_id),
                                  "detail": {"action": row["action_name"],
                                             "routing_reason": routing_reason,
                                             "deadlines_seconds": {
                                                 "reminder": reminder_seconds,
                                                 "escalation": escalation_seconds,
                                                 "expiry": expiry_seconds,
                                             } if requires_hitl else {}},
                                  "created_at": datetime.now(timezone.utc).isoformat()})
        return copy.deepcopy(row)
    def list_approval_tasks(self, status="pending"):
        now = datetime.now(timezone.utc)
        rows = []
        for row in self.support_requests.values():
            task_status = row.get("approval_status")
            expires = datetime.fromisoformat(row["expires_at"]) if row.get("expires_at") else now
            matches = (
                (status == "pending" and task_status == "pending" and expires > now
                 and row.get("queue_name", "review") == "review")
                or (status == "overdue" and task_status == "pending" and expires > now
                    and row.get("queue_name") == "supervisor")
                or task_status == status
                or status == "all"
            )
            if matches:
                rows.append({"request_id": row["id"], "reference_number": row["reference_number"],
                             "customer_request": row["summary"], "status": task_status,
                             "request_status": row["status"], "expires_at": row.get("expires_at"),
                             "reminder_at": row.get("reminder_at"),
                             "escalation_at": row.get("escalation_at"),
                             "escalated_at": row.get("escalated_at"),
                             "queue_name": row.get("queue_name", "review"),
                             "review_necessary": row.get("review_necessary")})
        return copy.deepcopy(rows)

    def get_approval_detail(self, request_id):
        row = self.support_requests.get(str(request_id))
        if not row:
            return None
        result = copy.deepcopy(row)
        result.update({"request_id": row["id"], "customer_request": row["summary"],
                       "proposed_response": row.get("draft_content"),
                       "audit_history": [copy.deepcopy(event) for event in self.audit_events
                                         if event["resource_id"] == str(request_id)]})
        return result

    def list_admin_reviewers(self):
        return [{"id": p["id"], "email": p["email"], "display_name": p["display_name"]}
                for p in self.profiles.values() if p["role"] == "admin"]

    def hitl_operational_metrics(self):
        rows = list(self.support_requests.values())
        decided = [row for row in rows if row.get("approval_status") in {"approved", "rejected"}]
        necessary = [row for row in decided if row.get("review_necessary") is True]
        return {
            "decided": len(decided),
            "marked_necessary": len(necessary),
            "unlabeled_decisions": sum(row.get("review_necessary") is None for row in decided),
            "pending": sum(row.get("approval_status") == "pending" for row in rows),
            "expired": sum(row.get("approval_status") == "expired" for row in rows),
            "escalation_precision": round(100 * len(necessary) / len(decided), 1) if decided else None,
        }

    def operational_metrics(self):
        from support_chatbot.operational import age_seconds
        rows = list(self.support_requests.values())
        active = {"RECEIVED", "QUEUED", "DRAFTING", "APPROVED", "EXECUTING"}
        approvals = [row for row in rows if row.get("approval_status")]
        pending = [row for row in approvals if row.get("approval_status") == "pending"]

        def elapsed(start, end):
            if not start or not end:
                return None
            return max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000)

        def percentile(values, fraction):
            values = sorted(value for value in values if value is not None)
            if not values:
                return None
            return round(values[min(round((len(values) - 1) * fraction), len(values) - 1)], 1)

        drafting = [elapsed(row.get("created_at"), row.get("drafted_at") or row.get("updated_at"))
                    for row in rows if row.get("draft_content")]
        approval_wait = [elapsed(row.get("approval_created_at") or row.get("updated_at"),
                                 row.get("decided_at")) for row in approvals]
        resume = [elapsed(row.get("decided_at"), row.get("execution_started_at")) for row in rows]
        resolution = [elapsed(row.get("created_at"), row.get("completed_at")) for row in rows]
        decisions = {}
        for row in approvals:
            status = row.get("approval_status")
            decisions[status] = decisions.get(status, 0) + 1
        result = {
            "queue_depth": sum(row["status"] in active for row in rows),
            "pending_approvals": len(pending),
            "oldest_pending_approval_seconds": max(
                (age_seconds(row.get("approval_created_at") or row.get("updated_at")) or 0
                 for row in pending), default=None),
            "retry_count": sum(max(row.get("enqueue_attempts", 0) - 1, 0) for row in rows),
            "dead_letter_count": len(self.dead_letters),
            "expired_approvals": sum(row.get("approval_status") == "expired" for row in rows),
            "drafting_p50_ms": percentile(drafting, .5), "drafting_p95_ms": percentile(drafting, .95),
            "approval_wait_p50_ms": percentile(approval_wait, .5), "approval_wait_p95_ms": percentile(approval_wait, .95),
            "resume_p50_ms": percentile(resume, .5), "resume_p95_ms": percentile(resume, .95),
            "resolution_p50_ms": percentile(resolution, .5), "resolution_p95_ms": percentile(resolution, .95),
            "decision_breakdown": decisions,
        }
        result.update(self.hitl_operational_metrics())
        latest = self.latest_hitl_evaluation() or {}
        summary = latest.get("summary") or {}
        result.update({"hitl_recall": summary.get("hitl_recall"),
                       "hitl_recall_numerator": summary.get("hitl_recall_numerator", 0),
                       "hitl_recall_denominator": summary.get("hitl_recall_denominator", 0)})
        return result

    def operational_requests(self, limit=100):
        return [self._operational_request(row) for row in
                list(self.support_requests.values())[-limit:][::-1]]

    def _operational_request(self, row):
        from support_chatbot.operational import explain_state, safe_error_code
        allowed = {"id", "reference_number", "status", "created_at", "updated_at",
                   "enqueued_at", "completed_at", "enqueue_attempts", "last_error",
                   "action_name", "requires_hitl", "routing_reason", "drafted_at",
                   "approval_status", "approval_created_at", "decided_at", "expires_at",
                   "execution_started_at", "execution_finished_at", "execution_status",
                   "error_code", "dead_letters"}
        result = {key: copy.deepcopy(value) for key, value in row.items() if key in allowed}
        result["id"] = str(row["id"])
        result["state_explanation"] = explain_state(row["status"])
        if result.get("last_error"):
            result["last_error"] = safe_error_code(result["last_error"])
        result.setdefault("dead_letters", sum(item["request_id"] == str(row["id"])
                                              for item in self.dead_letters))
        return result

    def operational_request_timeline(self, request_id):
        from support_chatbot.operational import safe_audit_detail
        row = self.support_requests.get(str(request_id))
        if not row:
            return None
        result = self._operational_request(row)
        timeline = [
            {"event": event["event_type"],
             "actor": ("system" if event.get("actor_id") in {"workflow", "absence-policy"}
                       else "reviewer"),
             "at": event.get("created_at"), "detail": safe_audit_detail(event.get("detail"))}
            for event in self.audit_events if event.get("resource_id") == str(request_id)
        ]
        milestones = (
            ("request_received", row.get("created_at")),
            ("request_enqueued", row.get("enqueued_at")),
            ("resolution_drafted", row.get("drafted_at")),
            ("approval_wait_started", row.get("approval_created_at")),
            (f"approval_{row.get('approval_status')}", row.get("decided_at")),
            ("execution_started", row.get("execution_started_at")),
            ("request_completed", row.get("completed_at")),
        )
        timeline.extend({"event": name, "actor": "system", "at": moment, "detail": {}}
                        for name, moment in milestones if moment)
        timeline.sort(key=lambda event: event.get("at") or "")
        result["timeline"] = timeline
        return result

    def save_hitl_evaluation(self, report):
        saved = copy.deepcopy(report)
        saved["run_id"] = str(uuid.uuid4())
        self.hitl_evaluation_runs.append(saved)
        return copy.deepcopy(saved)

    def latest_hitl_evaluation(self):
        return copy.deepcopy(self.hitl_evaluation_runs[-1]) if self.hitl_evaluation_runs else None

    def decide_support_request(self, request_id, approve, reason, admin_user_id,
                               edited_response=None, review_necessary=None):
        row = self.support_requests.get(str(request_id)); target = "APPROVED" if approve else "COMPLETED"
        if not row: raise InvalidWorkflowTransition(request_id)
        if row.get("expires_at") and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            self.process_absence_policy()
            raise InvalidWorkflowTransition(request_id)
        if row["status"] == target: return copy.deepcopy(row)
        if row["status"] != "AWAITING_APPROVAL": raise InvalidWorkflowTransition(request_id)
        edited = bool(edited_response and edited_response.strip()
                      and edited_response.strip() != row.get("draft_content"))
        if edited:
            row["draft_content"] = edited_response.strip()
            session = self.sessions.get(row.get("conversation_id"))
            if session:
                for message in reversed(session.get("history", [])):
                    if message.get("role") == "assistant" and message.get("content"):
                        message["content"] = row["draft_content"]
                        break
        row.update({"status": target, "approval_status": "approved" if approve else "rejected",
                    "decision_reason": reason, "assigned_to": admin_user_id,
                    "review_necessary": review_necessary,
                    "decided_at": datetime.now(timezone.utc).isoformat()})
        if approve: row["approved_action_hash"] = row.get("action_hash")
        self.audit_events.append({"event_type": "approval_approved" if approve else "approval_rejected",
                                  "actor_id": admin_user_id, "resource_id": str(request_id),
                                  "detail": {"reason": reason, "review_necessary": review_necessary,
                                             "response_edited": edited},
                                  "created_at": datetime.now(timezone.utc).isoformat()})
        return copy.deepcopy(row)
    def reassign_approval(self, request_id, assignee_id, reason, admin_user_id):
        row = self.support_requests.get(str(request_id))
        assignee = next((p for p in self.profiles.values()
                         if p["id"] == str(assignee_id) and p["role"] == "admin"), None)
        if not assignee: raise ActionProposalError("Assignee must be an administrator")
        if not row or row.get("approval_status") != "pending": raise InvalidWorkflowTransition(request_id)
        row["assigned_to"] = str(assignee_id)
        self.audit_events.append({"event_type": "approval_reassigned", "actor_id": admin_user_id,
                                  "resource_id": str(request_id),
                                  "detail": {"assigned_to": str(assignee_id), "reason": reason},
                                  "created_at": datetime.now(timezone.utc).isoformat()})
        return self.get_approval_detail(request_id)
    def claim_execution(self, request_id, lease_seconds):
        row = self.support_requests.get(str(request_id))
        if not row or row["status"] != "APPROVED": return None
        if row.get("expires_at") and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            row["status"] = "COMPLETED_WITHOUT_ACTION"
            row["last_error"] = "approval_expired"
            row.setdefault("metadata", {})["customer_status"] = "The approval deadline passed. No action was taken."
            return None
        row["status"] = "EXECUTING"; row["processing_lease_until"] = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        row["execution_started_at"] = datetime.now(timezone.utc).isoformat()
        return copy.deepcopy(row)
    def complete_execution(self, request_id):
        from support_chatbot.actions import action_hash
        row = self.support_requests.get(str(request_id))
        if not row: return None
        key = f"support-request:{request_id}:execute"
        if key in self.action_executions: return copy.deepcopy(row)
        action = row.get("action_name")
        if action and action != "send_resolution":
            computed = action_hash(action, row["action_arguments"], row["customer_consequences"], row["policy_evidence"], row["order_version"])
            if not row.get("customer_confirmed_at") or computed != row.get("action_hash") or computed != row.get("approved_action_hash"):
                self.action_executions[key] = {
                    "status": "failed", "error": "action_seal_mismatch"
                }
                row["status"] = "COMPLETED_WITHOUT_ACTION"
                row["last_error"] = "action_seal_mismatch"
                return copy.deepcopy(row)
            order = self.orders.get(row["action_arguments"]["order_id"])
            expected = "preparing" if action == "cancel_order" else "delivered"
            if not order or order["version"] != row["order_version"] or order["status"] != expected:
                self.action_executions[key] = {"status": "failed", "error": "stale_order"}
                row["status"] = "COMPLETED_WITHOUT_ACTION"; row["last_error"] = "stale_order"
                return copy.deepcopy(row)
            order["status"] = "cancelled" if action == "cancel_order" else "return started"
            order["version"] += 1
        self.action_executions[key] = {"status": "succeeded", "response": row.get("draft_content")}
        if row["status"] == "EXECUTING":
            row["status"] = "COMPLETED"
            row["completed_at"] = datetime.now(timezone.utc).isoformat()
            row["execution_finished_at"] = row["completed_at"]
        return copy.deepcopy(row)
    def process_absence_policy(self, now=None):
        reminded, escalated, expired = [], [], []
        now = now or datetime.now(timezone.utc)
        for row in self.support_requests.values():
            if row["status"] != "AWAITING_APPROVAL" or row.get("approval_status") != "pending":
                continue
            expires_at = datetime.fromisoformat(row["expires_at"])
            if not row.get("reminded_at") and datetime.fromisoformat(row["reminder_at"]) <= now < expires_at:
                row["reminded_at"] = now.isoformat(); reminded.append(row["id"])
                row.setdefault("metadata", {})["customer_status"] = "Human review is taking longer than expected. Your request is still pending and no action has been taken."
                self.audit_events.append({"event_type": "approval_reminder_sent", "actor_id": "absence-policy",
                                          "resource_id": row["id"], "detail": {"customer_notified": True},
                                          "created_at": now.isoformat()})
            if not row.get("escalated_at") and datetime.fromisoformat(row["escalation_at"]) <= now < expires_at:
                row["escalated_at"] = now.isoformat(); row["queue_name"] = "supervisor"
                row["assigned_to"] = None; escalated.append(row["id"])
                row.setdefault("metadata", {})["customer_status"] = "Your request is delayed and has been escalated to the supervisor queue. No action has been taken."
                self.audit_events.append({"event_type": "approval_escalated", "actor_id": "absence-policy",
                                          "resource_id": row["id"],
                                          "detail": {"queue": "supervisor", "customer_notified": True},
                                          "created_at": now.isoformat()})
            if expires_at <= now:
                row["status"] = "COMPLETED_WITHOUT_ACTION"; row["approval_status"] = "expired"; expired.append(row["id"])
                row.setdefault("metadata", {})["customer_status"] = "Human approval was not received before the deadline. No action was taken."
                self.audit_events.append({"event_type": "approval_expired", "actor_id": "absence-policy",
                                          "resource_id": row["id"],
                                          "detail": {"customer_notified": True, "action_executed": False},
                                          "created_at": now.isoformat()})
        return {"reminded": reminded, "escalated": escalated, "expired": expired}
    def expire_approvals(self):
        return self.process_absence_policy()["expired"]
    def record_dead_letter(self, request_id, workflow_run_id, status, body, headers):
        entry = {"request_id": str(request_id), "workflow_run_id": workflow_run_id, "status": status, "body": body, "headers": headers}
        if entry not in self.dead_letters: self.dead_letters.append(entry)
        row = self.support_requests.get(str(request_id))
        if row and row.get("status") not in {"COMPLETED", "COMPLETED_WITHOUT_ACTION"}:
            row["status"] = "COMPLETED_WITHOUT_ACTION"
            row["completed_at"] = datetime.now(timezone.utc).isoformat()
            row["last_error"] = f"workflow dead-letter: {status}"
            row.setdefault("metadata", {})["customer_status"] = (
                "Ami could not finish this response. No account action was taken. Please try again."
            )
    def session_exists(self, sid): return sid in self.sessions
    def latest_session_id(self, user_id):
        matches = [sid for sid, value in self.sessions.items() if value.get("user_id") == user_id]
        return matches[-1] if matches else None
    def list_conversations(self, limit=50):
        identity = current_identity()
        rows = []
        for sid, value in reversed(list(self.sessions.items())):
            if not identity or value.get("user_id") != identity.user_id:
                continue
            first = next((m.get("content") for m in value.get("history", [])
                          if m.get("role") == "user" and m.get("content")), None)
            if not first:
                continue
            rows.append({"conversation_id": sid,
                         "title": (value.get("title") or first or "New conversation")[:100],
                         "message_count": len([m for m in value.get("history", [])
                                               if m.get("role") in {"user", "assistant"}]),
                         "created_at": None, "updated_at": None})
            if len(rows) >= limit: break
        return copy.deepcopy(rows)
    def rename_conversation(self, sid, title):
        identity = current_identity(); value = self.sessions.get(sid)
        if not value or not identity or value.get("user_id") != identity.user_id: return False
        value["title"] = title.strip()
        return True
    def _rebuild_memory_for_user(self, user_id):
        profile = next((p for p in self.profiles.values() if p["id"] == user_id), None)
        if not profile: return
        self.memories.pop(profile["email"], None)
        for sid, value in self.sessions.items():
            if value.get("user_id") == user_id:
                self.remember(value.get("work", {}), sid)
    def delete_conversation(self, sid):
        identity = current_identity(); value = self.sessions.get(sid)
        if not value or not identity or value.get("user_id") != identity.user_id: return False
        if any(row.get("conversation_id") == sid and row.get("status") in {
            "RECEIVED", "QUEUED", "DRAFTING", "AWAITING_APPROVAL", "APPROVED", "EXECUTING"
        } for row in self.support_requests.values()):
            raise ConversationBusyError(sid)
        for row in self.support_requests.values():
            if row.get("conversation_id") == sid:
                row["conversation_id"] = None; row["browser_session_id"] = None
                row["summary"] = "[conversation deleted by customer]"
                row.pop("draft_content", None)
        del self.sessions[sid]
        self.audit_events.append({"event_type": "conversation_deleted",
                                  "actor_id": identity.user_id, "resource_id": sid,
                                  "detail": {"transcript_removed": True,
                                             "derived_memory_rebuilt": True},
                                  "created_at": datetime.now(timezone.utc).isoformat()})
        self._rebuild_memory_for_user(identity.user_id)
        return True
    def create_session(self, sid, work, user_id=None):
        identity = current_identity(); owner = user_id or (identity.user_id if identity else None)
        self.sessions.setdefault(sid, {"history": [], "work": copy.deepcopy(work), "user_id": owner})
    def load_session(self, sid):
        value = self.sessions.get(sid)
        return copy.deepcopy(value) if value and self._visible(value.get("user_id")) else None
    def save_session(self, sid, history, work, planner="react"):
        identity = current_identity(); previous = self.sessions.get(sid)
        if previous and not self._visible(previous.get("user_id")): return
        self.sessions[sid] = {"history": copy.deepcopy(history), "work": copy.deepcopy(work),
                              "planner": planner,
                              "user_id": identity.user_id if identity else (previous or {}).get("user_id")}
    def reset_session(self, sid, work):
        previous = self.sessions.get(sid, {})
        self.sessions[sid] = {"history": [], "work": copy.deepcopy(work),
                              "user_id": previous.get("user_id"),
                              "planner": previous.get("planner", "react")}
    def remember(self, work, session_id):
        email = work.get("customer_email")
        if not email: return
        rec = self.memories.setdefault(email.lower(), {"first_seen": date.today().isoformat(), "sessions": [], "orders_discussed": [], "actions": [], "escalations": [], "refusals": 0})
        rec["last_seen"] = date.today().isoformat()
        for key, values in (("sessions", [session_id]), ("orders_discussed", list(work.get("orders", {}))), ("actions", [action for action in work.get("actions", []) if not action.startswith("Escalated to a human")]), ("escalations", [])):
            rec[key].extend(value for value in values if value not in rec[key])
        rec["refusals"] = max(rec["refusals"], len(work.get("failures", [])))
    def recall(self, email):
        normalized = (email or "").lower()
        if self.enforce_auth:
            identity = current_identity()
            profile = self.profiles.get(normalized)
            if not identity or not profile or (
                identity.role != "admin" and identity.user_id != profile["id"]
            ):
                return None
        return copy.deepcopy(self.memories.get(normalized))
    def save_feedback(self, entry): self.feedback.append(copy.deepcopy(entry))


_repository = PostgresRepository()


def get_repository():
    return _repository


def set_repository(repository):
    global _repository
    previous, _repository = _repository, repository
    return previous
