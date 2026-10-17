"""Stream an authenticated, off-host-ready encrypted ReadEra SQLite backup.

Private operator-driven workflow only: the key is a raw, 32-byte, mode-0600
file (never a command-line value), the output is an AES-256-GCM sealed bundle,
and decryption may publish only a NEW offline SQLite file.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .ops_backup import (
    BackupRejected, SCHEMA as SNAPSHOT_SCHEMA, _CHUNK, _FILE, _HEX64,
    _healthy, _private_directory, _publish_new, _regular_file,
    _sync_directory, _temporary, verify,
)

SEALED_SCHEMA = "readera.encrypted-sqlite.v1"
_MAGIC = b"READERA-AES256-GCM-V1\x00"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_MAX_METADATA = 4096
_OUTER = re.compile(r"readera-\d{8}T\d{6}Z-[0-9a-f]{16}\.sqlite3\.rbe\Z")


def _read_key(path: str | Path) -> bytes:
    source = Path(path)
    _regular_file(source, private=True)
    if source.stat().st_uid not in {os.geteuid(), 0}:
        raise BackupRejected("Encryption key must be owned by the operator or root")
    with source.open("rb") as stream:
        key = stream.read(33)
    if len(key) != 32:
        raise BackupRejected("Encryption key must contain exactly 32 random bytes")
    return key


def generate_key(path: str | Path) -> Path:
    """One-time key provisioning; exclusive create, no stdout secret."""
    location = Path(path)
    folder = _private_directory(location.parent)
    destination = folder / location.name
    if destination.name in {"", ".", ".."}:
        raise BackupRejected("Key filename required")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(os.urandom(32))
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(folder)
        return destination
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _manifest_metadata(info: dict) -> bytes:
    data = json.dumps(
        {"schema": SEALED_SCHEMA, "snapshot": info},
        sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    ).encode("ascii")
    if not 2 <= len(data) <= _MAX_METADATA:
        raise BackupRejected("Snapshot metadata too large")
    return len(data).to_bytes(4, "big") + data


def _decode_metadata(blob: bytes) -> dict:
    try:
        info = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupRejected("Encrypted snapshot metadata invalid") from exc
    if not isinstance(info, dict) or set(info) != {"schema", "snapshot"}:
        raise BackupRejected("Encrypted snapshot structure invalid")
    if info["schema"] != SEALED_SCHEMA:
        raise BackupRejected("Unsupported encrypted snapshot version")
    snap = info["snapshot"]
    if not isinstance(snap, dict) or set(snap) != {
        "schema", "file", "bytes", "sha256", "created_utc"
    }:
        raise BackupRejected("Encrypted snapshot manifest invalid")
    if (snap["schema"] != SNAPSHOT_SCHEMA
            or not isinstance(snap["file"], str) or not _FILE.fullmatch(snap["file"])
            or not isinstance(snap["sha256"], str)
            or not _HEX64.fullmatch(snap["sha256"])
            or type(snap["bytes"]) is not int or snap["bytes"] < 512
            or not isinstance(snap["created_utc"], str)
            or snap["created_utc"] != snap["file"][8:24]):
        raise BackupRejected("Encrypted snapshot identity invalid")
    return snap


def seal(manifest_path: str | Path, key_path: str | Path,
         directory: str | Path) -> Path:
    """Encrypt a VERIFIED SQLite snapshot into a new authenticated bundle."""
    info = verify(manifest_path)
    key = _read_key(key_path)
    source_folder = _private_directory(Path(manifest_path).parent)
    folder = _private_directory(Path(directory))
    if folder == source_folder:
        raise BackupRejected("Encrypted bundles require a separate private directory")
    source = source_folder / info["file"]
    _regular_file(source, private=True)
    destination = folder / (info["file"] + ".rbe")
    if not _OUTER.fullmatch(destination.name):
        raise BackupRejected("Invalid encrypted bundle name")
    incomplete = _temporary(folder, ".rbe")
    published = False
    try:
        nonce = os.urandom(_NONCE_BYTES)
        header = _MAGIC + nonce
        encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(header)
        metadata = _manifest_metadata(info)
        digest = hashlib.sha256()
        size = 0
        with incomplete.open("wb") as out, source.open("rb") as inp:
            out.write(header)
            out.write(encryptor.update(metadata))
            while block := inp.read(_CHUNK):
                out.write(encryptor.update(block))
                digest.update(block)
                size += len(block)
            if size != info["bytes"] or digest.hexdigest() != info["sha256"]:
                raise BackupRejected("SQLite source changed during sealing")
            out.write(encryptor.finalize())
            out.write(encryptor.tag)
            out.flush()
            os.fsync(out.fileno())
        _publish_new(incomplete, destination)
        published = True
        _sync_directory(folder)
        return destination
    except BaseException:
        if published:
            destination.unlink(missing_ok=True)
        raise
    finally:
        incomplete.unlink(missing_ok=True)


def unseal(archive_path: str | Path, key_path: str | Path,
           destination: str | Path) -> Path:
    """Authenticate all bytes before publishing a NEW, verified offline DB."""
    archive = Path(archive_path)
    _regular_file(archive, private=True)
    key = _read_key(key_path)
    path = Path(destination)
    if path.name in {"", ".", ".."}:
        raise BackupRejected("Offline SQLite filename required")
    folder = _private_directory(path.parent)
    target = folder / path.name
    if (target.exists() or target.is_symlink()
            or (folder / (target.name + "-wal")).exists()
            or (folder / (target.name + "-wal")).is_symlink()
            or (folder / (target.name + "-shm")).exists()
            or (folder / (target.name + "-shm")).is_symlink()):
        raise BackupRejected("Restored SQLite target must not already exist")
    length = archive.stat().st_size
    overhead = len(_MAGIC) + _NONCE_BYTES + _TAG_BYTES
    if length < overhead + 4 + 2 + 512:
        raise BackupRejected("Encrypted backup too short")
    incomplete = _temporary(folder, ".sqlite3")
    published = False
    try:
        with archive.open("rb") as inp:
            header = inp.read(len(_MAGIC) + _NONCE_BYTES)
            if not header.startswith(_MAGIC) or len(header) != len(_MAGIC) + _NONCE_BYTES:
                raise BackupRejected("Encrypted backup format invalid")
            nonce = header[len(_MAGIC):]
            inp.seek(-_TAG_BYTES, os.SEEK_END)
            tag = inp.read(_TAG_BYTES)
            inp.seek(len(header), os.SEEK_SET)
            remaining = length - overhead
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(header)
            pending = bytearray()
            metadata_length = None
            expected = None
            digest = hashlib.sha256()
            written = 0
            with incomplete.open("wb") as out:
                while remaining:
                    block = inp.read(min(remaining, _CHUNK))
                    if not block:
                        raise BackupRejected("Truncated encrypted bundle")
                    remaining -= len(block)
                    plain = decryptor.update(block)
                    if expected is None:
                        pending.extend(plain)
                        if metadata_length is None and len(pending) >= 4:
                            metadata_length = int.from_bytes(pending[:4], "big")
                            if not 2 <= metadata_length <= _MAX_METADATA:
                                raise BackupRejected("Encrypted metadata length invalid")
                        if metadata_length is None or len(pending) < 4 + metadata_length:
                            if len(pending) > 4 + _MAX_METADATA + _CHUNK:
                                raise BackupRejected("Encrypted metadata overflow")
                            continue
                        expected = _decode_metadata(
                            bytes(pending[4:4 + metadata_length])
                        )
                        plain = bytes(pending[4 + metadata_length:])
                        pending.clear()
                    if plain:
                        written += len(plain)
                        if written > expected["bytes"]:
                            raise BackupRejected("Encrypted snapshot exceeds recorded length")
                        digest.update(plain)
                        out.write(plain)
                try:
                    decryptor.finalize()
                except InvalidTag as exc:
                    raise BackupRejected("Encrypted backup authentication failed") from exc
                if expected is None or written != expected["bytes"]:
                    raise BackupRejected("Encrypted SQLite byte count mismatch")
                if digest.hexdigest() != expected["sha256"]:
                    raise BackupRejected("Encrypted SQLite checksum mismatch")
                out.flush()
                os.fsync(out.fileno())
        _healthy(incomplete)
        _publish_new(incomplete, target)
        published = True
        _sync_directory(folder)
        return target
    except BaseException:
        if published:
            target.unlink(missing_ok=True)
        raise
    finally:
        incomplete.unlink(missing_ok=True)
        (folder / (incomplete.name + "-wal")).unlink(missing_ok=True)
        (folder / (incomplete.name + "-shm")).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ReadEra private encrypted SQLite bundles")
    sub = parser.add_subparsers(dest="command", required=True)
    key = sub.add_parser("generate-key", help="Create new raw 32-byte key file, 0600")
    key.add_argument("--path", type=Path, required=True)
    pack = sub.add_parser("seal", help="Encrypt verified snapshot for off-host transfer")
    pack.add_argument("--manifest", type=Path, required=True)
    pack.add_argument("--key-file", type=Path, required=True)
    pack.add_argument("--directory", type=Path, required=True)
    unpack = sub.add_parser("unseal", help="Restore encrypted bundle to NEW offline file")
    unpack.add_argument("--archive", type=Path, required=True)
    unpack.add_argument("--key-file", type=Path, required=True)
    unpack.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate-key":
            result = generate_key(args.path)
            print("key_generated=PASS")
            print("key_file=" + result.name)
        elif args.command == "seal":
            result = seal(args.manifest, args.key_file, args.directory)
            print("seal=PASS")
            print("bundle=" + result.name)
        else:
            result = unseal(args.archive, args.key_file, args.destination)
            print("unseal=PASS")
            print("database=" + result.name)
        return 0
    except (BackupRejected, OSError, sqlite3.Error, ValueError, TypeError):
        # Never log secret path, key, content, or user-supplied archive bytes.
        print("readera_encrypted_backup=REJECTED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
