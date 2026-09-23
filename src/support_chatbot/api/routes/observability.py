"""Health, metrics, and deterministic retrieval-evaluation routes."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from support_chatbot import dashboard, evals, observe
from support_chatbot.api.dependencies import require_admin, runtime
from support_chatbot.api.models import ResetRequest
from support_chatbot.llm import MODEL


router = APIRouter(tags=["observability"])
evaluation_router = APIRouter(
    tags=["evaluations"],
    dependencies=[Depends(require_admin)],
)


@router.get("/healthz")
def health():
    return {"status": "ok"}


@router.get("/readyz")
def readiness(request: Request):
    try:
        healthy = runtime(request).repository.healthcheck()
    except Exception as error:
        raise HTTPException(status_code=503, detail="Database is unavailable") from error
    if not healthy:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return {"status": "ready", "model": MODEL}


@router.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return PlainTextResponse(
        observe.prometheus_metrics(),
        media_type="text/plain; version=0.0.4",
    )


@evaluation_router.get("/evals", response_class=HTMLResponse)
def evaluation_page():
    return HTMLResponse(dashboard.EVAL_PAGE)


@evaluation_router.get("/evals.json")
def latest_evaluation():
    return evals.latest()


@evaluation_router.post("/evals/run")
def run_evaluation(payload: ResetRequest | None = None):
    del payload
    return evals.run()
