"""RE-08 offsite S3: AWS SDK semantics, integrity and full recovery."""
from __future__ import annotations

import io
import os
from pathlib import Path
import sqlite3

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws
import pytest

from chaptera_server.ops_backup import BackupRejected, snapshot
from chaptera_server.ops_backup_crypto import generate_key, seal, unseal
from chaptera_server.ops_backup_s3 import (
    OffhostRejected, S3BackupTransport, _client, _validate_endpoint,
)


def private(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir(mode=0o700)
    return folder


def encrypted_archive(tmp_path, *, big=False):
    db = tmp_path / "data.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE chapters(book TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        payload = os.urandom(9 * 1024 * 1024) if big else b"important private book text"
        con.execute("INSERT INTO chapters VALUES (?,?)", ("book", payload))
    local = private(tmp_path, "snapshots")
    key_folder = private(tmp_path, "keys")
    sealed_folder = private(tmp_path, "sealed")
    recovery = private(tmp_path, "recovery")
    downloaded = private(tmp_path, "downloaded")
    key = generate_key(key_folder / "master.key")
    manifest = snapshot(db, local)
    archive = seal(manifest, key, sealed_folder)
    return archive, key, recovery, downloaded, payload


def s3_and_store(vault="tenant-01"):
    # Moto intercepts the real signed boto3 S3 API; no external S3 contact.
    sdk = boto3.client("s3", region_name="us-east-1",
                       aws_access_key_id="test-only", aws_secret_access_key="test-only")
    sdk.create_bucket(Bucket="readera-backups-private")
    return sdk, S3BackupTransport(sdk, bucket="readera-backups-private", vault=vault)


@mock_aws
def test_remote_roundtrip_receipt_and_recovered_original(tmp_path):
    archive, master, recovery, downloaded, payload = encrypted_archive(tmp_path)
    _sdk, store = s3_and_store()
    pushed = store.push(archive)
    assert pushed["verified"] is True
    assert pushed["file"] == archive.name
    assert pushed["bytes"] == archive.stat().st_size
    assert len(pushed["sha256"]) == 64
    assert store.check_remote(archive.name) == pushed
    remote_archive = store.pull(archive.name, downloaded)
    assert remote_archive.read_bytes() == archive.read_bytes()
    restored = unseal(remote_archive, master, recovery / "safe.sqlite3")
    with sqlite3.connect(restored) as con:
        assert con.execute("SELECT payload FROM chapters").fetchone()[0] == payload
    with pytest.raises((ClientError, OffhostRejected)):
        store.push(archive)  # must not overwrite existing recovery point
    with pytest.raises(OffhostRejected):
        store.pull(archive.name, downloaded)  # must not overwrite local copy


@mock_aws
def test_multipart_10mib_and_conditional_write(tmp_path):
    archive, key, recovery, downloaded, payload = encrypted_archive(tmp_path, big=True)
    sdk, store = s3_and_store()
    pushed = store.push(archive)
    assert pushed["verified"] and pushed["bytes"] > 8 * 1024 * 1024
    remote = sdk.head_object(Bucket=store.bucket, Key=store.key_for(archive.name))
    assert remote["ContentLength"] == pushed["bytes"]
    assert remote["Metadata"]["sha256"] == pushed["sha256"]
    assert store.pull(archive.name, downloaded).is_file()
    restored = unseal(downloaded / archive.name, key, recovery / "retrieved.sqlite3")
    with sqlite3.connect(restored) as con:
        assert con.execute("SELECT payload FROM chapters").fetchone()[0] == payload
    with pytest.raises((ClientError, OffhostRejected)):
        store.push(archive)


@mock_aws
def test_changed_remote_ciphertext_must_fail_readback_and_download(tmp_path):
    archive, key, recovery, downloaded, payload = encrypted_archive(tmp_path)
    sdk, store = s3_and_store()
    good = store.push(archive)
    objkey = store.key_for(archive.name)
    corrupted = bytearray(archive.read_bytes())
    corrupted[-1] ^= 1
    sdk.put_object(
        Bucket=store.bucket, Key=objkey, Body=bytes(corrupted),
        Metadata={"sha256": good["sha256"], "format":"readera-rbe-v1"},
    )
    with pytest.raises(OffhostRejected):
        store.check_remote(archive.name)
    with pytest.raises(OffhostRejected):
        store.pull(archive.name, downloaded)
    assert list(downloaded.iterdir()) == []


@mock_aws
def test_remote_metadata_can_be_forged_but_aes_tag_still_rejects(tmp_path):
    archive, key, recovery, downloaded, _payload = encrypted_archive(tmp_path)
    sdk, store = s3_and_store()
    receipt = store.push(archive)
    corrupted = bytearray(archive.read_bytes())
    corrupted[-1] ^= 1
    import hashlib
    sdk.put_object(
        Bucket=store.bucket, Key=store.key_for(archive.name),
        Body=bytes(corrupted), Metadata={
            "sha256": hashlib.sha256(corrupted).hexdigest(),
            "format": "readera-rbe-v1",
        },
    )
    remote = store.pull(archive.name, downloaded)
    with pytest.raises(BackupRejected):
        unseal(remote, key, recovery / "must-not-exist.sqlite3")
    assert list(recovery.iterdir()) == []


def test_strict_namespace_tls_and_no_insecure_custom_endpoint():
    with pytest.raises(OffhostRejected):
        S3BackupTransport(None, bucket="bad/bucket", vault="private")
    with pytest.raises(OffhostRejected):
        S3BackupTransport(None, bucket="valid-backups", vault="../steal")
    with pytest.raises(OffhostRejected):
        S3BackupTransport(None, bucket="valid-backups", vault="x")
    store = S3BackupTransport(None, bucket="valid-backups", vault="vault-001")
    assert store.key_for("readera-20261018T060000Z-aaaaaaaaaaaaaaaa.sqlite3.rbe") == (
        "readera-backup-v1/vault-001/"
        "readera-20261018T060000Z-aaaaaaaaaaaaaaaa.sqlite3.rbe"
    )
    for endpoint in [
        "http://s3.example.com", "http://127.0.0.1:9000",
        "https://u:p@s3.example.org", "https://s3.example.org/a",
        "https://s3.example.org/?key=1", "ftp://storage.example.org",
    ]:
        with pytest.raises(OffhostRejected):
            _validate_endpoint(endpoint)
    assert _validate_endpoint("https://s3.example.org") == "https://s3.example.org"
    assert _validate_endpoint(None) is None


def test_local_upload_requires_gcm_magic_owner_only_and_safe_basename(tmp_path):
    archive, key, recovery, downloaded, _payload = encrypted_archive(tmp_path)
    store = S3BackupTransport(None, bucket="valid-backups", vault="vault-001")
    archive.chmod(0o644)
    with pytest.raises(BackupRejected):
        store.push(archive)
    archive.chmod(0o600)
    evil = archive.with_name("not-recovered.sqlite3.rbe")
    evil.write_bytes(archive.read_bytes())
    with pytest.raises(OffhostRejected):
        store.push(evil)
    evil.unlink()
    broken = archive.with_name("readera-20261018T060000Z-aaaaaaaaaaaaaaaa.sqlite3.rbe")
    broken.write_bytes(b"not an encrypted format")
    broken.chmod(0o600)
    with pytest.raises(OffhostRejected):
        store.push(broken)
    with pytest.raises(OffhostRejected):
        store.pull("../../secrets", downloaded)


@mock_aws
def test_wrong_vault_cannot_read_or_overwrite_another_tenant(tmp_path):
    archive, key, recovery, downloaded, _payload = encrypted_archive(tmp_path)
    sdk, store = s3_and_store("workspace-a")
    store.push(archive)
    other = S3BackupTransport(sdk, bucket=store.bucket, vault="workspace-b")
    with pytest.raises(ClientError):
        other.pull(archive.name, downloaded)
    assert list(downloaded.iterdir()) == []
    assert store.check_remote(archive.name)["verified"] is True


def test_sdk_region_supports_s3_compatible_clouds(monkeypatch):
    # Reject obvious bad CLI input before constructing a credentialed client.
    from chaptera_server import ops_backup_s3
    with pytest.raises(OffhostRejected):
        ops_backup_s3._client("../metadata", None)
    with pytest.raises(OffhostRejected):
        ops_backup_s3._client("us-east-1", "http://127.0.0.1:9000")
    with mock_aws():
        s = ops_backup_s3._client("us-east-1", None)
        assert s.meta.region_name == "us-east-1"
        # Cloud-compatible region notation used by many S3 providers.
        assert ops_backup_s3._client("ru-central1", None).meta.region_name == "ru-central1"


@mock_aws
def test_boto3_environment_endpoint_cannot_downgrade_tls(monkeypatch):
    from chaptera_server import ops_backup_s3
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:9000")
    with pytest.raises(OffhostRejected):
        ops_backup_s3._client("us-east-1", None)
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3")
    # No environment override: secure AWS SDK default remains accepted.
    assert ops_backup_s3._client("us-east-1", None).meta.endpoint_url.startswith("https://")
