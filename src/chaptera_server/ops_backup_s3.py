"""Write-only-by-default off-host transport for ReadEra encrypted .rbe bundles.

Only ciphertext is uploaded. No raw SQLite, password, key-file, or manifest
can be selected. S3 object keys are deterministic and namespaced per vault;
conditional writes forbid overwriting existing recovery points. The upload
gate reads back and SHA-256 checks ALL remote ciphertext before returning PASS.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit

from .ops_backup import (
    BackupRejected, _CHUNK, _private_directory, _publish_new, _regular_file,
    _sha_and_size, _sync_directory, _temporary,
)
from .ops_backup_crypto import _OUTER, _MAGIC

_FORMAT = "readera-rbe-v1"
_PREFIX = "readera-backup-v1"
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_VAULT = re.compile(r"[a-z0-9][a-z0-9-]{1,62}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MULTIPART_BYTES = 8 * 1024 * 1024  # 8 MiB, always at least S3's 5-MiB part minimum.
_MAX_PARTS = 10_000
_MAX_BYTES = _MULTIPART_BYTES * _MAX_PARTS
_READ_BYTES = 1024 * 1024


class OffhostRejected(BackupRejected):
    """Invalid inputs, external data, missing integrity proof, or overwrite."""


def _name(name: str) -> str:
    if not isinstance(name, str) or not _OUTER.fullmatch(name):
        raise OffhostRejected("Expected a single ReadEra encrypted bundle name")
    return name


def _metadata(record: dict) -> tuple[int, str]:
    try:
        size = record["ContentLength"]
        meta = record["Metadata"]
        checksum, format_name = meta["sha256"], meta["format"]
    except (KeyError, TypeError) as exc:
        raise OffhostRejected("Remote backup has no integrity metadata") from exc
    if (type(size) is not int or not 64 <= size <= _MAX_BYTES
            or not isinstance(checksum, str) or not _HEX.fullmatch(checksum)
            or format_name != _FORMAT):
        raise OffhostRejected("Remote backup format, length or hash invalid")
    return size, checksum


def _archive(path: str | Path) -> tuple[Path, int, str]:
    archive = Path(path)
    _name(archive.name)
    _regular_file(archive, private=True)
    if archive.stat().st_uid != os.geteuid():
        raise OffhostRejected("Archive must belong to the invoking user")
    # Do not accidentally upload cleartext SQLite or arbitrary fake .rbe files.
    with archive.open("rb") as stream:
        if stream.read(len(_MAGIC)) != _MAGIC:
            raise OffhostRejected("Encrypted bundle magic invalid")
    checksum, length = _sha_and_size(archive)
    if not 64 <= length <= _MAX_BYTES:
        raise OffhostRejected("Encrypted archive out of allowed size range")
    return archive, length, checksum


class S3BackupTransport:
    def __init__(self, client, *, bucket: str, vault: str):
        if (not isinstance(bucket, str) or not _BUCKET.fullmatch(bucket)
                or ".." in bucket or ".-" in bucket or "-." in bucket):
            raise OffhostRejected("S3 bucket identifier invalid")
        if not isinstance(vault, str) or not _VAULT.fullmatch(vault):
            raise OffhostRejected("Vault namespace invalid")
        self.client = client
        self.bucket = bucket
        self.vault = vault

    def key_for(self, name: str) -> str:
        return f"{_PREFIX}/{self.vault}/{_name(name)}"

    def _head(self, key: str) -> tuple[int, str]:
        record = self.client.head_object(Bucket=self.bucket, Key=key)
        return _metadata(record)

    def check_remote(self, name: str, *, expected: tuple[int, str] | None = None) -> dict:
        """Verify full remote bytes, not ETag (multipart ETags are not SHA256)."""
        key = self.key_for(name)
        size, checksum = self._head(key)
        if expected is not None and expected != (size, checksum):
            raise OffhostRejected("Remote object is not the uploaded snapshot")
        fetched = self.client.get_object(Bucket=self.bucket, Key=key)
        body = fetched["Body"]
        digest = hashlib.sha256()
        received = 0
        try:
            # Both HEAD and GET must agree; do not trust client-supplied ETags.
            if fetched.get("ContentLength") != size:
                raise OffhostRejected("S3 object size changed during readback")
            while block := body.read(_READ_BYTES):
                received += len(block)
                if received > size:
                    raise OffhostRejected("S3 object exceeded expected byte count")
                digest.update(block)
            if received != size or digest.hexdigest() != checksum:
                raise OffhostRejected("Remote object checksum mismatch")
        finally:
            body.close()
        return {"file": name, "bytes": size, "sha256": checksum, "verified": True}

    def push(self, source: str | Path) -> dict:
        """Create only (If-None-Match:*), then stream-readback full ciphertext."""
        path, size, digest = _archive(source)
        key = self.key_for(path.name)
        metadata = {"sha256": digest, "format": _FORMAT}
        if size <= _MULTIPART_BYTES:
            with path.open("rb") as inp:
                self.client.put_object(
                    Bucket=self.bucket, Key=key, Body=inp, ContentLength=size,
                    ContentType="application/octet-stream", Metadata=metadata,
                    IfNoneMatch="*",
                )
        else:
            response = self.client.create_multipart_upload(
                Bucket=self.bucket, Key=key, ContentType="application/octet-stream",
                Metadata=metadata,
            )
            upload_id = response["UploadId"]
            completed = False
            try:
                parts = []
                with path.open("rb") as inp:
                    for part_number in range(1, _MAX_PARTS + 1):
                        block = inp.read(_MULTIPART_BYTES)
                        if not block:
                            break
                        result = self.client.upload_part(
                            Bucket=self.bucket, Key=key, UploadId=upload_id,
                            PartNumber=part_number, Body=block, ContentLength=len(block),
                        )
                        parts.append({"PartNumber": part_number, "ETag": result["ETag"]})
                if not parts or len(parts) > _MAX_PARTS:
                    raise OffhostRejected("Multipart transfer contained no valid parts")
                self.client.complete_multipart_upload(
                    Bucket=self.bucket, Key=key, UploadId=upload_id,
                    MultipartUpload={"Parts": parts}, IfNoneMatch="*",
                )
                completed = True
            finally:
                if not completed:
                    # Never delete a completed recovery point even after failure.
                    try:
                        self.client.abort_multipart_upload(
                            Bucket=self.bucket, Key=key, UploadId=upload_id
                        )
                    except Exception:
                        pass
        # Archive can change while being read by boto3: recheck locally before
        # remotely attesting it. A failed assertion is NOT success.
        after_sha, after_size = _sha_and_size(path)
        if (after_size, after_sha) != (size, digest):
            raise OffhostRejected("Local encrypted source changed during transfer")
        return self.check_remote(path.name, expected=(size, digest))

    def pull(self, name: str, directory: str | Path) -> Path:
        """Download ONLY encrypted bytes into a new private file; unseal separately."""
        file_name = _name(name)
        target_folder = _private_directory(Path(directory))
        target = target_folder / file_name
        if target.exists() or target.is_symlink():
            raise OffhostRejected("Encrypted download target already exists")
        key = self.key_for(file_name)
        size, checksum = self._head(key)
        received = 0
        digest = hashlib.sha256()
        tmp = _temporary(target_folder, ".rbe")
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=key)
            body = result["Body"]
            try:
                if result.get("ContentLength") != size:
                    raise OffhostRejected("S3 download changed its length")
                with tmp.open("wb") as out:
                    while block := body.read(_READ_BYTES):
                        received += len(block)
                        if received > size:
                            raise OffhostRejected("Remote ciphertext exceeds expected size")
                        digest.update(block)
                        out.write(block)
                    out.flush()
                    os.fsync(out.fileno())
            finally:
                body.close()
            if received != size or digest.hexdigest() != checksum:
                raise OffhostRejected("Downloaded encrypted bundle checksum mismatch")
            # The name was validated earlier; tmp has a random private name.
            with tmp.open("rb") as stream:
                if stream.read(len(_MAGIC)) != _MAGIC:
                    raise OffhostRejected("Downloaded bytes are not ReadEra ciphertext")
            _publish_new(tmp, target)
            _sync_directory(target_folder)
            return target
        finally:
            tmp.unlink(missing_ok=True)


def _validate_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise OffhostRejected("Custom S3 endpoint must be HTTPS and contain no credentials")
    return endpoint


def _client(region: str, endpoint: str | None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}\Z", region):
        raise OffhostRejected("Explicit AWS-compatible region required")
    validated = _validate_endpoint(endpoint)
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise OffhostRejected("Install optional 'offsite-s3' dependencies") from exc
    # Credential resolution is boto3's standard protected chain, never CLI args.
    client = boto3.client(
        "s3", region_name=region, endpoint_url=validated,
        config=Config(signature_version="s3v4",
                      s3={"addressing_style": "path"},
                      connect_timeout=7, read_timeout=45,
                      retries={"mode": "standard", "max_attempts":3}),
    )
    # Boto3 also resolves AWS_ENDPOINT_URL_S3 from the environment. It can
    # silently override the no-custom-endpoint case. Reject plaintext even
    # when that configuration was supplied outside of our CLI.
    _validate_endpoint(client.meta.endpoint_url)
    return client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ReadEra encrypted S3 backup transport")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--endpoint", help="HTTPS-only custom S3/MinIO endpoint")
    sub = parser.add_subparsers(dest="action", required=True)
    push = sub.add_parser("push", help="Upload ciphertext once, read back and verify")
    push.add_argument("--archive", required=True, type=Path)
    check = sub.add_parser("check", help="Re-download and SHA256 verify remote ciphertext")
    check.add_argument("--name", required=True)
    pull = sub.add_parser("pull", help="Download only ciphertext into new private file")
    pull.add_argument("--name", required=True)
    pull.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        store = S3BackupTransport(_client(args.region, args.endpoint),
                                  bucket=args.bucket, vault=args.vault)
        if args.action == "push":
            receipt = store.push(args.archive)
        elif args.action == "check":
            receipt = store.check_remote(args.name)
        else:
            archive = store.pull(args.name, args.directory)
            receipt = {"file": archive.name, "verified": True}
        print("offsite_s3=PASS")
        print("object_file=" + receipt["file"])
        return 0
    except (OffhostRejected, BackupRejected, ValueError, OSError, KeyError):
        print("offsite_s3=REJECTED", file=sys.stderr)
        return 2
    except Exception:
        # Boto3 errors can contain endpoints, credentials or request headers.
        # Do not print remote exception details (including tokens/bucket names).
        print("offsite_s3=REMOTE_FAILURE", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
