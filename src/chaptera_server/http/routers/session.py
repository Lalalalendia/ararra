"""Closed-team browser login/logout. API key callers remain backward compatible."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ...auth import InvalidTeamToken, authenticate, role_for
from ...team_sessions import COOKIE_NAME, CSRF_HEADER, TeamSessions
from ...team_login_throttle import BrowserLoginThrottled
from ...team_passwords import PasswordRejected, PasswordVersionConflict, PasswordBusy

router = APIRouter()
_PRIVATE = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
}


class BrowserLogin(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


class BrowserPasswordLogin(BaseModel):
    member_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class BrowserPasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=128)


def _current_session(request: Request) -> tuple[str, str, str]:
    token = request.cookies.get(COOKIE_NAME, "")
    details = request.app.state.team_sessions.resolve_details(
        token, credentials=request.app.state.team_credentials,
        passwords=request.app.state.team_passwords,
        settings=request.app.state.settings,
    )
    if details is None:
        raise HTTPException(status_code=401, detail="Действующая сессия необходима")
    return details[0], token, details[1]


def _payload(request: Request, member: str, token: str) -> dict:
    details = request.app.state.team_sessions.resolve_details(
        token, credentials=request.app.state.team_credentials,
        passwords=request.app.state.team_passwords,
        settings=request.app.state.settings,
    )
    if details is None:
        raise HTTPException(status_code=401, detail="Session expired")
    return {
        "member_id": member,
        "auth_kind": details[1],
        "must_change_password": (
            details[1] == "password" and request.app.state.team_passwords.requires_change(member)
        ),
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


@router.post("/v1/session/password", status_code=201)
def login_password(body: BrowserPasswordLogin, request: Request) -> Response:
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site login forbidden")
    peer = request.client.host if request.client is not None else None
    throttle = request.app.state.team_login_throttle
    try:
        throttle.begin(peer, member_id=body.member_id)
    except BrowserLoginThrottled as exc:
        raise HTTPException(
            status_code=429, detail="Слишком много попыток входа",
            headers={**_PRIVATE, "Retry-After": str(exc.retry_after)},
        ) from exc
    try:
        snapshot = request.app.state.team_passwords.verify_login(
            member_id=body.member_id, password=body.password,
            credentials=request.app.state.team_credentials,
            settings=request.app.state.settings,
        )
    except PasswordBusy as exc:
        raise HTTPException(status_code=503, detail="Сервер проверяет другие пароли",
                            headers={**_PRIVATE, "Retry-After": "1"}) from exc
    except PasswordRejected as exc:
        throttle.failure(peer, member_id=body.member_id)
        raise HTTPException(status_code=401, detail="Неверные данные входа",
                            headers=_PRIVATE) from exc
    throttle.success(peer, member_id=body.member_id)
    settings = request.app.state.settings
    token = request.app.state.team_sessions.issue_password(
        member_id=body.member_id, fingerprint=snapshot,
        ttl_seconds=settings.browser_session_ttl_seconds,
    )
    response = JSONResponse(_payload(request, body.member_id, token),
                            status_code=201, headers=_PRIVATE)
    response.set_cookie(
        COOKIE_NAME, token, max_age=settings.browser_session_ttl_seconds,
        httponly=True, secure=settings.browser_session_secure, samesite="strict", path="/",
    )
    return response


@router.get("/v1/session")
def get_session(request: Request) -> Response:
    member, token, _kind = _current_session(request)
    return JSONResponse(_payload(request, member, token), headers=_PRIVATE)


@router.post("/v1/session/password/change", status_code=204)
def change_password(body: BrowserPasswordChange, request: Request) -> Response:
    member, token, kind = _current_session(request)
    if kind != "password":
        raise HTTPException(status_code=403, detail="Password session required")
    proof = request.headers.get(CSRF_HEADER, "")
    if not proof or not hmac.compare_digest(proof, TeamSessions.csrf_token(token)):
        raise HTTPException(status_code=403, detail="CSRF proof required")
    peer = request.client.host if request.client is not None else None
    throttle = request.app.state.team_login_throttle
    try:
        throttle.begin(peer, member_id=member)
    except BrowserLoginThrottled as exc:
        raise HTTPException(
            status_code=429, detail="Слишком много попыток",
            headers={**_PRIVATE, "Retry-After": str(exc.retry_after)},
        ) from exc
    try:
        request.app.state.team_passwords.change(
            member_id=member, current_password=body.current_password,
            new_password=body.new_password,
        )
    except PasswordBusy as exc:
        raise HTTPException(status_code=503, detail="Сервер проверяет другие пароли",
                            headers={**_PRIVATE, "Retry-After": "1"}) from exc
    except PasswordRejected as exc:
        throttle.failure(peer, member_id=member)
        raise HTTPException(status_code=401, detail="Неверный текущий пароль",
                            headers=_PRIVATE) from exc
    except PasswordVersionConflict as exc:
        throttle.failure(peer, member_id=member)
        raise HTTPException(status_code=409, detail=str(exc), headers=_PRIVATE) from exc
    except ValueError as exc:
        throttle.failure(peer, member_id=member)
        raise HTTPException(status_code=422, detail=str(exc), headers=_PRIVATE) from exc
    throttle.success(peer, member_id=member)
    response = Response(status_code=204, headers=_PRIVATE)
    response.delete_cookie(COOKIE_NAME, path="/",
                           secure=request.app.state.settings.browser_session_secure,
                           httponly=True, samesite="strict")
    return response


@router.post("/v1/session/logout", status_code=204)
def logout(request: Request) -> Response:
    token = request.cookies.get(COOKIE_NAME, "")
    if token and request.app.state.team_sessions.resolve(
        token, credentials=request.app.state.team_credentials,
        passwords=request.app.state.team_passwords,
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
