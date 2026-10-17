"""Centralized authentication, service lookup and HTTP exception mapping."""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from chaptera_translation_core.prompt_policy import GlossaryVersionConflict

from ..auth import InvalidTeamToken, authenticate, role_for
from ..library import BookLibrary, LibraryConflict, LibraryNotFound
from ..import_sessions import ImportSessions
from ..storage import QueueStore


def require_member(request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> str:
    try:
        return authenticate(x_api_key, request.app.state.settings)
    except InvalidTeamToken as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def require_admin(request: Request, actor: str = Depends(require_member)) -> str:
    """Reserved for system/member controls, never for ordinary book editing."""
    if role_for(actor, request.app.state.settings) != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required")
    return actor


def get_store(request: Request) -> QueueStore:
    return request.app.state.store


def get_import_sessions(request: Request) -> ImportSessions:
    return request.app.state.import_sessions


def get_library(request: Request) -> BookLibrary:
    return request.app.state.library


def map_library_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LibraryNotFound):
        return HTTPException(status_code=404, detail=str(exc.args[0]))
    if isinstance(exc, LibraryConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, GlossaryVersionConflict):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))
