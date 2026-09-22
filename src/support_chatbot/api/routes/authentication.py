"""Authentication route boundary.

Phase 1 deliberately adds no authentication behavior. Future sign-in, sign-out,
and session-identity routes belong on this router so they cannot be mixed into
customer chat handlers.
"""

from fastapi import APIRouter


router = APIRouter(prefix="/auth", tags=["authentication"])
