"""Authenticated, read-only local Gemini request allowance status.

Reports only this worker's configured project+model SQLite ledger; the
Google project identifier, API keys, source texts, tokens and quota secrets
must never leave the server.
"""
from fastapi import APIRouter, Depends, Request

from ...quota import read_daily_quota_status
from ...storage import QueueStore
from ..dependencies import get_store, require_member

router = APIRouter(prefix="/v1/quota")


@router.get("/gemini")
def gemini_quota_status(request: Request,
                        actor: str = Depends(require_member),
                        store: QueueStore = Depends(get_store)):
    settings = request.app.state.settings
    if not settings.gemini_requests_per_day or not settings.gemini_quota_project:
        return {"enabled": False, "model": settings.gemini_model}
    return {
        "enabled": True,
        **read_daily_quota_status(
            store, settings.gemini_quota_project,
            settings.gemini_model, settings.gemini_requests_per_day,
        ),
    }
