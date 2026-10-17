"""SQLite-backed moderator credentials for the closed ReadEra workspace.

Two GLOBAL roles only. The single administrator stays in protected host config.
The admin may issue and revoke *moderator* bearer tokens without editing a
running systemd credential. Credentials are 256-bit random, stored only as
SHA-256 digests and returned to the administrator exactly once on issue/rotate.

No passwords, user-provided tokens, emails, per-book permissions or sessions.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time

from .config import Settings
from .storage import QueueStore

_MEMBER_ID = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}\Z")


class MemberMissing(LookupError):
    """Moderator ID not registered."""


class MemberConflict(ValueError):
    """Illegal change to a system administrator, duplicate, or book owner."""


class TeamCredentials:
    def __init__(self, store: QueueStore):
        self.store = store
        with store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS team_access_credentials (
                    member_id TEXT PRIMARY KEY,
                    token_sha256 TEXT UNIQUE,
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS team_access_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (action IN ('issue','rotate','disable')),
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_team_access_events_member
                    ON team_access_events(member_id, id);
            """)

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _new_secret() -> str:
        return "re_" + secrets.token_urlsafe(32)

    @staticmethod
    def _audit(con: sqlite3.Connection, member_id: str, actor_id: str,
               action: str, now: float) -> None:
        con.execute(
            "INSERT INTO team_access_events(member_id,actor_id,action,created_at) "
            "VALUES(?,?,?,?)", (member_id, actor_id, action, now),
        )

    def authenticate(self, api_key: str | None, settings: Settings) -> str | None:
        """Resolve configured and managed credentials after DB override checks.

        A static moderator's old environment key is rejected immediately after
        admin rotation/revocation. The environment administrator is never
        eligible for overrides, even if a damaged DB row exists.
        """
        token = str(api_key or "")
        if not token:
            return None
        static_id = None
        for member_id, configured in settings.team_tokens.items():
            if hmac.compare_digest(token, configured):
                static_id = member_id
        with self.store._connect() as con:
            if static_id:
                if static_id == settings.admin_member_id:
                    return static_id
                override = con.execute(
                    "SELECT active,token_sha256 FROM team_access_credentials WHERE member_id=?",
                    (static_id,),
                ).fetchone()
                if override is None:
                    return static_id
                # Explicit override deactivates the old static token.
            match = con.execute(
                "SELECT member_id,active FROM team_access_credentials WHERE token_sha256=?",
                (self._digest(token),),
            ).fetchone()
            return str(match["member_id"]) if match and match["active"] else None

    def is_active(self, member_id: str, settings: Settings) -> bool:
        if member_id == settings.admin_member_id and member_id in settings.team_tokens:
            return True
        with self.store._connect() as con:
            row = con.execute(
                "SELECT active FROM team_access_credentials WHERE member_id=?", (member_id,),
            ).fetchone()
            if row is not None:
                return bool(row["active"])
            return member_id in settings.team_tokens

    def list_members(self, settings: Settings) -> list[dict]:
        """Admin-only caller; never returns digests, tokens or credentials."""
        with self.store._connect() as con:
            by_id = {
                str(r["member_id"]): r for r in con.execute(
                    "SELECT member_id,active,token_sha256,updated_at "
                    "FROM team_access_credentials"
                )
            }
        result = []
        for member_id in sorted(set(settings.team_tokens) | set(by_id)):
            row = by_id.get(member_id)
            is_admin = member_id == settings.admin_member_id
            status = "active" if (is_admin or row is None or row["active"]) else "disabled"
            source = ("managed" if row is not None and row["token_sha256"]
                      else "configuration")
            result.append({
                "member_id": member_id,
                "role": "admin" if is_admin else "moderator",
                "status": status,
                "credential_source": source,
            })
        return result

    def issue(self, actor_id: str, member_id: str, settings: Settings) -> dict:
        if not _MEMBER_ID.fullmatch(member_id):
            raise ValueError("Member ID must start with a Latin letter and contain up to 64 safe characters")
        if member_id == settings.admin_member_id or member_id in settings.team_tokens:
            raise MemberConflict("Member ID is already reserved by host configuration")
        secret, now = self._new_secret(), time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute(
                "SELECT 1 FROM team_access_credentials WHERE member_id=?", (member_id,),
            ).fetchone():
                raise MemberConflict("Member ID already exists")
            con.execute(
                """INSERT INTO team_access_credentials
                   (member_id,token_sha256,active,created_at,updated_at,created_by,updated_by)
                   VALUES(?,?,1,?,?,?,?)""",
                (member_id, self._digest(secret), now, now, actor_id, actor_id),
            )
            self._audit(con, member_id, actor_id, "issue", now)
        return {"member_id": member_id, "role": "moderator", "status": "active",
                "access_token": secret}

    @staticmethod
    def _ensure_moderator(member_id: str, settings: Settings,
                          con: sqlite3.Connection) -> sqlite3.Row | None:
        if member_id == settings.admin_member_id:
            raise MemberConflict("Administrator credentials are managed only on the host")
        row = con.execute(
            "SELECT * FROM team_access_credentials WHERE member_id=?", (member_id,),
        ).fetchone()
        if member_id not in settings.team_tokens and row is None:
            raise MemberMissing("Moderator not found")
        return row

    def rotate(self, actor_id: str, member_id: str, settings: Settings) -> dict:
        secret, now = self._new_secret(), time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._ensure_moderator(member_id, settings, con)
            if row is None:
                con.execute(
                    """INSERT INTO team_access_credentials
                       (member_id,token_sha256,active,created_at,updated_at,created_by,updated_by)
                       VALUES(?,?,1,?,?,?,?)""",
                    (member_id, self._digest(secret), now, now, actor_id, actor_id),
                )
            else:
                con.execute(
                    """UPDATE team_access_credentials
                       SET token_sha256=?,active=1,updated_at=?,updated_by=? WHERE member_id=?""",
                    (self._digest(secret), now, actor_id, member_id),
                )
            self._audit(con, member_id, actor_id, "rotate", now)
        return {"member_id": member_id, "role": "moderator", "status": "active",
                "access_token": secret}

    def disable(self, actor_id: str, member_id: str, settings: Settings) -> dict:
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._ensure_moderator(member_id, settings, con)
            # A book's designated moderator is its accountable owner (NOT
            # a permissions role). If book ownership is enabled (RE-03 PR#17),
            # require a deliberate owner transfer before disabling that account.
            columns = {c["name"] for c in con.execute("PRAGMA table_info(books)")}
            if "owner_member_id" in columns:
                owner = con.execute(
                    "SELECT id FROM books WHERE owner_member_id=? LIMIT 1", (member_id,),
                ).fetchone()
                if owner:
                    raise MemberConflict("Reassign owned books before disabling this moderator")
            if row is not None and not row["active"]:
                return {"member_id": member_id, "role": "moderator", "status": "disabled"}
            if row is None:
                con.execute(
                    """INSERT INTO team_access_credentials
                       (member_id,token_sha256,active,created_at,updated_at,created_by,updated_by)
                       VALUES(?,NULL,0,?,?,?,?)""",
                    (member_id, now, now, actor_id, actor_id),
                )
            else:
                con.execute(
                    """UPDATE team_access_credentials
                       SET token_sha256=NULL,active=0,updated_at=?,updated_by=? WHERE member_id=?""",
                    (now, actor_id, member_id),
                )
            # Outstanding requests must not initiate new paid Gemini calls
            # after the moderator is disabled. In-flight calls are signalled to
            # cooperatively cancel; existing completed results remain durable.
            pending = con.execute(
                "SELECT id,status FROM jobs WHERE actor_id=? AND status IN ('queued','retry_wait','running')",
                (member_id,),
            ).fetchall()
            for job in pending:
                if job["status"] == "running":
                    con.execute(
                        "UPDATE jobs SET cancel_requested=1,updated_at=? WHERE id=?",
                        (now, job["id"]),
                    )
                    stage = "cancel_requested"
                else:
                    con.execute(
                        "UPDATE jobs SET status='cancelled',cancel_requested=1,updated_at=? WHERE id=?",
                        (now, job["id"]),
                    )
                    stage = "cancelled"
                con.execute(
                    "INSERT INTO job_events(job_id,stage,detail,created_at) VALUES(?,?,?,?)",
                    (job["id"], stage, "member_disabled", now),
                )
            self._audit(con, member_id, actor_id, "disable", now)
        return {"member_id": member_id, "role": "moderator", "status": "disabled"}
