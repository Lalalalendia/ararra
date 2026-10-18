"""Centralized authentication, service lookup and HTTP exception mapping."""
from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, Request

from chaptera_translation_core.prompt_policy import GlossaryVersionConflict

from ..auth import InvalidTeamToken, authenticate, role_for
from ..team_sessions import COOKIE_NAME, CSRF_HEADER, TeamSessions
from ..library import BookLibrary, LibraryConflict, LibraryNotFound
from ..editorial import EditorialStore
from ..chapter_qa import ChapterQaStore
from ..import_sessions import ImportSessions
from ..storage import QueueStore


def require_member(request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> str:
    # Explicit X-API-Key always wins; invalid headers cannot silently fall back
    # to a browser cookie. Existing programmatic callers keep their contract.
    if x_api_key is not None:
        try:
            return authenticate(x_api_key, request.app.state.settings, request.app.state.team_credentials)
        except InvalidTeamToken as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    token = request.cookies.get(COOKIE_NAME, "")
    details = request.app.state.team_sessions.resolve_details(
        token, credentials=request.app.state.team_credentials,
        settings=request.app.state.settings,
    )
    if details is None:
        raise HTTPException(status_code=401, detail="Valid team authentication required")
    actor, _kind = details
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        proof = request.headers.get(CSRF_HEADER, "")
        if not proof or not hmac.compare_digest(proof, TeamSessions.csrf_token(token)):
            raise HTTPException(status_code=403, detail="CSRF proof required")
    return actor


def require_admin(request: Request, actor: str = Depends(require_member)) -> str:
    """Reserved for system/member controls, never for ordinary book editing."""
    if role_for(actor, request.app.state.settings, request.app.state.team_credentials) != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required")
    return actor


def get_store(request: Request) -> QueueStore:
    return request.app.state.store


def get_import_sessions(request: Request) -> ImportSessions:
    return request.app.state.import_sessions


def get_editorial(request: Request) -> EditorialStore:
    return request.app.state.editorial


def get_chapter_qa(request: Request) -> ChapterQaStore:
    return request.app.state.chapter_qa


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
