"""Closed-team API tokens. Future deployment can replace this with invitations and sessions."""
from __future__ import annotations

import hmac

from .config import Settings


class InvalidTeamToken(ValueError):
    """Authentication failed; HTTP adapters decide how to expose the error."""


def authenticate(api_key: str | None, settings: Settings, team_credentials=None) -> str:
    if team_credentials is not None:
        identity = team_credentials.authenticate(api_key, settings)
        if identity is None:
            raise InvalidTeamToken("Valid team API token required")
        return identity
    # Legacy/local authentication without managed credentials remains supported.
    # No configured tokens means fail closed, not anonymous access.
    authenticated = ""
    for member_id, token in settings.team_tokens.items():
        if hmac.compare_digest(str(api_key or ""), token):
            authenticated = member_id
    if not authenticated:
        raise InvalidTeamToken("Valid team API token required")
    return authenticated


def role_for(member_id: str, settings: Settings, team_credentials=None) -> str:
    """Two global roles only: one configured admin, everyone else moderator.

    The caller must be an authenticated configured team member; this helper
    deliberately does not guess an administrator from member order or names.
    """
    known = member_id in settings.team_tokens
    if team_credentials is not None:
        known = team_credentials.is_active(member_id, settings)
    if not known:
        raise InvalidTeamToken("Valid team member required")
    return "admin" if settings.admin_member_id and member_id == settings.admin_member_id else "moderator"
