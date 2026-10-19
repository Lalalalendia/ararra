"""RE-08: read-only Gemini vault escrow witness on an OFFLINE restored SQLite.

The .rbe AES key recovers SQLite, not the independent systemd Gemini vault
credential. Verify that every encrypted moderator project is decryptable
without logging the decrypted API key or the vault master. No writes/network.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import sys

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .ops_backup import (
    BackupRejected, _HEX64, _healthy, _private_directory, _read_only,
    _regular_file,
)
from .personal_gemini_keys import _API_KEY, _PROJECT, _aad, decode_master


def _master_from_private_file(source: Path) -> bytes:
    folder = _private_directory(source.parent)
    secret_file = folder / source.name
    _regular_file(secret_file, private=True)
    if secret_file.stat().st_uid != os.geteuid():
        raise BackupRejected("Gemini vault master must be operator-owned")
    # 32 random bytes encoded as strictly validated RFC4648 Base64 (44 bytes).
    # Permit a single terminal LF, as used by some credential provisioners.
    with secret_file.open("rb") as stream:
        data = stream.read(46)
    if data.endswith(b"\n"):
        data = data[:-1]
    if len(data) != 44:
        raise BackupRejected("Gemini vault credential must be a 32-byte Base64 secret")
    try:
        master = decode_master(data.decode("ascii"))
    except (UnicodeError, ValueError) as exc:
        raise BackupRejected("Gemini vault credential encoding invalid") from exc
    if master is None:
        raise BackupRejected("Gemini vault credential is missing")
    return master


def verify_restored_vault(database: str | Path, vault_key_file: str | Path) -> dict:
    """Prove all saved project keys decrypt on a NEW, standalone recovery image."""
    db_file = Path(database)
    folder = _private_directory(db_file.parent)
    db_file = folder / db_file.name
    _regular_file(db_file, private=True)
    if db_file.stat().st_uid != os.geteuid():
        raise BackupRejected("Restored SQLite must be operator-owned")
    # Never inspect a WAL-backed live database: this is a recovered image only.
    for suffix in ("-wal", "-shm"):
        sidecar = folder / (db_file.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise BackupRejected("A recovery witness must not have SQLite sidecars")
    _healthy(db_file)
    master = _master_from_private_file(Path(vault_key_file))
    aes = AESGCM(master)
    checked = 0
    try:
        with closing(_read_only(db_file)) as con:
            rows = con.execute(
                "SELECT member_id, project_id, nonce, encrypted_key, key_fingerprint "
                "FROM member_gemini_keys ORDER BY project_id"
            )
            for member, project, nonce, encrypted, fingerprint in rows:
                if (not isinstance(member, str) or not member
                        or not isinstance(project, str)
                        or not _PROJECT.fullmatch(project)
                        or not isinstance(fingerprint, str)
                        or not _HEX64.fullmatch(fingerprint)
                        or not isinstance(nonce, bytes) or len(nonce) != 12
                        or not isinstance(encrypted, bytes) or len(encrypted) < 17):
                    raise BackupRejected("Recovered vault row is invalid")
                try:
                    plain = aes.decrypt(nonce, encrypted, _aad(member, project))
                    candidate = plain.decode("ascii")
                except (InvalidTag, UnicodeError, ValueError) as exc:
                    raise BackupRejected("Gemini vault escrow cannot decrypt recovered row") from exc
                if (not _API_KEY.fullmatch(candidate)
                        or not hmac.compare_digest(
                            hmac.new(master, plain, hashlib.sha256).hexdigest(),
                            fingerprint,
                        )):
                    raise BackupRejected("Recovered Gemini credential is invalid")
                checked += 1
    except sqlite3.Error as exc:
        raise BackupRejected("Recovered vault table could not be inspected") from exc
    # An empty table cannot prove the supplied independent vault secret is
    # correct. Never report PASS on a vacuous key-escrow check.
    if checked == 0:
        raise BackupRejected("No encrypted Gemini credentials to test")
    return {"schema": "readera.gemini-vault-recovery.v1", "projects_checked": checked}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify recovered personal Gemini keys with independent vault escrow"
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--vault-key-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        evidence = verify_restored_vault(args.database, args.vault_key_file)
    except (BackupRejected, OSError, sqlite3.Error, ValueError, TypeError):
        # Never print paths, row values, exception strings or recovered secrets.
        print("vault_escrow=REJECTED", file=sys.stderr)
        return 2
    print("vault_escrow=PASS")
    print("projects_checked=" + str(evidence["projects_checked"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
