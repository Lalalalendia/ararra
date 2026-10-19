"""RE-08: encrypted-only Google Drive roundtrip, resumable integrity and privacy."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import httpx
import pytest

from chaptera_server.drive_export import GoogleDriveClient
from chaptera_server.ops_backup import BackupRejected, snapshot
from chaptera_server.ops_backup_crypto import generate_key, seal
from chaptera_server.ops_backup_drive import (
    DriveBackupRejected, EncryptedDriveBackups, backup_once,
)
from chaptera_server.storage import JobSpec, QueueStore


FOLDER = "fakeReadEraBackupFolder001"
SECRET = "Это строго конфиденциальное содержимое романа."
REMOTE_PREFIX = "fakeReadEraRbeArchive"


class FakeDrive:
    """Drive v3 API, not Google's real network or a reusable credential."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.bytes: dict[str, bytes] = {}
        self.session: dict | None = None
        self.created = 0
        self.upload_chunks = 0
        self.media_reads = 0
        self.bad_session = False
        self.bad_chunk_range = False
        self.corrupt_media = False
        self.unwritable = False
        self.ambiguous_finish = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            assert request.method == "POST"
            return httpx.Response(200, json={"access_token": "test-oauth-token"})
        assert request.url.host == "www.googleapis.com"
        assert request.headers.get("authorization") == "Bearer test-oauth-token"
        path = request.url.path

        if path == f"/drive/v3/files/{FOLDER}":
            return httpx.Response(200, json={
                "id": FOLDER, "mimeType": "application/vnd.google-apps.folder",
                "trashed": False,
                "capabilities": {"canAddChildren": not self.unwritable},
            })

        if path == "/drive/v3/files" and request.method == "GET":
            return httpx.Response(200, json={
                "files": list(self.files.values()), "incompleteSearch": False,
            })

        if path.startswith("/drive/v3/files/") and request.method == "GET":
            file_id = path.rsplit("/", 1)[-1]
            if file_id not in self.files:
                return httpx.Response(404)
            if request.url.params.get("alt") == "media":
                self.media_reads += 1
                data = self.bytes[file_id]
                if self.corrupt_media:
                    data = data[:-1] + bytes([data[-1] ^ 1])
                return httpx.Response(200, content=data)
            return httpx.Response(200, json={
                **self.files[file_id],
                "size": str(len(self.bytes[file_id])),
                "md5Checksum": hashlib.md5(
                    self.bytes[file_id], usedforsecurity=False,
                ).hexdigest(),
            })

        if path == "/upload/drive/v3/files" and request.method == "POST":
            assert request.url.params.get("uploadType") == "resumable"
            assert request.headers["x-upload-content-type"] == "application/octet-stream"
            metadata = json.loads(request.content)
            assert metadata["mimeType"] == "application/octet-stream"
            assert metadata["parents"] == [FOLDER]
            self.session = {"metadata": metadata, "data": bytearray(),
                            "total": int(request.headers["x-upload-content-length"])}
            link = (
                "https://attacker.example/upload/drive/v3/files?upload_id=fake"
                if self.bad_session else
                "https://www.googleapis.com/upload/drive/v3/files"
                "?uploadType=resumable&upload_id=mock-1234"
            )
            return httpx.Response(200, headers={"Location": link})

        if path == "/upload/drive/v3/files" and request.method == "PUT":
            assert self.session is not None
            m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                             request.headers["content-range"])
            assert m
            begin, end, total = map(int, m.groups())
            assert begin == len(self.session["data"])
            assert end + 1 - begin == len(request.content)
            assert total == self.session["total"]
            self.session["data"].extend(request.content)
            self.upload_chunks += 1
            if end + 1 < total:
                acknowledged = end - 1 if self.bad_chunk_range else end
                return httpx.Response(
                    308, headers={"Range": f"bytes=0-{acknowledged}"},
                )
            assert len(self.session["data"]) == total
            if self.ambiguous_finish:
                return httpx.Response(503)
            self.created += 1
            fid = f"{REMOTE_PREFIX}{self.created:05d}"
            row = {**self.session["metadata"], "id": fid, "trashed": False}
            self.files[fid] = row
            self.bytes[fid] = bytes(self.session["data"])
            return httpx.Response(200, json=row)

        raise AssertionError(f"Unexpected Google Drive request {request.method} {path}")

    def client(self) -> EncryptedDriveBackups:
        transport = httpx.MockTransport(self)
        http = httpx.Client(
            transport=transport, trust_env=False, follow_redirects=False,
        )
        drive = GoogleDriveClient(
            client_id="synthetic-client", client_secret="synthetic-client-secret",
            refresh_token="synthetic-refresh", http=http,
        )
        return EncryptedDriveBackups(drive, FOLDER)


def _private(root: Path, name: str) -> Path:
    folder = root / name
    folder.mkdir(mode=0o700)
    return folder


def _fixture(tmp_path: Path, *, large: bool = False):
    db = tmp_path / "online.sqlite3"
    store = QueueStore(db)
    source = "<p>" + SECRET * (65000 if large else 8) + "</p>"
    store.create_job("synthetic", JobSpec(chapter_id="fake", source_html=source))
    scratch = _private(tmp_path, "scratch")
    key_folder = _private(tmp_path, "key-vault")
    key_file = generate_key(key_folder / "rbe-backup-key")
    return db, scratch, key_file


def test_daily_backup_uploads_only_ciphertext_and_verified_readback(tmp_path):
    db, scratch, key = _fixture(tmp_path, large=True)
    fake = FakeDrive()
    remote = fake.client()

    proof = backup_once(db, key, scratch, remote)

    assert proof["status"] == "uploaded_verified"
    assert len(proof["archive_sha256"]) == 64
    assert proof["encrypted_bytes"] > 1024 * 1024
    assert fake.created == 1 and fake.upload_chunks >= 2
    assert fake.media_reads == 1
    assert len(fake.bytes) == 1
    payload = next(iter(fake.bytes.values()))
    assert SECRET.encode("utf-8") not in payload
    assert b"SQLite format 3" not in payload
    assert key.read_bytes() not in payload
    assert hashlib.sha256(payload).hexdigest() == proof["archive_sha256"]
    assert not list(scratch.iterdir())  # local plaintext AND encrypted temp removed
    assert db.exists() and key.exists()  # live original/escrow unchanged


def test_drive_upload_is_create_only_and_reuses_identical_verified_remote(tmp_path):
    db, _, key = _fixture(tmp_path)
    snap = _private(tmp_path, "snap")
    enc = _private(tmp_path, "enc")
    archive = seal(snapshot(db, snap), key, enc)
    fake = FakeDrive()
    remote = fake.client()

    first = remote.upload(archive)
    second = remote.upload(archive)

    assert first["status"] == "uploaded_verified"
    assert second["status"] == "verified_existing"
    assert first["remote_file_id"] == second["remote_file_id"]
    assert fake.created == 1 and fake.media_reads == 2


def test_drive_upload_rejects_mismatched_or_corrupted_remote_ciphertext(tmp_path):
    db, scratch, key = _fixture(tmp_path)
    fake = FakeDrive()
    fake.corrupt_media = True
    with pytest.raises(DriveBackupRejected, match="SHA-256"):
        backup_once(db, key, scratch, fake.client())
    assert not list(scratch.iterdir())
    assert fake.created == 1  # held for operator review, NEVER auto-deleted


def test_drive_rejects_out_of_scope_folder_without_copying_live_database(tmp_path):
    db, scratch, key = _fixture(tmp_path)
    fake = FakeDrive()
    fake.unwritable = True
    with pytest.raises(BackupRejected, match="missing or not writable"):
        backup_once(db, key, scratch, fake.client())
    assert fake.created == 0
    assert not list(scratch.iterdir())


@pytest.mark.parametrize("fault", ["evil_redirect", "wrong_range", "ambiguous_commit"])
def test_drive_upload_failure_is_fail_closed_and_no_plaintext(tmp_path, fault):
    db, scratch, key = _fixture(tmp_path, large=True)
    fake = FakeDrive()
    if fault == "evil_redirect":
        fake.bad_session = True
    elif fault == "wrong_range":
        fake.bad_chunk_range = True
    else:
        fake.ambiguous_finish = True

    with pytest.raises(DriveBackupRejected):
        backup_once(db, key, scratch, fake.client())
    assert not list(scratch.iterdir())
    assert fake.created == 0
    assert db.exists()


def test_drive_refuses_plaintext_disguised_as_encrypted_backup(tmp_path):
    fake = FakeDrive()
    folder = _private(tmp_path, "unsafe")
    file = folder / "readera-20261020T000000Z-0123456789abcdef.sqlite3.rbe"
    file.write_bytes(b"SQLite format 3" + b"X" * 2048)
    file.chmod(0o600)
    with pytest.raises(DriveBackupRejected, match="header"):
        fake.client().upload(file)
    assert fake.created == 0
