"""SQLite-backed, opaque browser sessions for existing ReadEra team credentials.

Never store raw team keys or browser tokens. Every request rechecks the current
member credential so rotate/disable (or a host administrator key change)
invalidates existing sessions without relying on caches or background jobs.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from .config import Settings
from .storage import QueueStore
from .team_credentials import TeamCredentials

COOKIE_NAME = "readera_session"
CSRF_HEADER = "X-ReadEra-CSRF"


class TeamSessions:
    def __init__(self, store: QueueStore):
        self.store = store
        with store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS team_browser_sessions (
                    token_sha256 TEXT PRIMARY KEY,
                    member_id TEXT NOT NULL,
                    credential_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS ix_team_browser_sessions_member
                    ON team_browser_sessions(member_id);
            """)

    @staticmethod
    def _digest(secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    @staticmethod
    def csrf_token(token: str) -> str:
        # The raw 256-bit cookie secret is never exposed to JavaScript. Only
        # this purpose-separated, non-bearer proof is returned to the UI.
        return hmac.new(
            token.encode("utf-8"), b"readera:browser-csrf:v1", hashlib.sha256
        ).hexdigest()

    def issue(self, *, member_id: str, team_key: str, ttl_seconds: int) -> str:
        if not member_id or not team_key or ttl_seconds <= 0:
            raise ValueError("Invalid browser session arguments")
        raw = "rs_" + secrets.token_urlsafe(32)
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "DELETE FROM team_browser_sessions WHERE expires_at<=? OR revoked_at IS NOT NULL",
                (now,),
            )
            con.execute(
                """INSERT INTO team_browser_sessions
                   (token_sha256,member_id,credential_sha256,created_at,expires_at)
                   VALUES(?,?,?,?,?)""",
                (self._digest(raw), member_id, self._digest(team_key),
                 now, now + ttl_seconds),
            )
        return raw

    def resolve(self, raw: str | None, *, credentials: TeamCredentials,
                settings: Settings) -> str | None:
        token = str(raw or "")
        if not token.startswith("rs_") or len(token) > 128:
            return None
        with self.store._connect() as con:
            row = con.execute(
                """SELECT member_id,credential_sha256 FROM team_browser_sessions
                   WHERE token_sha256=? AND revoked_at IS NULL AND expires_at>?""",
                (self._digest(token), time.time()),
            ).fetchone()
        if row is None:
            return None
        member_id = str(row["member_id"])
        return member_id if credentials.matches_credential_digest(
            member_id, str(row["credential_sha256"]), settings
        ) else None

    def revoke(self, raw: str | None) -> None:
        token = str(raw or "")
        if not token.startswith("rs_") or len(token) > 128:
            return
        with self.store._connect() as con:
            con.execute(
                "UPDATE team_browser_sessions SET revoked_at=? WHERE token_sha256=?",
                (time.time(), self._digest(token)),
            )
