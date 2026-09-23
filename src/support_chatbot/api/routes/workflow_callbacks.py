"""Operator recovery controls; Upstash's signed endpoint is registered separately."""

from fastapi import APIRouter, Depends, Query, Request

from support_chatbot.api.dependencies import require_admin, runtime


router = APIRouter(prefix="/workflow", tags=["workflow-callbacks"], dependencies=[Depends(require_admin)])


@router.post("/recover")
def recover_workflows(request: Request, limit: int = Query(100, ge=1, le=500)):
    return runtime(request).workflow.recover(limit)


@router.post("/expire")
def expire_workflows(request: Request):
    expired = runtime(request).repository.expire_approvals()
    return {"expired": len(expired), "request_ids": expired}
