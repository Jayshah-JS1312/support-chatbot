"""Observability: a record of everything the agent did, and what it cost.

An agent is a loop you cannot see from the outside. When it gives a bad
answer, the question is always the same: which step went wrong, and why?
The ReAct trace answers that for ONE turn, in the browser, while you watch.
This module answers it for every turn, after the fact.

Three event kinds, deliberately few:

    llm   — one model call:  latency, tokens, whether it asked for tools
    tool  — one tool call:   arguments, ok/error, latency
    turn  — one user message: the whole round trip, start to reply

Events are held in memory for the dashboard and appended to state/trace.jsonl so
they survive a restart. One JSON object per line — grep it, or load it in
pandas, without a parser.
"""

import json
import threading
import time
import uuid
from collections import Counter

from support_chatbot import pricing
from support_chatbot import STATE_DIR

LOGFILE = STATE_DIR / "trace.jsonl"
MAX_EVENTS = 500              # what the dashboard keeps in memory

EVENTS = []
SEQ = 0                       # ever-increasing; survives the EVENTS cap
_lock = threading.Lock()

# Which session/turn the current thread is working on. The web server handles
# requests on separate threads, so this keeps two customers' events apart
# without passing ids through every function signature.
_ctx = threading.local()


def _replay():
    """Reload recent events from disk so a restart does not blank the dashboard.

    The file is the record; the list in memory is just the fast view of it.
    """
    try:
        lines = LOGFILE.read_text().splitlines()[-MAX_EVENTS:]
    except OSError:
        return
    global SEQ
    for line in lines:
        try:
            EVENTS.append(json.loads(line))
        except json.JSONDecodeError:
            pass                       # a half-written last line is not fatal
    # Continue numbering after the replayed events, or "since seq N" filters
    # would match old events as well as new ones.
    SEQ = max((e.get("seq", 0) for e in EVENTS), default=0)


_replay()


def context(session=None, turn=None):
    """Tag everything this thread logs from here on."""
    if session is not None:
        _ctx.session = session
    if turn is not None:
        _ctx.turn = turn


def new_turn():
    _ctx.turn = uuid.uuid4().hex[:8]
    return _ctx.turn


def log(kind, **fields):
    global SEQ
    with _lock:
        SEQ += 1
        event = {
            "seq": SEQ,
            "ts": time.time(),
            "kind": kind,
            "session": getattr(_ctx, "session", "cli")[:8],
            "turn": getattr(_ctx, "turn", "-"),
            **fields,
        }
        EVENTS.append(event)
        del EVENTS[:-MAX_EVENTS]
        try:
            LOGFILE.parent.mkdir(exist_ok=True)
            with LOGFILE.open("a") as f:
                f.write(json.dumps(event) + "\n")
        except OSError:
            pass                      # never let logging break the agent
    return event


class timer:
    """`with timer() as t:` ... then t.ms — how long the block took."""

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = round((time.perf_counter() - self.t0) * 1000)


def turn_cost(turn_id):
    """What one user message cost, summed over every model call it triggered."""
    with _lock:
        return sum(e.get("cost") or 0 for e in EVENTS
                   if e["turn"] == turn_id and e["kind"] == "llm")


def recent(limit=100, kind=None):
    with _lock:
        events = [e for e in EVENTS if kind is None or e["kind"] == kind]
    return events[-limit:][::-1]          # newest first


def _percentile(values, p):
    if not values:
        return 0
    ordered = sorted(values)
    i = min(int(round(p / 100 * len(ordered) + 0.5)) - 1, len(ordered) - 1)
    return ordered[i]


def stats():
    """The numbers worth putting on a dashboard."""
    with _lock:
        events = list(EVENTS)

    turns = [e for e in events if e["kind"] == "turn"]
    llm = [e for e in events if e["kind"] == "llm"]
    calls = [e for e in events if e["kind"] == "tool"]
    errors = [e for e in calls if not e["ok"]]
    runtime_errors = [e for e in events if e["kind"] == "error"
                      or (e["kind"] == "llm" and e.get("error"))]
    retries = [e for e in events if e["kind"] == "llm_retry"]
    capacity_rejections = [
        e for e in events if e["kind"] == "model_capacity" and e.get("outcome") == "rejected"
    ]
    successful_turns = [e for e in turns if not e.get("error")]

    return {
        "turns": len(turns),
        "llm_calls": len(llm),
        "tool_calls": len(calls),
        "tool_errors": len(errors),
        "runtime_errors": len(runtime_errors),
        "success_rate": round(100 * len(successful_turns) / len(turns), 1)
        if turns else 100.0,
        "error_rate": round(100 * len(errors) / len(calls)) if calls else 0,
        "tokens": sum(e.get("tokens") or 0 for e in llm),
        "tokens_in": sum(e.get("tokens_in") or 0 for e in llm),
        "tokens_out": sum(e.get("tokens_out") or 0 for e in llm),
        "cached_tokens": sum(e.get("cached") or 0 for e in llm),
        "model_retries": len(retries),
        "model_capacity_rejections": len(capacity_rejections),
        "cost": sum(e.get("cost") or 0 for e in llm),
        # What one customer conversation actually costs — the number that
        # matters when you multiply by a support queue.
        "cost_per_turn": (sum(e.get("cost") or 0 for e in llm) / len(turns)
                          if turns else 0),
        "turn_p50_ms": _percentile([e["ms"] for e in turns], 50),
        "turn_p95_ms": _percentile([e["ms"] for e in turns], 95),
        "llm_p50_ms": _percentile([e["ms"] for e in llm], 50),
        # how often each tool was reached for, and how often it refused
        "by_tool": [
            {"tool": name, "calls": n,
             "errors": sum(1 for e in errors if e["tool"] == name)}
            for name, n in Counter(e["tool"] for e in calls).most_common()
        ],
        "steps_per_turn": round(
            sum(e.get("steps", 0) for e in turns) / len(turns), 1) if turns else 0,
        "last_event_at": max((e["ts"] for e in events), default=None),
    }


def prometheus_metrics(operations=None):
    """Return aggregate, low-cardinality metrics in Prometheus text format."""
    snapshot = stats()
    lines = [
        "# HELP support_agent_turns_total Completed customer turns.",
        "# TYPE support_agent_turns_total counter",
        f"support_agent_turns_total {snapshot['turns']}",
        "# HELP support_agent_llm_calls_total Model calls made by the agent.",
        "# TYPE support_agent_llm_calls_total counter",
        f"support_agent_llm_calls_total {snapshot['llm_calls']}",
        "# HELP support_agent_tool_calls_total Tool calls made by the agent.",
        "# TYPE support_agent_tool_calls_total counter",
        f"support_agent_tool_calls_total {snapshot['tool_calls']}",
        "# HELP support_agent_tool_errors_total Tool calls rejected or failed.",
        "# TYPE support_agent_tool_errors_total counter",
        f"support_agent_tool_errors_total {snapshot['tool_errors']}",
        "# HELP support_agent_runtime_errors_total Runtime and model errors.",
        "# TYPE support_agent_runtime_errors_total counter",
        f"support_agent_runtime_errors_total {snapshot['runtime_errors']}",
        "# HELP support_agent_tokens_total Model tokens consumed.",
        "# TYPE support_agent_tokens_total counter",
        f"support_agent_tokens_total {snapshot['tokens']}",
        "# HELP support_agent_cached_prompt_tokens_total Prompt tokens served from provider cache.",
        "# TYPE support_agent_cached_prompt_tokens_total counter",
        f"support_agent_cached_prompt_tokens_total {snapshot['cached_tokens']}",
        "# HELP support_agent_model_retries_total Retried transient model calls.",
        "# TYPE support_agent_model_retries_total counter",
        f"support_agent_model_retries_total {snapshot['model_retries']}",
        "# HELP support_agent_model_capacity_rejections_total Calls rejected by local concurrency backpressure.",
        "# TYPE support_agent_model_capacity_rejections_total counter",
        f"support_agent_model_capacity_rejections_total {snapshot['model_capacity_rejections']}",
        "# HELP support_agent_cost_usd_total Estimated model spend in US dollars.",
        "# TYPE support_agent_cost_usd_total counter",
        f"support_agent_cost_usd_total {snapshot['cost']:.8f}",
        "# HELP support_agent_turn_latency_p95_ms Recent p95 turn latency.",
        "# TYPE support_agent_turn_latency_p95_ms gauge",
        f"support_agent_turn_latency_p95_ms {snapshot['turn_p95_ms']}",
    ]
    if operations:
        gauges = {
            "support_agent_queue_depth": operations.get("queue_depth"),
            "support_agent_pending_approvals": operations.get("pending_approvals"),
            "support_agent_oldest_pending_approval_seconds": operations.get("oldest_pending_approval_seconds"),
            "support_agent_workflow_retries_total": operations.get("retry_count"),
            "support_agent_dead_letters_total": operations.get("dead_letter_count"),
            "support_agent_expired_approvals_total": operations.get("expired_approvals"),
            "support_agent_hitl_recall_percent": operations.get("hitl_recall"),
            "support_agent_escalation_precision_percent": operations.get("escalation_precision"),
        }
        for name, value in gauges.items():
            if value is not None:
                lines.extend((f"# TYPE {name} gauge", f"{name} {value}"))
    return "\n".join(lines) + "\n"
