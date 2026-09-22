"""Process-local application state shared by the FastAPI route modules."""

import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from support_chatbot import STATE_DIR, UI_DIR
from support_chatbot import agent_profile as profile
from support_chatbot import plan_execute, planner
from support_chatbot.config import settings
from support_chatbot.llm import MODEL
from support_chatbot.memory import ConversationMemory, LongTermMemory, WorkingMemory


SYSTEM = profile.system_prompt()
PLANNERS = {
    "react": (planner.react, planner.PLANNING_RULES),
    "plan": (plan_execute.plan_execute, plan_execute.PLANNING_RULES),
}


def _render_chat_page():
    page = (UI_DIR / "chat.html").read_text()
    return (page.replace("__MODEL__", MODEL)
                .replace("__GREETING__", json.dumps(profile.GREETING))
                .replace("__INTERNAL_ENABLED__", json.dumps(settings.expose_internal_ui)))


CHAT_PAGE = _render_chat_page()


@dataclass(frozen=True)
class SessionContext:
    sid: str
    session: dict
    stale: bool
    created: bool


class RuntimeState:
    """Own sessions, persistence paths, and locks for one application process."""

    def __init__(self, state_dir=None):
        root = Path(state_dir) if state_dir else STATE_DIR
        self.sessions_file = root / "sessions.json"
        self.feedback_file = root / "feedback.jsonl"
        self.longterm = LongTermMemory(root / "customers.json")
        self.sessions = {}
        self.session_lock = threading.RLock()
        self.feedback_lock = threading.Lock()
        self.turn_locks = {}
        self.started_at = time.time()
        self.load_sessions()

    @staticmethod
    def new_session():
        return {"convo": ConversationMemory(SYSTEM), "work": WorkingMemory()}

    def get_session(self, cookie_sid=None):
        """Resolve a browser session and replace unknown stale identifiers."""
        with self.session_lock:
            stale = bool(cookie_sid) and cookie_sid not in self.sessions
            sid = cookie_sid
            created = False
            if sid not in self.sessions:
                sid = uuid.uuid4().hex
                self.sessions[sid] = self.new_session()
                self.turn_locks[sid] = threading.RLock()
                created = True
            return SessionContext(sid, self.sessions[sid], stale, created)

    def get_turn_lock(self, sid):
        with self.session_lock:
            return self.turn_locks.setdefault(sid, threading.RLock())

    def reset_session(self, sid):
        with self.session_lock:
            self.sessions[sid] = self.new_session()
            self.turn_locks.setdefault(sid, threading.RLock())
            return self.sessions[sid]

    def save_sessions(self):
        try:
            self.sessions_file.parent.mkdir(parents=True, exist_ok=True)
            with self.session_lock:
                payload = json.dumps({
                    sid: {
                        "convo": session["convo"].to_dict(),
                        "work": session["work"].to_dict(),
                    }
                    for sid, session in self.sessions.items()
                })
                temporary = self.sessions_file.with_suffix(".tmp")
                temporary.write_text(payload)
                temporary.replace(self.sessions_file)
        except OSError:
            pass

    def load_sessions(self):
        try:
            raw = json.loads(self.sessions_file.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        restored = 0
        with self.session_lock:
            for sid, data in raw.items():
                try:
                    convo = ConversationMemory.from_dict(SYSTEM, data["convo"])
                    work = WorkingMemory.from_dict(data["work"])
                except (KeyError, TypeError, AttributeError):
                    continue
                self.sessions[sid] = {"convo": convo, "work": work}
                self.turn_locks[sid] = threading.RLock()
                restored += 1
        if restored:
            print(
                f"restored {restored} session(s) from {self.sessions_file.name}",
                flush=True,
            )

    def save_feedback(self, golden_row_id, original_response, corrected_response, reason):
        try:
            self.feedback_file.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "golden_row_id": golden_row_id,
                "original_response": original_response,
                "corrected_response": corrected_response,
                "reason": reason,
            }
            with self.feedback_lock, self.feedback_file.open("a") as file:
                file.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    def public_state(self, session):
        work = session["work"]
        result = {
            "messages": len(session["convo"]),
            "transcript": session["convo"].public_transcript(),
            "orders": len(work.orders),
            "actions": work.actions,
            "escalation": work.escalation,
        }
        if settings.expose_internal_ui:
            result.update({
                "working": work.brief() or "(empty — nothing established yet)",
                "longterm": self.longterm.recall(
                    work.customer_email, getattr(work, "session_id", None)
                ),
            })
        return result
