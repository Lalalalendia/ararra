"""Logged-in user's personal Gemini project credentials, never public keys."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ...quota import read_daily_quota_status
from ...personal_gemini_keys import (
    GeminiKeyConflict, GeminiKeyMissing, GeminiVaultUnavailable,
)
from ..dependencies import require_member

router = APIRouter(prefix="/v1/me/gemini-keys")
_PRIVATE = {"Cache-Control": "private, no-store", "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer"}


class SetDailyCap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Moderator enters a conservative ceiling, NOT an inferred Google quota.
    daily_request_cap: int = Field(ge=1, le=1_000_000)


class SetGeminiKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=6, max_length=64)
    # SecretStr masks sensitive values in repr/validation error traces.
    # Validate length/shape inside the vault without echoing user input.
    api_key: SecretStr


@router.get("")
def mine(request: Request, response: Response, member: str = Depends(require_member)):
    response.headers.update(_PRIVATE)
    state = request.app.state.personal_gemini_keys.list_for(member)
    settings = request.app.state.settings
    for project in state["projects"]:
        cap = project["daily_request_cap"]
        if cap is None:
            project["local_quota"] = None
            continue
        effective = min(cap, settings.gemini_requests_per_day) if settings.gemini_requests_per_day else cap
        project["local_quota"] = {
            **read_daily_quota_status(
                request.app.state.store, project["project_id"],
                settings.gemini_model, effective,
            ),
            "provider_remaining_known": False,
        }
    return state


@router.post("", status_code=201)
def save_key(body: SetGeminiKey, request: Request, response: Response,
             member: str = Depends(require_member)):
    response.headers.update(_PRIVATE)
    try:
        # Project claim is supplied by its owner; no paid Google call at save.
        # Uniqueness and key reuse are checked transactionally in SQLite.
        return request.app.state.personal_gemini_keys.put(
            member, body.project_id, body.api_key.get_secret_value()
        )
    except GeminiVaultUnavailable as exc:
        raise HTTPException(503, "Защищённое хранилище Gemini не настроено",
                            headers=_PRIVATE) from exc
    except GeminiKeyConflict as exc:
        raise HTTPException(409, str(exc), headers=_PRIVATE) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc), headers=_PRIVATE) from exc


@router.put("/{project_id}/daily-cap")
def set_daily_cap(project_id: str, body: SetDailyCap, request: Request,
                  response: Response, member: str = Depends(require_member)):
    response.headers.update(_PRIVATE)
    try:
        return request.app.state.personal_gemini_keys.set_daily_cap(
            member, project_id, body.daily_request_cap,
        )
    except GeminiVaultUnavailable as exc:
        raise HTTPException(503, "Защищённое хранилище Gemini не настроено",
                            headers=_PRIVATE) from exc
    except GeminiKeyMissing as exc:
        raise HTTPException(404, "Проект не найден", headers=_PRIVATE) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc), headers=_PRIVATE) from exc


@router.delete("/{project_id}", status_code=204)
def remove_key(project_id: str, request: Request, response: Response,
               member: str = Depends(require_member)):
    response.headers.update(_PRIVATE)
    try:
        request.app.state.personal_gemini_keys.delete(member, project_id)
    except GeminiVaultUnavailable as exc:
        raise HTTPException(503, "Защищённое хранилище Gemini не настроено",
                            headers=_PRIVATE) from exc
    except GeminiKeyMissing as exc:
        raise HTTPException(404, "Ключ проекта не найден", headers=_PRIVATE) from exc
