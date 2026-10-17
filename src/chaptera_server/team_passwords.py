"""Administrator-provisioned Argon2id passwords for EXISTING moderators only.

The administrator remains host-key-only. This service never creates members,
roles, email accounts or public registration; no plaintext credential persists.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from threading import BoundedSemaphore

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .config import Settings
from .storage import QueueStore
from .team_credentials import MemberConflict, MemberMissing, TeamCredentials

_HASHER = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1,
                         hash_len=32, salt_len=16, type=Type.ID)
_DUMMY_HASH = _HASHER.hash("unknown-readera-member-dummy-password")
# On the shared-memory VPS, cap simultaneous 19-MiB Argon2 allocations.
_ARGON_SLOTS = BoundedSemaphore(4)
_VALID_MEMBER = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}\Z")


class PasswordBusy(RuntimeError):
    """Bounded Argon2 worker capacity reached; client may retry."""


class PasswordRejected(ValueError):
    """Invalid password login or old password; do not reveal account status."""


class PasswordVersionConflict(ValueError):
    """Password was changed concurrently."""


def _new_password_valid(password: str) -> None:
    if not 12 <= len(password) <= 128 or len(password.encode("utf-8")) > 256:
        raise ValueError("Password must contain 12–128 characters (up to 256 bytes)")
    if any(ord(char) < 32 or ord(char) == 127 for char in password):
        raise ValueError("Password contains control characters")


def _hashed(password: str) -> str:
    if not _ARGON_SLOTS.acquire(blocking=False):
        raise PasswordBusy("Password verifier is busy")
    try:
        return _HASHER.hash(password)
    finally:
        _ARGON_SLOTS.release()


def _verified(encoded: str, password: str) -> bool:
    if not _ARGON_SLOTS.acquire(blocking=False):
        raise PasswordBusy("Password verifier is busy")
    try:
        try:
            return bool(_HASHER.verify(encoded, password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
    finally:
        _ARGON_SLOTS.release()


class TeamPasswords:
    def __init__(self, store: QueueStore):
        self.store = store
        with store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS team_password_credentials (
                    member_id TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    must_change INTEGER NOT NULL CHECK(must_change IN (0,1)),
                    updated_at REAL NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS team_password_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('issue','reset','change')),
                    created_at REAL NOT NULL
                );
            """)

    def issue_temporary(self, *, actor_id: str, member_id: str,
                        settings: Settings) -> dict:
        if not _VALID_MEMBER.fullmatch(member_id) or member_id == settings.admin_member_id:
            raise MemberConflict("Only registered moderators can receive passwords")
        temporary = "rp_" + secrets.token_urlsafe(24)
        encoded = _hashed(temporary)
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT active FROM team_access_credentials WHERE member_id=?",
                (member_id,),
            ).fetchone()
            if row is None and member_id not in settings.team_tokens:
                raise MemberMissing("Moderator not found")
            if row is not None and not row["active"]:
                raise MemberConflict("Disabled moderator cannot receive a password")
            existed = con.execute(
                "SELECT 1 FROM team_password_credentials WHERE member_id=?",
                (member_id,),
            ).fetchone()
            con.execute(
                """INSERT INTO team_password_credentials
                   (member_id,password_hash,must_change,updated_at,updated_by)
                   VALUES(?,?,1,?,?)
                   ON CONFLICT(member_id) DO UPDATE SET
                     password_hash=excluded.password_hash,
                     must_change=1,
                     updated_at=excluded.updated_at,
                     updated_by=excluded.updated_by""",
                (member_id, encoded, now, actor_id),
            )
            con.execute(
                """UPDATE team_browser_sessions SET revoked_at=?
                   WHERE member_id=? AND auth_kind='password' AND revoked_at IS NULL""",
                (now, member_id),
            )
            con.execute(
                """INSERT INTO team_password_events(member_id,actor_id,action,created_at)
                   VALUES(?,?,?,?)""",
                (member_id, actor_id, "reset" if existed else "issue", now),
            )
        return {"member_id": member_id, "temporary_password": temporary,
                "must_change_password": True}

    def session_fingerprint(self, member_id: str, *, credentials: TeamCredentials,
                            settings: Settings) -> str | None:
        if member_id == settings.admin_member_id:
            return None
        token_digest = credentials.active_credential_digest(member_id, settings)
        if token_digest is None:
            return None
        with self.store._connect() as con:
            row = con.execute(
                "SELECT password_hash FROM team_password_credentials WHERE member_id=?",
                (member_id,),
            ).fetchone()
        if row is None:
            return None
        material = "readera:password-session:v1:" + token_digest + ":" + str(row["password_hash"])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def verify_login(self, *, member_id: str, password: str,
                     credentials: TeamCredentials, settings: Settings) -> str:
        row = None
        if _VALID_MEMBER.fullmatch(member_id) and member_id != settings.admin_member_id:
            with self.store._connect() as con:
                row = con.execute(
                    "SELECT password_hash FROM team_password_credentials WHERE member_id=?",
                    (member_id,),
                ).fetchone()
        encoded = str(row["password_hash"]) if row is not None else _DUMMY_HASH
        valid = _verified(encoded, password)
        if not valid or row is None:
            raise PasswordRejected("Invalid credentials")
        snapshot = self.session_fingerprint(member_id, credentials=credentials, settings=settings)
        if snapshot is None:
            raise PasswordRejected("Invalid credentials")
        with self.store._connect() as con:
            current = con.execute(
                "SELECT password_hash FROM team_password_credentials WHERE member_id=?",
                (member_id,),
            ).fetchone()
        if current is None or not hmac.compare_digest(str(current["password_hash"]), encoded):
            raise PasswordRejected("Invalid credentials")
        return snapshot

    def requires_change(self, member_id: str) -> bool:
        with self.store._connect() as con:
            row = con.execute(
                "SELECT must_change FROM team_password_credentials WHERE member_id=?",
                (member_id,),
            ).fetchone()
        return row is None or bool(row["must_change"])

    def change(self, *, member_id: str, current_password: str,
               new_password: str) -> None:
        with self.store._connect() as con:
            row = con.execute(
                "SELECT password_hash FROM team_password_credentials WHERE member_id=?",
                (member_id,),
            ).fetchone()
        if row is None or not _verified(str(row["password_hash"]), current_password):
            raise PasswordRejected("Invalid credentials")
        _new_password_valid(new_password)
        if hmac.compare_digest(current_password, new_password):
            raise ValueError("The new password must differ from the previous password")
        encoded = _hashed(new_password)
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            changed = con.execute(
                """UPDATE team_password_credentials
                   SET password_hash=?,must_change=0,updated_at=?,updated_by=?
                   WHERE member_id=? AND password_hash=?""",
                (encoded, now, member_id, member_id, str(row["password_hash"])),
            )
            if changed.rowcount != 1:
                raise PasswordVersionConflict("Password changed concurrently")
            con.execute(
                """UPDATE team_browser_sessions SET revoked_at=?
                   WHERE member_id=? AND auth_kind='password' AND revoked_at IS NULL""",
                (now, member_id),
            )
            con.execute(
                """INSERT INTO team_password_events(member_id,actor_id,action,created_at)
                   VALUES(?,?,?,?)""",
                (member_id, member_id, "change", now),
            )
