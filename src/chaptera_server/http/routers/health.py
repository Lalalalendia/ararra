"""Unauthenticated liveness endpoint; never returns private state."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}
