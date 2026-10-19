"""RE-08 Google Drive off-host transport: authenticated encrypted SQLite only.

The Drive OAuth scope stays drive.file. This code never uploads the SQLite,
manifest, private keys, or an unencrypted recovery artifact. It creates new
application/octet-stream files only; no remote update, delete or sharing.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

import httpx

from .config import _read_systemd_credential
from .drive_export import DriveExportError, GoogleDriveClient
from .ops_backup import BackupRejected, _private_directory, _regular_file, snapshot
from .ops_backup_crypto import _MAGIC, _OUTER, seal, unseal

MIME = "application/octet-stream"
CHUNK = 1024 * 1024  # Google resumable chunks must be 256-KiB multiples.
MAX_BYTES = 10 * 1024 * 1024 * 1024
_TAG = "readeraOffsiteFormat"
_FORMAT = "readera-aes256-gcm-v1"
_FOLDER_ENV = "CHAPTERA_BACKUP_DRIVE_FOLDER_ID"
_NAME_SHA = re.compile(r"[a-f0-9]{64}\Z")


class DriveBackupRejected(BackupRejected):
    """Refuse ambiguous writes, unknown remote identities and unsafe inputs."""


def _digest(path: Path) -> tuple[str, str, int]:
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(CHUNK):
            sha.update(block)
            md5.update(block)
            size += len(block)
            if size > MAX_BYTES:
                raise DriveBackupRejected("Backup archive exceeds supported size")
    return sha.hexdigest(), md5.hexdigest(), size


def _check_archive(path: Path) -> tuple[str, str, int]:
    _private_directory(path.parent)
    _regular_file(path, private=True)
    if not _OUTER.fullmatch(path.name):
        raise DriveBackupRejected("Only a ReadEra sealed .rbe archive can be uploaded")
    with path.open("rb") as stream:
        if stream.read(len(_MAGIC)) != _MAGIC:
            raise DriveBackupRejected("Encrypted backup header is invalid")
    sha, md5, size = _digest(path)
    if size < len(_MAGIC) + 12 + 16 + 512:
        raise DriveBackupRejected("Encrypted backup is too small")
    return sha, md5, size


def _session_url(value: str) -> str:
    if not isinstance(value, str):
        raise DriveBackupRejected("Google Drive resumable session missing")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != "www.googleapis.com"
            or parsed.path != "/upload/drive/v3/files" or parsed.fragment
            or parsed.username or parsed.password):
        raise DriveBackupRejected("Google Drive returned untrusted upload session")
    params = parse_qs(parsed.query)
    if not params.get("upload_id") or len(params["upload_id"]) != 1:
        raise DriveBackupRejected("Google Drive returned invalid upload session")
    return value


class EncryptedDriveBackups:
    """Create-only resumable backup transport with full remote byte readback."""

    def __init__(self, drive: GoogleDriveClient, folder_id: str):
        self.drive = drive
        self.folder = drive._check_id(folder_id)

    def probe(self) -> None:
        # Particularly important for drive.file: the separate OAuth application
        # may not see a folder created through another Google Drive connector.
        self.drive.check_destination(self.folder)

    def _file_metadata(self, file_id: str) -> dict:
        fid = self.drive._check_id(file_id)
        response = self.drive._request(
            "GET", f"https://www.googleapis.com/drive/v3/files/{fid}",
            params={"fields": "id,name,mimeType,parents,appProperties,size,md5Checksum,trashed"},
        )
        return self.drive._json(response)

    @staticmethod
    def _expected_tags(sha: str) -> dict:
        if not _NAME_SHA.fullmatch(sha):
            raise DriveBackupRejected("Invalid SHA-256 for remote backup identity")
        return {_TAG: _FORMAT, "readeraEncryptedSha256": sha}

    def _identity(self, metadata: dict, name: str, sha: str, md5: str, size: int) -> str:
        candidate = metadata.get("id")
        if not isinstance(candidate, str):
            raise DriveBackupRejected("Remote backup identity missing")
        file_id = self.drive._check_id(candidate)
        if (metadata.get("name") != name or metadata.get("mimeType") != MIME
                or metadata.get("parents") != [self.folder]
                or metadata.get("appProperties") != self._expected_tags(sha)
                or metadata.get("trashed") is True
                or str(metadata.get("size")) != str(size)
                or metadata.get("md5Checksum") != md5):
            raise DriveBackupRejected("Google Drive backup metadata mismatch")
        return file_id

    def _readback(self, file_id: str, sha: str, size: int) -> None:
        fid = self.drive._check_id(file_id)
        url = f"https://www.googleapis.com/drive/v3/files/{fid}"
        got = hashlib.sha256()
        read = 0
        try:
            with self.drive.http.stream(
                "GET", url, params={"alt": "media"},
                headers={"Authorization": f"Bearer {self.drive._access_token()}"},
                follow_redirects=False, timeout=120.0,
            ) as response:
                if response.status_code != 200:
                    raise DriveBackupRejected("Google Drive archive readback failed")
                for block in response.iter_bytes(CHUNK):
                    read += len(block)
                    if read > size:
                        raise DriveBackupRejected("Google Drive archive is oversized")
                    got.update(block)
        except httpx.HTTPError as exc:
            raise DriveBackupRejected("Google Drive encrypted readback unavailable") from exc
        if read != size or not hmac.compare_digest(got.hexdigest(), sha):
            raise DriveBackupRejected("Encrypted archive readback SHA-256 mismatch")

    def _upload_new(self, path: Path, sha: str, size: int) -> str:
        metadata = {
            "name": path.name, "mimeType": MIME, "parents": [self.folder],
            "appProperties": self._expected_tags(sha),
        }
        try:
            initiated = self.drive.http.post(
                "https://www.googleapis.com/upload/drive/v3/files",
                params={"uploadType": "resumable",
                        "fields": "id,name,mimeType,parents,appProperties,size,md5Checksum"},
                json=metadata,
                headers={
                    "Authorization": f"Bearer {self.drive._access_token()}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": MIME,
                    "X-Upload-Content-Length": str(size),
                },
                follow_redirects=False, timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise DriveBackupRejected("Remote backup creation is ambiguous; review before retry") from exc
        if initiated.status_code != 200:
            raise DriveBackupRejected("Remote backup resumable initiation rejected")
        session = _session_url(initiated.headers.get("Location", ""))
        final = None
        with path.open("rb") as stream:
            written = 0
            while written < size:
                payload = stream.read(min(CHUNK, size - written))
                if not payload:
                    raise DriveBackupRejected("Sealed archive changed during upload")
                end = written + len(payload) - 1
                try:
                    response = self.drive.http.put(
                        session, content=payload,
                        headers={
                            "Authorization": f"Bearer {self.drive._access_token()}",
                            "Content-Type": MIME,
                            "Content-Length": str(len(payload)),
                            "Content-Range": f"bytes {written}-{end}/{size}",
                        },
                        follow_redirects=False, timeout=120.0,
                    )
                except httpx.HTTPError as exc:
                    raise DriveBackupRejected("Remote backup upload outcome is ambiguous") from exc
                if end + 1 < size:
                    # Strict sequential acknowledgement; never silently skip
                    # bytes or restart with another remote object.
                    if (response.status_code != 308 or
                            response.headers.get("Range") != f"bytes=0-{end}"):
                        raise DriveBackupRejected("Incomplete or ambiguous Drive chunk receipt")
                else:
                    if response.status_code not in (200, 201):
                        raise DriveBackupRejected("Final Drive upload receipt missing")
                    final = self.drive._json(response)
                written = end + 1
        if not isinstance(final, dict):
            raise DriveBackupRejected("Drive did not confirm a complete upload")
        return self.drive._check_id(final.get("id", ""))

    def upload(self, archive: Path) -> dict:
        sha, md5, size = _check_archive(archive)
        self.probe()
        matches = [
            item for item in self.drive.children(self.folder)
            if item.get("name") == archive.name
            or (item.get("appProperties") or {}).get("readeraEncryptedSha256") == sha
        ]
        if len(matches) > 1:
            raise DriveBackupRejected("Duplicate remote archive identity: review required")
        if matches:
            remote_id = self.drive._check_id(matches[0]["id"])
            reused = True
        else:
            remote_id = self._upload_new(archive, sha, size)
            reused = False
        self._identity(self._file_metadata(remote_id), archive.name, sha, md5, size)
        self._readback(remote_id, sha, size)
        return {
            "status": "verified_existing" if reused else "uploaded_verified",
            "remote_file_id": remote_id, "archive_sha256": sha,
            "encrypted_bytes": size,
        }


def backup_once(
    database: Path, backup_key: Path, scratch_root: Path, remote: EncryptedDriveBackups,
) -> dict:
    """Encrypted Drive backup from live WAL-consistent SQLite; no plaintext export."""
    _regular_file(database, private=False)
    root = _private_directory(scratch_root)
    # Snapshot + sealed artifact + independent local authenticated unseal
    # proof. Refuse if all three won't fit on the staging filesystem.
    required = max(128 * 1024 * 1024, 3 * database.stat().st_size + 64 * 1024 * 1024)
    free = shutil.disk_usage(root).free
    if free < required:
        raise DriveBackupRejected("Insufficient private scratch disk space for safe backup")
    # Fail on inaccessible Drive destination BEFORE reading the live database.
    remote.probe()
    with tempfile.TemporaryDirectory(prefix="readera-offhost-", dir=root) as temp:
        top = Path(temp)
        a, b, c = (top / name for name in ("snapshots", "encrypted", "recovery"))
        for directory in (a, b, c):
            directory.mkdir(mode=0o700)
        manifest = snapshot(database, a)
        sealed = seal(manifest, backup_key, b)
        # Authenticate the whole AES-GCM stream AND standalone SQLite before
        # uploading anything. The restored data is erased with the private temp.
        unseal(sealed, backup_key, c / "readera-restored.sqlite3")
        return remote.upload(sealed)


def _credentials() -> tuple[str, str, str]:
    values = tuple(
        _read_systemd_credential(key, env, "").strip()
        for key, env in (
            ("google_drive_client_id", "CHAPTERA_DRIVE_CLIENT_ID"),
            ("google_drive_client_secret", "CHAPTERA_DRIVE_CLIENT_SECRET"),
            ("google_drive_refresh_token", "CHAPTERA_DRIVE_REFRESH_TOKEN"),
        )
    )
    if not all(values):
        raise DriveBackupRejected("Private Google Drive OAuth is not configured")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ReadEra encrypted Google Drive backup")
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--probe-folder", action="store_true", help="Check OAuth/folder access, no writes")
    commands.add_argument("--run-once", action="store_true", help="Create verified encrypted off-host copy")
    parser.add_argument("--allow-upload", action="store_true", help="Explicitly authorize encrypted Drive writes")
    parser.add_argument("--folder-id", default=os.environ.get(_FOLDER_ENV, ""))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--scratch-root", type=Path)
    args = parser.parse_args(argv)
    if args.run_once and (not args.allow_upload or not args.database
                          or not args.key_file or not args.scratch_root):
        parser.error("Backup requires --allow-upload, --database, --key-file, --scratch-root")
    if args.probe_folder and args.allow_upload:
        parser.error("A read-only folder probe cannot authorize upload")
    try:
        with httpx.Client(timeout=60.0, trust_env=False, follow_redirects=False) as http:
            drive = GoogleDriveClient(
                client_id=_credentials()[0], client_secret=_credentials()[1],
                refresh_token=_credentials()[2], http=http,
            )
            remote = EncryptedDriveBackups(drive, args.folder_id)
            if args.probe_folder:
                remote.probe()
                print("drive_backup_folder_access=PASS")
            else:
                receipt = backup_once(args.database, args.key_file, args.scratch_root, remote)
                print("drive_encrypted_backup=PASS")
                print("remote_file_id=" + receipt["remote_file_id"])
                print("encrypted_sha256=" + receipt["archive_sha256"])
                print("encrypted_bytes=" + str(receipt["encrypted_bytes"]))
        return 0
    except (BackupRejected, DriveExportError, OSError, sqlite3.Error, ValueError, TypeError):
        # Avoid printing credential details, URLs, filenames, account or book text.
        print("drive_encrypted_backup=REJECTED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
