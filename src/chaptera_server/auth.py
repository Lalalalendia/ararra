"""Closed-team API tokens. Future deployment can replace this with invitations and sessions."""
from __future__ import annotations

import hmac

from .config import Settings


class InvalidTeamToken(ValueError):
    """Authentication failed; HTTP adapters decide how to expose the error."""


def authenticate(api_key: str | None, settings: Settings) -> str:
    # No configured tokens means fail closed, not anonymous access.
    authenticated = ""
    for member_id, token in settings.team_tokens.items():
        if hmac.compare_digest(str(api_key or ""), token):
            authenticated = member_id
    if not authenticated:
        raise InvalidTeamToken("Valid team API token required")
    return authenticated
