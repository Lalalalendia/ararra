"""Two-role team identity and administrator-managed moderator access.

Administrative credentials remain a protected host secret. New moderator
tokens are random and hashed in SQLite; only newly issued tokens are returned
once, to the authenticated administrator, with no-store response headers.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ...auth import role_for
from ...team_credentials import MemberConflict, MemberMissing
from ..dependencies import require_admin, require_member
from ..schemas import InviteModerator

router = APIRouter()


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _failed(exc: Exception) -> HTTPException:
    if isinstance(exc, MemberMissing):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, MemberConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/v1/me")
def who_am_i(request: Request, response: Response,
             actor: str = Depends(require_member)):
    _private(response)
    return {
        "member_id": actor,
        "role": role_for(actor, request.app.state.settings,
                         request.app.state.team_credentials),
    }


@router.get("/v1/admin/members")
def admin_team_members(request: Request, response: Response,
                       actor: str = Depends(require_member),
                       admin_actor: str = Depends(require_admin)):
    _private(response)
    return {"members": request.app.state.team_credentials.list_members(request.app.state.settings)}


@router.post("/v1/admin/members", status_code=201)
def issue_moderator(body: InviteModerator, request: Request, response: Response,
                    actor: str = Depends(require_member),
                    admin_actor: str = Depends(require_admin)):
    _private(response)
    try:
        return request.app.state.team_credentials.issue(
            admin_actor, body.member_id, request.app.state.settings,
        )
    except (MemberConflict, ValueError) as exc:
        raise _failed(exc) from exc


@router.post("/v1/admin/members/{member_id}/rotate")
def rotate_moderator(member_id: str, request: Request, response: Response,
                     actor: str = Depends(require_member),
                     admin_actor: str = Depends(require_admin)):
    _private(response)
    try:
        return request.app.state.team_credentials.rotate(
            admin_actor, member_id, request.app.state.settings,
        )
    except (MemberMissing, MemberConflict, ValueError) as exc:
        raise _failed(exc) from exc


@router.post("/v1/admin/members/{member_id}/password")
def issue_temporary_password(member_id: str, request: Request, response: Response,
                             actor: str = Depends(require_member),
                             admin_actor: str = Depends(require_admin)):
    _private(response)
    try:
        return request.app.state.team_passwords.issue_temporary(
            actor_id=admin_actor, member_id=member_id,
            settings=request.app.state.settings,
        )
    except (MemberMissing, MemberConflict, ValueError) as exc:
        raise _failed(exc) from exc


@router.post("/v1/admin/members/{member_id}/disable")
def disable_moderator(member_id: str, request: Request, response: Response,
                      actor: str = Depends(require_member),
                      admin_actor: str = Depends(require_admin)):
    _private(response)
    try:
        return request.app.state.team_credentials.disable(
            admin_actor, member_id, request.app.state.settings,
        )
    except (MemberMissing, MemberConflict, ValueError) as exc:
        raise _failed(exc) from exc
