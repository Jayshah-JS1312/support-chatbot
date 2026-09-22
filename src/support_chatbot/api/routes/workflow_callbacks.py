"""Asynchronous workflow callback route boundary.

Callbacks are intentionally not accepted until the later durable workflow and
authorization phases define signed payloads, replay protection, and idempotency.
"""

from fastapi import APIRouter


router = APIRouter(prefix="/workflow", tags=["workflow-callbacks"])
