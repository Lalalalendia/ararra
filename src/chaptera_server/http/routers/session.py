"""Closed-team browser login/logout. API key callers remain backward compatible."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ...auth import InvalidTeamToken, authenticate, role_for
from ...team_sessions import COOKIE_NAME, CSRF_HEADER, TeamSessions
from ...team_login_throttle import BrowserLoginThrottled

router = APIRouter()
_PRIVATE = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
}


class BrowserLogin(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


def _current_session(request: Request) -> tuple[str, str]:
    token = request.cookies.get(COOKIE_NAME, "")
    member = request.app.state.team_sessions.resolve(
        token, credentials=request.app.state.team_credentials,
        settings=request.app.state.settings,
    )
    if member is None:
        raise HTTPException(status_code=401, detail="Действующая сессия необходима")
    return member, token


def _payload(request: Request, member: str, token: str) -> dict[str, str]:
    return {
        "member_id": member,
        "role": role_for(
            member, request.app.state.settings, request.app.state.team_credentials
        ),
        "csrf_token": TeamSessions.csrf_token(token),
    }


@router.post("/v1/session", status_code=201)
def login(body: BrowserLogin, request: Request) -> Response:
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site login forbidden")
    peer = request.client.host if request.client is not None else None
    throttle = request.app.state.team_login_throttle
    try:
        throttle.begin(peer)
    except BrowserLoginThrottled as exc:
        raise HTTPException(
            status_code=429, detail="Слишком много попыток входа",
            headers={**_PRIVATE, "Retry-After": str(exc.retry_after)},
        ) from exc
    try:
        member = authenticate(
            body.api_key, request.app.state.settings, request.app.state.team_credentials
        )
    except InvalidTeamToken as exc:
        throttle.failure(peer)
        raise HTTPException(
            status_code=401, detail="Неверный ключ участника", headers=_PRIVATE
        ) from exc
    throttle.success(peer)
    settings = request.app.state.settings
    token = request.app.state.team_sessions.issue(
        member_id=member, team_key=body.api_key,
        ttl_seconds=settings.browser_session_ttl_seconds,
    )
    response = JSONResponse(_payload(request, member, token), status_code=201, headers=_PRIVATE)
    response.set_cookie(
        COOKIE_NAME, token, max_age=settings.browser_session_ttl_seconds,
        httponly=True, secure=settings.browser_session_secure, samesite="strict", path="/",
    )
    return response


@router.get("/v1/session")
def get_session(request: Request) -> Response:
    member, token = _current_session(request)
    return JSONResponse(_payload(request, member, token), headers=_PRIVATE)


@router.post("/v1/session/logout", status_code=204)
def logout(request: Request) -> Response:
    token = request.cookies.get(COOKIE_NAME, "")
    if token and request.app.state.team_sessions.resolve(
        token, credentials=request.app.state.team_credentials,
        settings=request.app.state.settings,
    ) is not None:
        provided = request.headers.get(CSRF_HEADER, "")
        if not provided or not hmac.compare_digest(
            provided, TeamSessions.csrf_token(token)
        ):
            raise HTTPException(status_code=403, detail="CSRF proof required")
    request.app.state.team_sessions.revoke(token)
    response = Response(status_code=204, headers=_PRIVATE)
    response.delete_cookie(
        COOKIE_NAME, path="/", secure=request.app.state.settings.browser_session_secure,
        httponly=True, samesite="strict",
    )
    return response
