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

    return {
        "turns": len(turns),
        "llm_calls": len(llm),
        "tool_calls": len(calls),
        "tool_errors": len(errors),
        "error_rate": round(100 * len(errors) / len(calls)) if calls else 0,
        "tokens": sum(e.get("tokens") or 0 for e in llm),
        "tokens_in": sum(e.get("tokens_in") or 0 for e in llm),
        "tokens_out": sum(e.get("tokens_out") or 0 for e in llm),
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
    }
