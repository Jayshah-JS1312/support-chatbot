"""Operator-only monitoring, event, and trace routes."""

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from support_chatbot import dashboard, observe
from support_chatbot.api.dependencies import require_internal_ui, runtime
from support_chatbot.llm import MODEL


router = APIRouter(
    tags=["admin"],
    dependencies=[Depends(require_internal_ui)],
)


@router.get("/monitoring", response_class=HTMLResponse)
@router.get("/logs", response_class=HTMLResponse)
def monitoring_page():
    return HTMLResponse(dashboard.PAGE)


@router.get("/logs.json")
def monitoring_data(request: Request):
    app_runtime = runtime(request)
    return {
        "stats": observe.stats(),
        "events": observe.recent(120),
        "runtime": {
            "status": "operational",
            "uptime_seconds": round(time.time() - app_runtime.started_at),
            "sessions": len(app_runtime.sessions),
            "model": MODEL,
        },
    }


@router.get("/trace.jsonl", response_class=PlainTextResponse)
def trace_log():
    try:
        return PlainTextResponse(observe.LOGFILE.read_text())
    except OSError:
        return PlainTextResponse("")
