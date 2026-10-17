"""Two-role team identity endpoints for the closed ReadEra workspace.

Tokens remain operator-managed credentials; only member IDs and roles are
returned. Moderators cannot enumerate the roster or change global privileges.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...auth import role_for
from ..dependencies import require_admin, require_member

router = APIRouter()


@router.get("/v1/me")
def who_am_i(request: Request, actor: str = Depends(require_member)):
    return {"member_id": actor, "role": role_for(actor, request.app.state.settings)}


@router.get("/v1/admin/members")
def admin_team_members(request: Request, actor: str = Depends(require_member),
                       admin_actor: str = Depends(require_admin)):
    """Admin-only, read-only membership inventory; never return secret tokens."""
    settings = request.app.state.settings
    return {
        "members": [
            {"member_id": member_id, "role": role_for(member_id, settings)}
            for member_id in sorted(settings.team_tokens)
        ]
    }
