"""RE-08 recovery tests: WAL, private directories, checksum, fail-closed restore."""
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
from chaptera_server.ops_backup import BackupRejected, restore, snapshot, verify
from chaptera_server.storage import JobSpec, QueueStore


def _sample(tmp_path: Path) -> tuple[Path, str, str]:
    db = tmp_path / "live.sqlite3"
    settings = Settings(db_path=db, team_tokens={"chief": "admin-secret-for-ops"},
                        admin_member_id="chief", browser_session_secure=False)
    app = create_app(settings)
    created = app.state.team_credentials.issue("chief", "writer", settings)
    member_key = created["access_token"]
    job = app.state.store.create_job(
        "writer", JobSpec(chapter_id="ch101", source_html="<p>秘密 источника</p>")
    )
    app.state.library.create_book("shared", "Книга восстановления", owner_member_id="writer")
    app.state.team_passwords.issue_temporary(
        actor_id="chief", member_id="writer", settings=settings
    )
    raw = app.state.team_sessions.issue(
        member_id="writer", team_key=member_key, ttl_seconds=7200
    )
    # Force writes in WAL, without copying any sidecar manually.
    with app.state.store._connect() as con:
        con.execute("INSERT INTO job_events(job_id,stage,detail,created_at) VALUES(?,?,?,?)",
                    (job["id"], "recovery_test", "durable", 1234.5))
    return db, raw, member_key


def _private(tmp_path: Path, name: str) -> Path:
    out = tmp_path / name
    out.mkdir(mode=0o700)
    return out


def test_backup_wal_and_restore_all_state_without_overwrite(tmp_path):
    db, raw_session, member_key = _sample(tmp_path)
    folder = _private(tmp_path, "snapshots")
    # Keep a real reader connection open so recent committed writes remain
    # in WAL: raw cp jobs.sqlite3 would miss this inserted event.
    live = sqlite3.connect(db)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute(
        "INSERT INTO job_events(job_id,stage,detail,created_at) "
        "SELECT id,'wal_uncheckpointed','recent',4321.0 FROM jobs LIMIT 1"
    )
    live.commit()
    assert db.with_name(db.name + "-wal").exists()
    manifest = snapshot(db, folder)
    live.close()
    backup = manifest.with_name(manifest.name.removesuffix(".manifest.json"))
    meta = verify(manifest)
    assert meta["schema"] == "readera.sqlite-snapshot.v1"
    assert meta["file"] == backup.name
    assert meta["bytes"] == backup.stat().st_size
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    with sqlite3.connect(backup) as standalone:
        assert standalone.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not backup.with_name(backup.name + "-wal").exists()
    assert not backup.with_name(backup.name + "-shm").exists()
    assert "Книга восстановления" not in manifest.read_text()
    assert "admin-secret-for-ops" not in manifest.read_text()
    assert not list(folder.glob(".readera-incomplete-*"))

    # Writes made after the completed point-in-time snapshot must be absent.
    with sqlite3.connect(db) as con:
        con.execute("UPDATE jobs SET last_error='after-snapshot'")

    restore_dir = _private(tmp_path, "recovery")
    target = restore(manifest, restore_dir / "recovered.sqlite3")
    assert target.is_file() and stat.S_IMODE(target.stat().st_mode) == 0o600
    with sqlite3.connect(target) as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute(
            "SELECT COUNT(*) FROM books WHERE id='shared'"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM job_events WHERE stage='recovery_test'"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM job_events WHERE stage='wal_uncheckpointed'"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM team_password_credentials WHERE member_id='writer'"
        ).fetchone()[0] == 1
        assert con.execute("SELECT last_error FROM jobs").fetchone()[0] is None

    app = create_app(Settings(
        db_path=target, team_tokens={"chief": "admin-secret-for-ops"},
        admin_member_id="chief", browser_session_secure=False,
    ), QueueStore(target))

    async def browser():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.cookies.set("readera_session", raw_session)
            assert (await client.get("/v1/me")).json()["member_id"] == "writer"
            assert (await client.get("/v1/books")).status_code == 200
            assert (await client.get("/v1/me", headers={
                "X-API-Key": member_key,
            })).status_code == 200
    asyncio.run(browser())

    with pytest.raises(BackupRejected):
        restore(manifest, target)
    assert target.exists()


def test_modified_backup_must_not_create_partial_restore(tmp_path):
    db, _, _ = _sample(tmp_path)
    folder = _private(tmp_path, "snapshots")
    manifest = snapshot(db, folder)
    source = folder / verify(manifest)["file"]
    with source.open("ab") as stream:
        stream.write(b"tamper")
    targetdir = _private(tmp_path, "recovery")
    with pytest.raises(BackupRejected):
        verify(manifest)
    with pytest.raises(BackupRejected):
        restore(manifest, targetdir / "new.sqlite3")
    assert list(targetdir.iterdir()) == []


def test_reject_unsafe_manifest_symlink_and_permissive_directory(tmp_path):
    db, _, _ = _sample(tmp_path)
    folder = _private(tmp_path, "private")
    manifest = snapshot(db, folder)
    details = json.loads(manifest.read_text(encoding="utf-8"))
    assert details["file"].startswith("readera-")
    details["file"] = "../live.sqlite3"
    manifest.write_text(json.dumps(details), encoding="utf-8")
    with pytest.raises(BackupRejected):
        verify(manifest)
    folder.chmod(0o755)
    with pytest.raises(BackupRejected):
        snapshot(db, folder)
    folder.chmod(0o700)

    mirror = tmp_path / "alias"
    mirror.symlink_to(folder, target_is_directory=True)
    with pytest.raises(BackupRejected):
        snapshot(db, mirror)


def test_restore_does_not_replace_sidecar_or_existing_target(tmp_path):
    db, _, _ = _sample(tmp_path)
    folder = _private(tmp_path, "private")
    manifest = snapshot(db, folder)
    recover = _private(tmp_path, "recover")
    target = recover / "jobs.sqlite3"
    target.with_name(target.name + "-wal").write_bytes(b"should-not-touch")
    with pytest.raises(BackupRejected):
        restore(manifest, target)
    assert target.with_name(target.name + "-wal").read_bytes() == b"should-not-touch"
    target.with_name(target.name + "-wal").unlink()
    target.write_bytes(b"existing state")
    with pytest.raises(BackupRejected):
        restore(manifest, target)
    assert target.read_bytes() == b"existing state"


def test_command_line_offline_and_no_contents_in_stdout(tmp_path):
    db, _, _ = _sample(tmp_path)
    folder = _private(tmp_path, "private")
    output = subprocess.run(
        [sys.executable, "-m", "chaptera_server.ops_backup",
         "snapshot", "--database", str(db), "--directory", str(folder)],
        text=True, capture_output=True, check=True,
    )
    assert output.stdout.startswith("snapshot=PASS")
    assert "Книга" not in output.stdout
    files = list(folder.glob("*.manifest.json"))
    assert len(files) == 1
    result = subprocess.run(
        [sys.executable, "-m", "chaptera_server.ops_backup",
         "verify", "--manifest", str(files[0])],
        text=True, capture_output=True, check=True,
    )
    assert "verify=PASS" in result.stdout
    recovered = _private(tmp_path, "fresh") / "data.sqlite3"
    result = subprocess.run(
        [sys.executable, "-m", "chaptera_server.ops_backup",
         "restore", "--manifest", str(files[0]), "--destination", str(recovered)],
        text=True, capture_output=True, check=True,
    )
    assert "restore=PASS" in result.stdout
    second = subprocess.run(
        [sys.executable, "-m", "chaptera_server.ops_backup",
         "restore", "--manifest", str(files[0]), "--destination", str(recovered)],
        text=True, capture_output=True,
    )
    assert second.returncode == 2
    assert "REJECTED" in second.stderr


def test_backup_rejects_valid_checksum_but_nonportable_wal_journal(tmp_path):
    import hashlib

    db, _, _ = _sample(tmp_path)
    folder = _private(tmp_path, "private")
    manifest = snapshot(db, folder)
    meta = verify(manifest)
    image = folder / meta["file"]
    with sqlite3.connect(image) as con:
        assert con.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    # An attacker with access to the private folder might rewrite SHA metadata.
    # The standalone-journal invariant must fail independently of SHA256.
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
    payload["bytes"] = image.stat().st_size
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BackupRejected):
        verify(manifest)
