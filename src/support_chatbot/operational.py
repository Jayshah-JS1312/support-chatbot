"""Safe, low-cardinality operational views for administrators."""

import json
import re
from datetime import datetime, timezone


SENSITIVE_KEYS = {
    "args", "arguments", "body", "content", "customer_email", "customer_name",
    "email", "headers", "input", "message", "password", "prompt", "question",
    "request_payload", "response", "result_payload", "summary", "token",
}
SAFE_AUDIT_KEYS = {
    "action", "assigned_to", "customer_notified", "deadlines_seconds",
    "draft_id", "draft_version", "response_edited", "routing_reason",
}
_SECRET = re.compile(
    r"(?i)(bearer\s+[a-z0-9._-]+|sk-[a-z0-9_-]{12,}|"
    r"(?:api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+)"
)
_EMAIL = re.compile(r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


def redact_text(value):
    text = str(value or "")
    text = _SECRET.sub("[secret redacted]", text)
    text = _EMAIL.sub("[email redacted]", text)
    text = _CARD.sub("[payment data redacted]", text)
    return text[:500]


def public_event(event):
    """Allowlist safe telemetry fields; never expose tool/model payloads."""
    allowed = {
        "seq", "ts", "kind", "session", "turn", "model", "served_by",
        "finish", "tool_calls", "tokens", "tokens_in", "tokens_out", "cached",
        "cost", "ms", "tool", "ok", "rule", "stage", "suite", "recall_at_1",
        "recall_at_3", "steps", "hits", "category", "where",
    }
    result = {key: value for key, value in event.items() if key in allowed}
    if event.get("error"):
        result["error"] = redact_text(type(event["error"]).__name__
                                      if not isinstance(event["error"], str)
                                      else event["error"])
    if "hits" in result:
        result["hits"] = [
            {"heading": redact_text(hit.get("heading")), "category": hit.get("category")}
            for hit in (result["hits"] or [])[:5] if isinstance(hit, dict)
        ]
    return result


def public_events(events):
    return [public_event(event) for event in events]


def sanitized_jsonl(events):
    return "".join(json.dumps(public_event(event), separators=(",", ":")) + "\n"
                   for event in events)


def sanitized_logfile(path):
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return sanitized_jsonl(events)


def safe_audit_detail(detail):
    detail = detail if isinstance(detail, dict) else {}
    return {key: value for key, value in detail.items() if key in SAFE_AUDIT_KEYS}


def safe_error_code(value):
    candidate = str(value or "").split(":", 1)[0]
    return candidate if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", candidate) else "workflow_error"


def iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def age_seconds(value, now=None):
    if not value:
        return None
    now = now or datetime.now(timezone.utc)
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return max(0, round((now - moment).total_seconds()))


STATE_EXPLANATIONS = {
    "RECEIVED": "Stored durably; queue publication has not completed.",
    "QUEUED": "Published and waiting for a drafting worker.",
    "DRAFTING": "A worker is producing and validating a resolution.",
    "AWAITING_APPROVAL": "Drafted and paused for an authorized reviewer.",
    "APPROVED": "Approved and waiting to resume execution.",
    "EXECUTING": "The approved action is being revalidated and executed.",
    "COMPLETED": "Resolution delivery or approved execution completed.",
    "REJECTED": "A reviewer rejected the proposal; no action executed.",
    "EXPIRED": "The review deadline passed; no action executed.",
    "COMPLETED_WITHOUT_ACTION": "Work ended fail-closed without a privileged action.",
}


def explain_state(status):
    return STATE_EXPLANATIONS.get(status, "Unknown workflow state; investigate the audit trail.")
