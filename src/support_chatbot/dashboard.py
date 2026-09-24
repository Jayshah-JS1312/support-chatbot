"""The observability page: /monitoring (with /logs kept as an alias).

Three sections, in the order you actually debug in:

  1. a KPI row  — is anything wrong right now?
  2. tool usage — which tools get reached for, and which ones refuse
  3. the event log — the individual requests, newest first

Colors follow one rule: the bars are ONE hue (magnitude), and refusals wear
the reserved status red, never a third series colour. Every bar is labelled,
so the colour is never the only thing carrying the meaning.

The page markup lives in ui/logs.html.
"""

from . import UI_DIR

PAGE = (UI_DIR / "logs.html").read_text()
EVAL_PAGE = (UI_DIR / "evals.html").read_text()
APPROVAL_PAGE = (UI_DIR / "approvals.html").read_text()
ADMIN_TICKETS_PAGE = (UI_DIR / "admin_tickets.html").read_text()
HITL_EVAL_PAGE = (UI_DIR / "hitl_evals.html").read_text()
