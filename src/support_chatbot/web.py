"""HTTP application for the support chatbot.

Run with ``support-chatbot-web`` after installing the project.

One session per browser, kept in a cookie and saved to state/sessions.json,
so a reload or a restart does not lose the conversation.

The browser can switch between ReAct and plan-and-execute. Every turn passes
through the policy layer and can use long-term customer memory.

The page markup lives in ui/chat.html.
"""

import json
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

from support_chatbot import agent_profile as profile
from support_chatbot import dashboard
from support_chatbot import evals
from support_chatbot import observe
from support_chatbot import plan_execute
from support_chatbot import planner
from support_chatbot import policy
from support_chatbot import STATE_DIR, UI_DIR
from support_chatbot.config import settings
from support_chatbot.llm import MODEL
from support_chatbot.memory import ConversationMemory, LongTermMemory, WorkingMemory
SYSTEM = profile.system_prompt()          # planner rules are added per request

# The two planners are interchangeable: same inputs, same outputs. The page
# has a switch so you can run the same conversation through each.
PLANNERS = {
    "react": (planner.react, planner.PLANNING_RULES),
    "plan":  (plan_execute.plan_execute, plan_execute.PLANNING_RULES),
}

LONGTERM = LongTermMemory()               # shared: it is keyed by customer, not session
FEEDBACK_FILE = STATE_DIR / "feedback.jsonl"
SESSION_LOCK = threading.RLock()
FEEDBACK_LOCK = threading.Lock()
STARTED_AT = time.time()

# One session PER BROWSER, keyed by a cookie. The earlier version kept a
# single global conversation, which meant two people on the same server —
# or the same person after a reload — silently shared one customer's
# orders, cancellations and escalations. Memory has to be scoped to whose
# memory it is.
#
# And it has to SURVIVE. An in-memory dict means every restart silently
# wipes every conversation while the customer's browser still shows the
# transcript — the agent then truthfully says it remembers nothing. So
# sessions are written to disk after each turn and reloaded on boot.
SESSIONS = {}
SESSIONS_FILE = STATE_DIR / "sessions.json"


def new_session():
    return {"convo": ConversationMemory(SYSTEM), "work": WorkingMemory()}


def save_sessions():
    try:
        SESSIONS_FILE.parent.mkdir(exist_ok=True)
        with SESSION_LOCK:
            payload = json.dumps({
                sid: {"convo": s["convo"].to_dict(), "work": s["work"].to_dict()}
                for sid, s in SESSIONS.items()
            })
            temporary = SESSIONS_FILE.with_suffix(".tmp")
            temporary.write_text(payload)
            temporary.replace(SESSIONS_FILE)
    except OSError:
        pass                      # never let persistence break a reply


def load_sessions():
    try:
        raw = json.loads(SESSIONS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return
    with SESSION_LOCK:
        for sid, d in raw.items():
            SESSIONS[sid] = {
                "convo": ConversationMemory.from_dict(SYSTEM, d["convo"]),
                "work": WorkingMemory.from_dict(d["work"]),
            }
    print(f"restored {len(SESSIONS)} session(s) from {SESSIONS_FILE.name}", flush=True)


load_sessions()


def save_feedback(golden_row_id, original_response, corrected_response, reason):
    """Log feedback entry to state/feedback.jsonl."""
    try:
        FEEDBACK_FILE.parent.mkdir(exist_ok=True)
        feedback_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "golden_row_id": golden_row_id,
            "original_response": original_response,
            "corrected_response": corrected_response,
            "reason": reason
        }
        with FEEDBACK_LOCK, FEEDBACK_FILE.open("a") as f:
            f.write(json.dumps(feedback_entry) + "\n")
    except OSError:
        pass                      # never let persistence break a reply


def state(session):
    """Everything the page needs to redraw its panels."""
    work = session["work"]
    result = {
        "messages": len(session["convo"]),
        # Only restore customer-visible dialogue. Tool calls, observations,
        # and planner messages stay in the operator view.
        "transcript": session["convo"].public_transcript(),
        "orders": len(work.orders),
        # Customer-facing: what the agent actually DID on their account.
        "actions": work.actions,
        "escalation": work.escalation,
    }
    if settings.expose_internal_ui:
        result.update({
            "working": work.brief() or "(empty — nothing established yet)",
            "longterm": LONGTERM.recall(
                work.customer_email, getattr(work, "session_id", None)
            ),
        })
    return result


class Handler(BaseHTTPRequestHandler):

    def _session(self):
        """Find this browser's session, minting one (and a cookie) if new."""
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookie["sid"].value if "sid" in cookie else None
        # A cookie we do not recognise means the session is GONE, not new.
        # Say so, rather than quietly handing back an empty conversation.
        self.stale = bool(sid) and sid not in SESSIONS
        with SESSION_LOCK:
            if sid not in SESSIONS:
                sid = uuid.uuid4().hex
                SESSIONS[sid] = new_session()
                self._set_cookie = sid
            return sid, SESSIONS[sid]

    def _send(self, body, content_type="application/json", status=200):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if getattr(self, "_set_cookie", None):
            flags = "; Secure" if settings.secure_cookies else ""
            self.send_header(
                "Set-Cookie",
                f"sid={self._set_cookie}; Path=/; HttpOnly; SameSite=Lax{flags}",
            )
            self._set_cookie = None
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/healthz":
            self._send(json.dumps({"status": "ok"}))
        elif self.path == "/readyz":
            self._send(json.dumps({"status": "ready", "model": MODEL}))
        elif self.path == "/metrics":
            self._send(observe.prometheus_metrics(), "text/plain; version=0.0.4")
        elif self.path in ("/", "/index.html"):
            self._session()                      # mint the cookie on first load
            self._send(PAGE, "text/html")
        elif self.path in ("/monitoring", "/logs") and settings.expose_internal_ui:
            self._send(dashboard.PAGE, "text/html")
        elif self.path == "/evals" and settings.expose_internal_ui:
            self._send(dashboard.EVAL_PAGE, "text/html")
        elif self.path == "/evals.json" and settings.expose_internal_ui:
            self._send(json.dumps(evals.latest()))
        elif self.path == "/logs.json" and settings.expose_internal_ui:
            self._send(json.dumps({
                "stats": observe.stats(),
                "events": observe.recent(120),
                "runtime": {
                    "status": "operational",
                    "uptime_seconds": round(time.time() - STARTED_AT),
                    "sessions": len(SESSIONS),
                    "model": MODEL,
                },
            }))
        elif self.path == "/trace.jsonl" and settings.expose_internal_ui:
            try:
                self._send(observe.LOGFILE.read_text(), "text/plain")
            except OSError:
                self._send("", "text/plain")
        elif self.path == "/state":
            _, session = self._session()
            self._send(json.dumps({**state(session), "stale": self.stale}))
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > settings.max_request_bytes:
            self._send(json.dumps({"error": "request too large"}), status=413)
            return
        try:
            data = json.loads(self.rfile.read(length) or "{}")
        except json.JSONDecodeError:
            self._send(json.dumps({"error": "invalid JSON"}), status=400)
            return

        if self.path == "/evals/run" and settings.expose_internal_ui:
            self._send(json.dumps(evals.run()))
            return

        sid, session = self._session()

        if self.path == "/reset":
            SESSIONS[sid] = new_session()
            save_sessions()
            self._send(json.dumps({"ok": True, **state(SESSIONS[sid])}))
            return

        if self.path == "/feedback":
            golden_row_id = data.get("golden_row_id") or None
            original_response = data.get("original_response", "").strip()
            corrected_response = data.get("corrected_response", "").strip()
            reason = data.get("reason", "").strip()

            if original_response and corrected_response:
                save_feedback(golden_row_id, original_response, corrected_response, reason)
                self._send(json.dumps({"ok": True, "message": "Feedback saved"}))
            else:
                self._send(json.dumps({"ok": False, "message": "Missing required fields"}))
            return

        if self.path != "/chat":
            self.send_error(404)
            return

        text = (data.get("message") or "").strip()
        if not text:
            self._send(json.dumps({"reply": "", "steps": [], **state(session)}))
            return

        convo, work = session["convo"], session["work"]
        before = len(work.actions)          # so we can tell the customer what changed
        run, rules = PLANNERS.get(data.get("planner"), PLANNERS["react"])
        convo.system = SYSTEM + rules

        # Tag this thread so every model call and tool call underneath is
        # attributed to this customer and this turn.
        observe.context(session=sid)
        turn_id = observe.new_turn()

        # policy: input — before the text touches memory
        text, note = policy.check_input(text)
        work.turn += 1
        work.session_id = sid            # so long-term recall excludes THIS chat
        convo.add_user(text)

        steps = []
        turn_failed = False
        with observe.timer() as t:
            try:
                reply = run(convo, work, trace=False, steps=steps,
                            longterm=LONGTERM, extra=note)
            except Exception as e:                  # keep the page alive
                turn_failed = True
                observe.log("error", where="chat", error=type(e).__name__)
                reply = "Something went wrong while processing your request. Please try again."

        # policy: output — before the customer sees it
        reply = policy.check_output(
            reply, work, text,
            context=LONGTERM.recall(work.customer_email, sid) or "")

        # long-term memory: if a tool has identified the customer, remember them
        LONGTERM.remember(work, session_id=sid)

        observe.log("turn", user=text, steps=len(steps), ms=t.ms,
                    actions=len(work.actions) - before,
                    planner=data.get("planner", "react"),
                    cost=observe.turn_cost(turn_id), error=turn_failed)
        save_sessions()                  # survive a restart

        public_steps = steps if settings.expose_internal_ui else []
        self._send(json.dumps({"reply": reply, "steps": public_steps,
                               "new_actions": work.actions[before:],
                               **state(session)}))

    def log_message(self, *args):
        pass                                        # quiet the request spam


PAGE = (UI_DIR / "chat.html").read_text()

PAGE = (PAGE.replace("__MODEL__", MODEL)
            .replace("__GREETING__", json.dumps(profile.GREETING))
            .replace("__INTERNAL_ENABLED__", json.dumps(settings.expose_internal_ui)))


def main():
    """Run the built-in HTTP server."""
    print(
        f"Ami is running at http://{settings.host}:{settings.port} (ctrl-c to stop)",
        flush=True,
    )
    ThreadingHTTPServer((settings.host, settings.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
