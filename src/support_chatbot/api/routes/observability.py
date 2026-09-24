"""Health, metrics, and deterministic retrieval-evaluation routes."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from support_chatbot import dashboard, evals, hitl_evals, observe
from support_chatbot.api.dependencies import require_admin, require_internal_ui, runtime
from support_chatbot.api.models import ResetRequest
from support_chatbot.llm import MODEL


router = APIRouter(tags=["observability"])
evaluation_router = APIRouter(
    tags=["evaluations"],
    dependencies=[Depends(require_internal_ui)],
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
def metrics(request: Request, _admin=Depends(require_admin)):
    return PlainTextResponse(
        observe.prometheus_metrics(runtime(request).repository.operational_metrics()),
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


@evaluation_router.get("/admin/hitl-evals", response_class=HTMLResponse)
def hitl_evaluation_page():
    return HTMLResponse(dashboard.HITL_EVAL_PAGE)


@evaluation_router.get("/admin/hitl-evals.json")
def latest_hitl_evaluation(request: Request):
    repository = runtime(request).repository
    report = repository.latest_hitl_evaluation()
    return {
        "report": report or {"status": "not_run", "summary": None, "results": []},
        "operations": repository.hitl_operational_metrics(),
    }


@evaluation_router.post("/admin/hitl-evals/run")
def run_hitl_evaluation(request: Request, payload: ResetRequest | None = None):
    del payload
    repository = runtime(request).repository
    report = repository.save_hitl_evaluation(hitl_evals.run())
    return {"report": report, "operations": repository.hitl_operational_metrics()}
