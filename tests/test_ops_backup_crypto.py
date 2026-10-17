"""RE-08: authenticated streaming backup bundles, recovery and rejection gates."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.ops_backup import BackupRejected, snapshot
from chaptera_server.ops_backup_crypto import generate_key, seal, unseal
from chaptera_server.storage import JobSpec, QueueStore


def private(root: Path, name: str) -> Path:
    result = root / name
    result.mkdir(mode=0o700)
    return result


def scenario(tmp_path: Path):
    db = tmp_path / "live.sqlite3"
    settings = Settings(db_path=db, team_tokens={"operator": "test-root-token"},
                        admin_member_id="operator", browser_session_secure=False)
    app = create_app(settings)
    member = app.state.team_credentials.issue("operator", "reader", settings)
    key = member["access_token"]
    temporary = app.state.team_passwords.issue_temporary(
        actor_id="operator", member_id="reader", settings=settings
    )["temporary_password"]
    session = app.state.team_sessions.issue(member_id="reader", team_key=key,
                                            ttl_seconds=3600)
    app.state.library.create_book("rbe-book", "Секретная книга",
                                  owner_member_id="reader")
    job = app.state.store.create_job(
        "reader", JobSpec(chapter_id="rbe-chapter",
                          source_html="<p>Длинный секрет " + "a" * 2_100_000 + "</p>")
    )
    unencrypted = private(tmp_path, "snapshots")
    sealed_dir = private(tmp_path, "sealed")
    recovery = private(tmp_path, "recovery")
    keys = private(tmp_path, "keys")
    key_path = generate_key(keys / "recovery.key")
    manifest = snapshot(db, unencrypted)
    return settings, manifest, key_path, sealed_dir, recovery, session, key, job["id"], temporary


def test_encrypt_and_restore_private_auth_and_large_sqlite(tmp_path):
    settings, manifest, secret, folder, targetdir, session, key, job_id, temp = scenario(tmp_path)
    sealed = seal(manifest, secret, folder)
    assert sealed.name.endswith(".sqlite3.rbe")
    assert stat.S_IMODE(sealed.stat().st_mode) == 0o600
    assert list(folder.iterdir()) == [sealed]
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    raw_archive = sealed.read_bytes()
    assert "Секретная книга".encode() not in raw_archive
    assert "Длинный секрет".encode() not in raw_archive
    assert b"test-root-token" not in raw_archive
    assert temp.encode() not in raw_archive
    assert key.encode() not in raw_archive
    assert b"readera.sqlite-snapshot.v1" not in raw_archive

    restored = unseal(sealed, secret, targetdir / "jobs.sqlite3")
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600
    assert not list(targetdir.glob(".readera-incomplete-*"))
    with sqlite3.connect(restored) as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT COUNT(*) FROM books WHERE id='rbe-book'").fetchone()[0] == 1
        assert con.execute("SELECT length(input_json) FROM jobs WHERE id=?",
                           (job_id,)).fetchone()[0] > 2_100_000
        assert con.execute(
            "SELECT COUNT(*) FROM team_password_credentials WHERE member_id='reader'"
        ).fetchone()[0] == 1

    app = create_app(Settings(
        db_path=restored, team_tokens={"operator": "test-root-token"},
        admin_member_id="operator", browser_session_secure=False,
    ), QueueStore(restored))

    async def verify_api():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            client.cookies.set("readera_session", session)
            assert (await client.get("/v1/me")).json()["member_id"] == "reader"
            assert (await client.get("/v1/books")).status_code == 200
            assert (await client.get("/v1/me", headers={"X-API-Key": key})).status_code == 200
    asyncio.run(verify_api())

    with pytest.raises((BackupRejected, FileExistsError)):
        unseal(sealed, secret, restored)
    assert restored.is_file()


@pytest.mark.parametrize("mutation", ["ciphertext", "tag", "nonce", "truncate", "header"])
def test_corrupt_bundle_fails_closed_without_published_db(tmp_path, mutation):
    _, manifest, secret, folder, targetdir, *_ = scenario(tmp_path)
    sealed = seal(manifest, secret, folder)
    data = bytearray(sealed.read_bytes())
    if mutation == "ciphertext":
        data[len(data) // 2] ^= 8
    elif mutation == "tag":
        data[-1] ^= 1
    elif mutation == "nonce":
        data[24] ^= 2
    elif mutation == "header":
        data[0] ^= 4
    else:
        del data[-6:]
    sealed.write_bytes(data)
    with pytest.raises((BackupRejected, ValueError)):
        unseal(sealed, secret, targetdir / "restore.sqlite3")
    assert list(targetdir.iterdir()) == []


def test_wrong_key_rejected_and_key_provisioning_never_overwrites(tmp_path):
    _, manifest, secret, folder, targetdir, *_ = scenario(tmp_path)
    sealed = seal(manifest, secret, folder)
    other = generate_key(secret.parent / "not-the-key")
    assert other.read_bytes() != secret.read_bytes()
    with pytest.raises((BackupRejected, ValueError)):
        unseal(sealed, other, targetdir / "restore.sqlite3")
    with pytest.raises(FileExistsError):
        generate_key(secret)
    assert secret.stat().st_size == 32
    assert list(targetdir.iterdir()) == []


def test_unsafe_encrypted_paths_and_sidecars_are_rejected(tmp_path):
    _, manifest, secret, folder, targetdir, *_ = scenario(tmp_path)
    sealed = seal(manifest, secret, folder)
    symlink = tmp_path / "archive-link"
    symlink.symlink_to(sealed)
    with pytest.raises(BackupRejected):
        unseal(symlink, secret, targetdir / "recover.sqlite3")

    secret.chmod(0o644)
    with pytest.raises(BackupRejected):
        unseal(sealed, secret, targetdir / "recover.sqlite3")
    secret.chmod(0o600)

    sidecar = targetdir / "recover.sqlite3-wal"
    sidecar.symlink_to(secret)
    with pytest.raises(BackupRejected):
        unseal(sealed, secret, targetdir / "recover.sqlite3")
    sidecar.unlink()

    folder.chmod(0o755)
    with pytest.raises(BackupRejected):
        seal(manifest, secret, folder)
    folder.chmod(0o700)

    with pytest.raises(BackupRejected):
        seal(manifest, secret, manifest.parent)

    assert list(targetdir.iterdir()) == []


def test_no_plaintext_leak_on_failed_cli_unseal(tmp_path):
    _, manifest, secret, folder, targetdir, *_ = scenario(tmp_path)
    sealed = seal(manifest, secret, folder)
    other = generate_key(secret.parent / "wrong.key")
    args = [sys.executable, "-m", "chaptera_server.ops_backup_crypto",
            "unseal", "--archive", str(sealed),
            "--key-file", str(other),
            "--destination", str(targetdir / "recovered.sqlite3")]
    proc = subprocess.run(args, capture_output=True, text=True)
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert proc.stderr.strip() == "readera_encrypted_backup=REJECTED"
    assert list(targetdir.iterdir()) == []


def test_key_not_embedded_and_archive_has_random_nonce(tmp_path):
    _, manifest, secret, folder, targetdir, *_ = scenario(tmp_path)
    sealed = seal(manifest, secret, folder)
    blob = sealed.read_bytes()
    assert secret.read_bytes() not in blob
    # Recreating an archive with the same name fails closed. A new folder
    # creates a second encrypted bundle with distinct nonce/ciphertext.
    second_dir = private(tmp_path, "sealed2")
    repeated = seal(manifest, secret, second_dir)
    assert repeated.read_bytes() != blob
    assert repeated.read_bytes()[:23] == blob[:23]
    with pytest.raises(FileExistsError):
        seal(manifest, secret, folder)
