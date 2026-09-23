"""Process-local application state shared by the FastAPI route modules."""

import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from support_chatbot import UI_DIR
from support_chatbot import agent_profile as profile
from support_chatbot import plan_execute, planner
from support_chatbot.config import settings
from support_chatbot.llm import MODEL
from support_chatbot.memory import ConversationMemory, LongTermMemory, WorkingMemory
from support_chatbot.persistence import get_repository
from support_chatbot.workflow import WorkflowCoordinator


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

    def __init__(self, repository=None, workflow=None):
        self.repository = repository or get_repository()
        self.workflow = workflow or WorkflowCoordinator(self.repository)
        self.longterm = LongTermMemory(self.repository)
        self.sessions = {}
        self.session_lock = threading.RLock()
        self.turn_locks = {}
        self.started_at = time.time()

    @staticmethod
    def new_session(user_id=None):
        return {
            "convo": ConversationMemory(SYSTEM),
            "work": WorkingMemory(),
            "user_id": user_id,
        }

    def get_session(self, cookie_sid=None, user_id=None):
        """Resolve a browser session and replace unknown stale identifiers."""
        with self.session_lock:
            restored_sid = None
            if not cookie_sid and user_id:
                restored_sid = self.repository.latest_session_id(user_id)
            candidate_sid = cookie_sid or restored_sid
            cached = self.sessions.get(candidate_sid)
            cache_owned = bool(cached and cached.get("user_id") == user_id)
            persisted = self.repository.load_session(candidate_sid) if candidate_sid and not cache_owned else None
            stale = bool(cookie_sid) and not cache_owned and persisted is None
            sid = candidate_sid
            created = False
            if not cache_owned:
                if persisted:
                    self.sessions[sid] = {
                        "convo": ConversationMemory.from_dict(
                            SYSTEM, {"history": persisted["history"]}
                        ),
                        "work": WorkingMemory.from_dict(persisted["work"]),
                        "user_id": user_id,
                    }
                    created = not bool(cookie_sid)
                else:
                    sid = uuid.uuid4().hex
                    self.sessions[sid] = self.new_session(user_id)
                    self.repository.create_session(
                        sid, self.sessions[sid]["work"].to_dict(), user_id
                    )
                    created = True
                self.turn_locks[sid] = threading.RLock()
            return SessionContext(sid, self.sessions[sid], stale, created)

    def get_turn_lock(self, sid):
        with self.session_lock:
            return self.turn_locks.setdefault(sid, threading.RLock())

    def reload_session(self, sid, user_id):
        persisted = self.repository.load_session(sid)
        if not persisted:
            return self.sessions.get(sid)
        with self.session_lock:
            self.sessions[sid] = {
                "convo": ConversationMemory.from_dict(
                    SYSTEM, {"history": persisted["history"]}
                ),
                "work": WorkingMemory.from_dict(persisted["work"]),
                "user_id": user_id,
            }
            return self.sessions[sid]

    def reset_session(self, sid):
        with self.session_lock:
            user_id = self.sessions.get(sid, {}).get("user_id")
            self.sessions[sid] = self.new_session(user_id)
            self.turn_locks.setdefault(sid, threading.RLock())
            self.repository.reset_session(sid, self.sessions[sid]["work"].to_dict())
            return self.sessions[sid]

    def save_sessions(self, sid=None):
        with self.session_lock:
            snapshot = (
                [(sid, self.sessions[sid])]
                if sid and sid in self.sessions
                else list(self.sessions.items())
            )
        for sid, session in snapshot:
            self.repository.save_session(
                sid, session["convo"].history, session["work"].to_dict()
            )

    def save_feedback(self, golden_row_id, original_response, corrected_response, reason):
        self.repository.save_feedback({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "golden_row_id": golden_row_id,
            "original_response": original_response,
            "corrected_response": corrected_response,
            "reason": reason,
        })

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

    def close(self):
        self.workflow.close()
        close = getattr(self.repository, "close", None)
        if close:
            close()
