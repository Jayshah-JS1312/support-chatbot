"""Element 2 of the agent: MEMORY.

There are two kinds, and mixing them up is the usual beginner mistake.

CONVERSATION MEMORY — what was SAID.
    The transcript. Grows every turn, gets sent to the model on every call,
    and is the thing that makes turn 5 understand the word "it". Its job is
    recall, and its enemy is length.

WORKING MEMORY — what is KNOWN and DONE.
    The agent's scratchpad for the current task: facts confirmed by tools,
    actions already taken, the goal it is chasing. It is small, structured,
    and rewritten as the agent learns. Its job is not to remember words but
    to stop the agent repeating itself.

You can see the difference in one question: "did I already escalate this?"
The transcript can answer it only by re-reading everything. Working memory
answers it by looking at one field.

LONG-TERM MEMORY — what is known about a customer across conversations.
It is stored as verified facts and a compact PostgreSQL summary.
WorkingMemory also gains two fields, `turn` and
`pending`, which the policy layer uses to make a confirmation span two
turns.
"""

import json
import re
from support_chatbot.persistence import get_repository


class ConversationMemory:
    """The dialogue transcript, in the shape the model expects."""

    def __init__(self, system_prompt, max_turns=40):
        self.system = system_prompt
        self.history = []          # everything after the system message
        self.max_turns = max_turns

    def add_user(self, text):
        self.history.append({"role": "user", "content": text})

    def add_assistant(self, message):
        """Takes the raw model message (it may carry tool_calls)."""
        self.history.append(message)

    def add_observation(self, tool_call_id, result):
        self.history.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result),
        })

    def persist_safe_reply(self, raw_reply, safe_reply):
        """Make the customer-safe reply the only persisted final response.

        Planners append their final model response before the output policy has
        inspected it. Replace that exact terminal response after sanitization;
        if a planner failed or exhausted its steps without appending a reply,
        append the safe fallback instead.
        """
        if self.history and self.history[-1].get("role") == "assistant" \
                and self.history[-1].get("content") == raw_reply \
                and not self.history[-1].get("tool_calls"):
            self.history[-1] = {"role": "assistant", "content": safe_reply}
            return
        self.history.append({"role": "assistant", "content": safe_reply})

    def messages(self, extra_system=None):
        """What we actually send. `extra_system` is where working memory rides in."""
        head = [{"role": "system", "content": self.system}]
        if extra_system:
            head.append({"role": "system", "content": extra_system})
        return head + self._trimmed()

    def _trimmed(self):
        """Keep the transcript from growing forever.

        We trim from the front, but never leave a 'tool' message stranded
        without the assistant message that requested it — the API rejects that.
        """
        if len(self.history) <= self.max_turns:
            return self.history
        cut = len(self.history) - self.max_turns
        while cut < len(self.history) and self.history[cut].get("role") == "tool":
            cut += 1
        return self.history[cut:]

    def __len__(self):
        return len(self.history)

    def public_transcript(self):
        """Return only dialogue that is safe and useful to redraw for a user.

        Planner tool requests and tool observations are part of the model
        transcript but are operator internals, not customer-visible messages.
        """
        return [
            {"role": message["role"], "content": message["content"]}
            for message in self.history
            if message.get("role") in {"user", "assistant"}
            and isinstance(message.get("content"), str)
            and message["content"]
        ]

    # -- persistence ------------------------------------------------------

    def to_dict(self):
        return {"history": self.history, "max_turns": self.max_turns}

    @classmethod
    def from_dict(cls, system_prompt, data):
        m = cls(system_prompt, max_turns=data.get("max_turns", 40))
        m.history = data.get("history", [])
        return m


class WorkingMemory:
    """What the agent has established during this task.

    Updated from tool observations, never from the customer's claims —
    a customer saying "my order shipped" is not a fact, a tool saying it is.
    """

    def __init__(self):
        self.customer_email = None
        self.orders = {}        # order_id -> what we looked up
        self.active_order_id = None  # one verified order referred to most recently
        self.actions = []       # things that actually changed something
        self.failures = []      # what we tried that was refused, and why
        self.escalation = None  # ticket number, once we have one
        self.turn = 0           # user messages so far — the policy layer uses
        self.pending = None     # this to insist a confirmation spans a turn

    # -- writing ----------------------------------------------------------

    def record(self, tool, args, result):
        """Fold one Observation into what we know."""
        order_id = args.get("order_id")
        if order_id and tool in {
            "get_order", "track_package", "cancel_order", "start_return",
        } and not result.get("retry") and (
            not result.get("error") or order_id in self.orders
        ):
            # The tool boundary enforces ownership. Remember a successful
            # lookup, or an ineligible operation for an order already verified.
            self.active_order_id = order_id
        if result.get("needs_confirmation"):
            return                     # a preview changes nothing yet
        if result.get("proposal"):
            oid = result.get("arguments", {}).get("order_id")
            if oid:
                facts = {"version": result.get("order_version")}
                observed = result.get("policy_evidence", {}).get("observed_status")
                if observed:
                    facts["status"] = observed
                self.orders.setdefault(oid, {}).update(facts)
            return                     # proposals are not completed actions
        if tool == "find_orders" and "orders" in result:
            self.customer_email = args.get("email")
            for o in result["orders"]:
                self.orders.setdefault(o["order_id"], {}).update(o)

        elif tool in ("get_order", "track_package") and "error" not in result:
            oid = args.get("order_id")
            if oid:
                self.orders.setdefault(oid, {}).update(
                    {k: v for k, v in result.items() if k != "events"})

        if "error" in result:
            # A retryable error is the agent's own slip, not a decision about
            # the customer. Logging it would poison working memory with a
            # refusal that never happened.
            if not result.get("retry") and not result.get("temporarily_unavailable"):
                self.failures.append(f"{tool}({args.get('order_id', '')}) "
                                     f"refused: {result['error']}")
            return

        if tool == "cancel_order":
            self.actions.append(f"Cancelled {result['order_id']}, "
                                f"${result['refund_amount']} refunded")
            self.orders.setdefault(result["order_id"], {})["status"] = "cancelled"
        elif tool == "start_return":
            self.actions.append(f"Return started for {result['order_id']}, "
                                f"{result['rma']}, ${result['refund_amount']}")
            self.orders.setdefault(result["order_id"], {})["status"] = "return started"
        elif tool == "escalate":
            self.escalation = result["ticket"]
            self.actions.append(f"Escalated to a human, ticket {result['ticket']}")

    def update_focus_from_reply(self, reply):
        """Remember one verified order that Ami unambiguously offers to act on.

        A generated reply can list several orders and then offer to track or
        return one of them. The transcript preserves the words, but an async
        worker or a later model call should not have to infer that selection
        again. Only orders already returned through the authorized tool layer
        are candidates, and a segment matching more than one order is ignored.
        """
        if not reply or not self.orders:
            return
        action = re.compile(
            r"\b(track|tracking|cancel|cancellation|return|refund|help with|"
            r"check|status|proceed)\b", re.I,
        )
        segments = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", reply)]
        for segment in reversed(segments):
            if not action.search(segment):
                continue
            matches = self._orders_mentioned_in(segment)
            if len(matches) == 1:
                self.active_order_id = matches[0]
                return
            if len(matches) > 1:
                # A clarification must replace stale focus. Otherwise a prior
                # active order can silently bias a later ambiguous request.
                self.active_order_id = None
                return

    def _orders_mentioned_in(self, text):
        normalized = re.findall(r"[a-z0-9]+", text.casefold())
        words = set(normalized)
        item_tokens = {
            order_id: re.findall(r"[a-z0-9]+", str(order.get("item", "")).casefold())
            for order_id, order in self.orders.items()
        }
        token_owners = {}
        for order_id, tokens in item_tokens.items():
            for token in set(tokens):
                token_owners.setdefault(token, set()).add(order_id)

        scores = {}
        for order_id, tokens in item_tokens.items():
            if order_id.casefold() in text.casefold():
                scores[order_id] = 100
                continue
            overlap = words.intersection(tokens)
            shared_words = {
                token for token in overlap if not token.isdigit() and len(token) >= 3
            }
            distinctive = {
                token for token in overlap
                if len(token_owners.get(token, ())) == 1
                and (len(token) >= 4 or (token.isdigit() and shared_words))
            }
            if len(overlap) >= 2 or distinctive:
                scores[order_id] = len(overlap) + (2 * len(distinctive))
        if not scores:
            return []
        best = max(scores.values())
        return [order_id for order_id, score in scores.items() if score == best]

    # -- reading ----------------------------------------------------------

    def brief(self):
        """Working memory as a short note the model reads before every step."""
        if not any([self.customer_email, self.orders, self.active_order_id,
                    self.actions, self.failures, self.escalation, self.pending]):
            return None

        lines = ["WHAT YOU ALREADY KNOW (do not look these up again):"]
        if self.customer_email:
            lines.append(f"- Customer email: {self.customer_email}")
        for oid, o in self.orders.items():
            bits = [f"{oid}: {o.get('item', 'unknown item')}",
                    f"status={o.get('status', '?')}"]
            if o.get("eta"):
                bits.append(f"eta={o['eta']}")
            if o.get("delivered_on"):
                bits.append(f"delivered={o['delivered_on']}")
            lines.append("- " + ", ".join(bits))
        if self.active_order_id:
            active = self.orders.get(self.active_order_id, {})
            lines.append(
                f"CURRENT ORDER CONTEXT: {self.active_order_id}"
                f" ({active.get('item', 'verified order')}). Resolve pronouns or "
                "acceptance of Ami's latest offer to this order. If the customer "
                "explicitly describes multiple matching orders, ask which one."
            )
        if self.actions:
            lines.append("ALREADY DONE (never do these twice):")
            lines += [f"- {a}" for a in self.actions]
        if self.failures:
            lines.append("ALREADY REFUSED (do not retry):")
            lines += [f"- {f}" for f in self.failures]
        if self.escalation:
            lines.append(f"NOTE: this conversation is already escalated as "
                         f"{self.escalation}. Refer to that ticket rather than "
                         f"escalating again.")
        if self.pending:
            action, order_id = self.pending.get("key", ["an action", ""])
            lines.append(
                f"AWAITING CUSTOMER CONFIRMATION: {action} for order {order_id}. "
                "Do not preview it again. The server will consume an unambiguous "
                "yes; otherwise ask one concise clarification."
            )
        return "\n".join(lines)

    # -- persistence ------------------------------------------------------

    def to_dict(self):
        return {"customer_email": self.customer_email, "orders": self.orders,
                "active_order_id": self.active_order_id,
                "actions": self.actions, "failures": self.failures,
                "escalation": self.escalation, "turn": self.turn,
                "pending": self.pending}

    @classmethod
    def from_dict(cls, data):
        w = cls()
        w.customer_email = data.get("customer_email")
        w.orders = data.get("orders", {})
        active_order_id = data.get("active_order_id")
        w.active_order_id = active_order_id if active_order_id in w.orders else None
        w.actions = data.get("actions", [])
        w.failures = data.get("failures", [])
        w.escalation = data.get("escalation")
        w.turn = data.get("turn", 0)
        w.pending = data.get("pending")
        return w

    def __repr__(self):
        return (f"<WorkingMemory orders={len(self.orders)} "
                f"actions={len(self.actions)} escalation={self.escalation}>")


class LongTermMemory:
    """What we know about a CUSTOMER, across every conversation they have had.

    Conversation memory dies with the chat. Working memory dies with the
    task. This one is keyed by the customer and lives in PostgreSQL, so when
    the same person comes back tomorrow the agent knows it has met them:
    how many times, what they asked about, whether they already used up an
    exception.

    Deliberately small. It stores FACTS DERIVED FROM TOOLS (working memory),
    never raw transcript — that is how you avoid a memory full of things a
    customer once said in anger.
    """

    def __init__(self, repository=None):
        self.repository = repository or get_repository()

    # -- writing ----------------------------------------------------------

    def remember(self, work, session_id="cli"):
        """Fold the current task's working memory into the customer's record."""
        self.repository.remember(work.to_dict(), session_id)

    # -- reading ----------------------------------------------------------

    def recall(self, email, current_session=None):
        """A short note for the model, or None for a customer we have not met
        in an EARLIER conversation than this one."""
        rec = self.repository.recall(email)
        if not rec:
            return None
        previous = [s for s in rec["sessions"] if s != current_session]
        if not previous:
            return None
        lines = [f"RETURNING CUSTOMER ({email}): {len(previous)} previous "
                 f"conversation(s), last on {rec['last_seen']}."]
        business_actions = [action for action in rec["actions"]
                            if not action.startswith("Escalated to a human")]
        if business_actions:
            lines.append("Previously done for them: " + "; ".join(business_actions[-3:]))
        # Handoffs are scoped to their support ticket/conversation. Reusing an
        # old ESC reference for a new issue would hide work from the queue.
        if rec["refusals"]:
            lines.append(f"Has hit a policy refusal {rec['refusals']} time(s) before; "
                         f"be clear about rules up front.")
        return "\n".join(lines)
