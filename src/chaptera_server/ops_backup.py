"""Offline-operable, WAL-consistent backups of the private ReadEra SQLite state.

Uses SQLite's online backup API: copying jobs.sqlite3 directly ignores WAL.
Never overwrites a live database. Backups contain private book contents and
credential hashes; the destination must be an operator-owned mode-0700 folder
and must be copied to encrypted off-host storage by the operator.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
from urllib.parse import quote

SCHEMA = "readera.sqlite-snapshot.v1"
_FILE = re.compile(r"readera-\d{8}T\d{6}Z-[0-9a-f]{16}\.sqlite3\Z")
_HEX64 = re.compile(r"[a-f0-9]{64}\Z")
_CHUNK = 1024 * 1024
_MAX_MANIFEST = 4096


class BackupRejected(ValueError):
    """Unsafe path, mismatched backup, corrupt state or attempted overwrite."""


def _private_directory(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise BackupRejected("Backup/restore directory must exist and not be a symlink")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
        raise BackupRejected("Backup/restore directory must not be accessible to group/others")
    if metadata.st_uid != os.geteuid():
        raise BackupRejected("Backup/restore directory must belong to the invoking user")
    return resolved


def _regular_file(path: Path, *, private: bool) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise BackupRejected("Input must be an ordinary file without symlinks")
    if private and info.st_mode & 0o077:
        raise BackupRejected("Backup files must be owner-only (0600)")


def _read_only(database: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=15)


def _healthy(path: Path) -> None:
    with _read_only(path) as con:
        if [row[0] for row in con.execute("PRAGMA integrity_check")] != ["ok"]:
            raise BackupRejected("SQLite integrity_check failed")
        con.execute("PRAGMA foreign_keys=ON")
        if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupRejected("SQLite foreign_key_check failed")


def _sha_and_size(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        while block := stream.read(_CHUNK):
            length += len(block)
            hasher.update(block)
    return hasher.hexdigest(), length


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _temporary(directory: Path, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=".readera-incomplete-", suffix=suffix,
                                 dir=directory)
    os.close(fd)
    return Path(name)


def _publish_new(incomplete: Path, destination: Path) -> None:
    # Hard-link creates only when destination is absent: never replace an
    # existing verified snapshot even under a race.
    os.link(incomplete, destination, follow_symlinks=False)
    incomplete.unlink()


def snapshot(database: str | Path, directory: str | Path) -> Path:
    """Return the manifest pathname. Source may have active WAL writers."""
    source = Path(database)
    _regular_file(source, private=False)
    folder = _private_directory(Path(directory))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    basename = f"readera-{timestamp}-{os.urandom(8).hex()}.sqlite3"
    target = folder / basename
    manifest = folder / (basename + ".manifest.json")
    incomplete = _temporary(folder, ".sqlite3")
    unfinished_manifest = None
    published = False
    try:
        with _read_only(source.resolve(strict=True)) as source_db:
            with sqlite3.connect(str(incomplete), timeout=15) as destination_db:
                source_db.backup(destination_db, pages=128, sleep=0.05)
        _healthy(incomplete)
        digest, size = _sha_and_size(incomplete)
        if size < 512:
            raise BackupRejected("Snapshot is too small to contain a SQLite database")
        with incomplete.open("rb") as stream:
            os.fsync(stream.fileno())
        payload = {"schema": SCHEMA, "file": basename, "bytes": size,
                   "sha256": digest, "created_utc": timestamp}
        unfinished_manifest = _temporary(folder, ".manifest.json")
        with unfinished_manifest.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _publish_new(incomplete, target)
        published = True
        _publish_new(unfinished_manifest, manifest)
        _sync_directory(folder)
        return manifest
    except BaseException:
        if published:
            target.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
        raise
    finally:
        incomplete.unlink(missing_ok=True)
        if unfinished_manifest is not None:
            unfinished_manifest.unlink(missing_ok=True)


def verify(manifest_path: str | Path) -> dict:
    """Validate checksum and complete SQLite consistency before returning metadata."""
    manifest = Path(manifest_path)
    folder = _private_directory(manifest.parent)
    manifest = folder / manifest.name
    _regular_file(manifest, private=True)
    if manifest.stat().st_size > _MAX_MANIFEST:
        raise BackupRejected("Invalid manifest size")
    with manifest.open("r", encoding="utf-8") as stream:
        info = json.load(stream)
    if not isinstance(info, dict) or set(info) != {
        "schema", "file", "bytes", "sha256", "created_utc"
    }:
        raise BackupRejected("Invalid backup manifest fields")
    filename = info["file"]
    if (info["schema"] != SCHEMA or not isinstance(filename, str)
            or not _FILE.fullmatch(filename)
            or manifest.name != filename + ".manifest.json"):
        raise BackupRejected("Manifest/file identity mismatch")
    expected = info["sha256"]
    if not isinstance(expected, str) or not _HEX64.fullmatch(expected):
        raise BackupRejected("Invalid backup digest")
    size = info["bytes"]
    if type(size) is not int or size < 512:
        raise BackupRejected("Invalid backup byte count")
    data = folder / filename
    _regular_file(data, private=True)
    if data.stat().st_size != size:
        raise BackupRejected("Backup byte count mismatch")
    current_sha, actual_size = _sha_and_size(data)
    if actual_size != size or current_sha != expected:
        raise BackupRejected("Backup SHA256 mismatch")
    _healthy(data)
    return {"schema": SCHEMA, "file": filename, "bytes": size, "sha256": expected,
            "created_utc": info["created_utc"]}


def restore(manifest_path: str | Path, destination: str | Path) -> Path:
    """Restore to a NEW, offline path only; never replaces a live SQLite DB."""
    info = verify(manifest_path)
    source = Path(manifest_path).parent / info["file"]
    target = Path(destination)
    if not target.name or target.name in {".", ".."}:
        raise BackupRejected("A destination SQLite filename is required")
    folder = _private_directory(target.parent)
    target = folder / target.name
    if (target.exists() or target.is_symlink()
            or (folder / (target.name + "-wal")).exists()
            or (folder / (target.name + "-wal")).is_symlink()
            or (folder / (target.name + "-shm")).exists()
            or (folder / (target.name + "-shm")).is_symlink()):
        raise BackupRejected("Destination or SQLite sidecar already exists")
    if source.resolve(strict=True) == target.resolve(strict=False):
        raise BackupRejected("Cannot restore over snapshot source")
    incomplete = _temporary(folder, ".sqlite3")
    try:
        digest = hashlib.sha256()
        written = 0
        with source.open("rb") as inp, incomplete.open("wb") as out:
            while data := inp.read(_CHUNK):
                out.write(data)
                digest.update(data)
                written += len(data)
            out.flush()
            os.fsync(out.fileno())
        if written != info["bytes"] or digest.hexdigest() != info["sha256"]:
            raise BackupRejected("Snapshot changed while restoring")
        _healthy(incomplete)
        _publish_new(incomplete, target)
        _sync_directory(folder)
        return target
    finally:
        incomplete.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ReadEra SQLite offline recovery tools")
    actions = parser.add_subparsers(dest="command", required=True)
    create = actions.add_parser("snapshot", help="WAL-consistent SQLite online snapshot")
    create.add_argument("--database", required=True, type=Path)
    create.add_argument("--directory", required=True, type=Path)
    check = actions.add_parser("verify", help="Check manifest, SHA256 and SQLite integrity")
    check.add_argument("--manifest", required=True, type=Path)
    recover = actions.add_parser("restore", help="Restore to a nonexistent, OFFLINE file")
    recover.add_argument("--manifest", required=True, type=Path)
    recover.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = snapshot(args.database, args.directory)
            print("snapshot=PASS")
            print("manifest=" + result.name)
        elif args.command == "verify":
            result = verify(args.manifest)
            print("verify=PASS")
            print("sha256=" + result["sha256"])
        else:
            result = restore(args.manifest, args.destination)
            print("restore=PASS")
            print("database=" + result.name)
        return 0
    except (BackupRejected, sqlite3.Error, OSError, ValueError, TypeError):
        # No file paths or private database content appear in command logs.
        print("readera_backup=REJECTED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
