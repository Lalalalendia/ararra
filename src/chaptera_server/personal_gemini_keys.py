"""Encrypted, actor-owned Gemini credentials: exactly one key per Google project.

No keys appear in SQLite plaintext, HTTP responses, URL paths, or audit text.
Project IDs are operator-supplied identifiers, not Google-verified ownership.
The same recovery-independent 256-bit vault key must be available to API and
worker. Missing key fails closed for mutation and credential resolution.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import sqlite3
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .storage import QueueStore

_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,61}[a-z0-9]\Z")
_API_KEY = re.compile(r"[A-Za-z0-9_-]{20,256}\Z")
_AAD = b"readera-personal-gemini-v1\x00"


class GeminiKeyConflict(ValueError):
    """Another moderator already owns this project or key."""


class GeminiKeyMissing(LookupError):
    """No key is available for this moderator/project."""


class GeminiVaultUnavailable(RuntimeError):
    """The service lacks its protected master-key credential."""


def decode_master(secret: str) -> bytes | None:
    """Only a base64-encoded random 32-byte secret is accepted."""
    if not secret:
        return None
    try:
        raw = base64.b64decode(secret, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid Gemini vault master-key encoding") from exc
    if len(raw) != 32:
        raise ValueError("Gemini vault master key must contain 32 random bytes")
    return raw


def _aad(member: str, project: str) -> bytes:
    return _AAD + member.encode("utf-8") + b"\x00" + project.encode("utf-8")


class PersonalGeminiKeys:
    def __init__(self, store: QueueStore, master_key_b64: str = ""):
        self.store = store
        self._key = decode_master(master_key_b64)
        with store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS member_gemini_keys (
                    project_id TEXT PRIMARY KEY,
                    member_id TEXT NOT NULL,
                    nonce BLOB NOT NULL,
                    encrypted_key BLOB NOT NULL,
                    key_fingerprint TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_member_gemini_keys_member
                    ON member_gemini_keys(member_id, project_id);
            """)

    @property
    def configured(self) -> bool:
        return self._key is not None

    def _require_key(self) -> bytes:
        if self._key is None:
            raise GeminiVaultUnavailable("Protected Gemini key storage is unavailable")
        return self._key

    def list_for(self, member_id: str) -> dict:
        with self.store._connect() as con:
            rows = con.execute(
                "SELECT project_id, updated_at FROM member_gemini_keys "
                "WHERE member_id=? ORDER BY project_id", (member_id,)
            ).fetchall()
        return {
            "configured": self.configured,
            "projects": [
                {"project_id": str(row["project_id"]), "updated_at": float(row["updated_at"])}
                for row in rows
            ],
        }

    def put(self, member_id: str, project_id: str, api_key: str) -> dict:
        vault = self._require_key()
        if not _PROJECT.fullmatch(project_id):
            raise ValueError("Invalid Google project ID")
        if not _API_KEY.fullmatch(api_key):
            raise ValueError("Invalid Gemini API key format")
        fingerprint = hmac.new(vault, api_key.encode("ascii"), hashlib.sha256).hexdigest()
        nonce = os.urandom(12)
        ciphertext = AESGCM(vault).encrypt(nonce, api_key.encode("ascii"), _aad(member_id, project_id))
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            project_row = con.execute(
                "SELECT member_id FROM member_gemini_keys WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if project_row and project_row["member_id"] != member_id:
                raise GeminiKeyConflict("Google project is already registered")
            duplicate = con.execute(
                "SELECT project_id FROM member_gemini_keys WHERE key_fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if duplicate and duplicate["project_id"] != project_id:
                raise GeminiKeyConflict("Gemini key is already registered for another project")
            con.execute("""
                INSERT INTO member_gemini_keys (
                    project_id,member_id,nonce,encrypted_key,key_fingerprint,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(project_id) DO UPDATE SET
                    nonce=excluded.nonce,
                    encrypted_key=excluded.encrypted_key,
                    key_fingerprint=excluded.key_fingerprint,
                    updated_at=excluded.updated_at
            """, (project_id, member_id, nonce, ciphertext, fingerprint, now, now))
        return {"project_id": project_id, "updated_at": now}

    def delete(self, member_id: str, project_id: str) -> None:
        self._require_key()
        with self.store._connect() as con:
            count = con.execute(
                "DELETE FROM member_gemini_keys WHERE member_id=? AND project_id=?",
                (member_id, project_id),
            ).rowcount
        if count != 1:
            raise GeminiKeyMissing("Gemini project was not found")

    def choices(self, member_id: str) -> list[tuple[str, str]]:
        """Worker-only non-secret project+fingerprint listing."""
        self._require_key()
        with self.store._connect() as con:
            rows = con.execute(
                "SELECT project_id,key_fingerprint FROM member_gemini_keys "
                "WHERE member_id=? ORDER BY project_id", (member_id,)
            ).fetchall()
        return [(str(r["project_id"]), str(r["key_fingerprint"])) for r in rows]

    def resolve(self, member_id: str, project_id: str, fingerprint: str) -> str:
        """Worker-only, fail closed after deletion, rotation, or key corruption."""
        vault = self._require_key()
        with self.store._connect() as con:
            row = con.execute(
                "SELECT nonce,encrypted_key,key_fingerprint FROM member_gemini_keys "
                "WHERE member_id=? AND project_id=?",
                (member_id, project_id),
            ).fetchone()
        if not row or not hmac.compare_digest(str(row["key_fingerprint"]), fingerprint):
            raise GeminiKeyMissing("Gemini credential was rotated or revoked")
        try:
            data = AESGCM(vault).decrypt(
                bytes(row["nonce"]), bytes(row["encrypted_key"]), _aad(member_id, project_id)
            )
        except (InvalidTag, ValueError) as exc:
            raise GeminiVaultUnavailable("Encrypted Gemini credential cannot be opened") from exc
        secret = data.decode("ascii")
        if (not _API_KEY.fullmatch(secret) or
                not hmac.compare_digest(
                    hmac.new(vault, data, hashlib.sha256).hexdigest(), fingerprint
                )):
            raise GeminiVaultUnavailable("Encrypted Gemini credential integrity failed")
        return secret

    def is_current(self, member_id: str, project_id: str, fingerprint: str) -> bool:
        with self.store._connect() as con:
            row = con.execute(
                "SELECT key_fingerprint FROM member_gemini_keys "
                "WHERE member_id=? AND project_id=?", (member_id, project_id)
            ).fetchone()
        return bool(row) and hmac.compare_digest(str(row["key_fingerprint"]), fingerprint)
