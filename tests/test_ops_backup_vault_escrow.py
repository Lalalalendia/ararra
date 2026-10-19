"""RE-08 DR: a valid encrypted SQLite restore still needs independent vault escrow.

Disposable synthetic data only. Neither this test nor the backup CLI exports
the real systemd gemini_vault_key; a second private operator-held secret must
survive host loss for moderator project credentials to remain usable.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from chaptera_server.library import BookLibrary
from chaptera_server.ops_backup import BackupRejected, snapshot
from chaptera_server.ops_backup_crypto import generate_key, seal, unseal
from chaptera_server.ops_backup_vault import main as vault_cli, verify_restored_vault
from chaptera_server.personal_gemini_keys import (
    GeminiVaultUnavailable, PersonalGeminiKeys,
)
from chaptera_server.storage import JobSpec, QueueStore


def _private(root: Path, name: str) -> Path:
    folder = root / name
    folder.mkdir(mode=0o700)
    return folder


def test_encrypted_disaster_restore_requires_backup_key_and_gemini_vault_key(tmp_path):
    db = tmp_path / "host-lost.sqlite3"
    store = QueueStore(db)
    library = BookLibrary(store)
    library.create_book(
        "synthetic-novel", "Fixture novel",
        owner_member_id="alice", actor_id="operator",
    )
    library.put_chapter(
        "synthetic-novel", "first",
        chapter_index=1, chapter_path="chapters/1.xhtml",
        source_html="<p>Safe synthetic source chapter.</p>",
        expected_revision=0,
    )
    queued = store.create_job(
        "alice", JobSpec(chapter_id="synthetic-job",
                         source_html="<p>Only test queue data.</p>"),
    )

    # The backup-encryption key and the *application* Gemini vault master are
    # independent. Both must be independently recoverable after a host failure.
    vault_master = base64.b64encode(os.urandom(32)).decode("ascii")
    wrong_master = base64.b64encode(os.urandom(32)).decode("ascii")
    test_api_key = "AIzaTEST_private_disaster_restore_key_012345"
    vault = PersonalGeminiKeys(store, vault_master)
    vault.put("alice", "alice-project-001", test_api_key)
    vault.set_daily_cap("alice", "alice-project-001", 7)
    fingerprint = vault.choices("alice")[0][1]

    keys = _private(tmp_path, "operator-keys")
    backup_key = generate_key(keys / "backup-rbe.key")
    wrong_backup_key = generate_key(keys / "wrong-rbe.key")
    snapshots = _private(tmp_path, "plain-snapshots")
    sealed_dir = _private(tmp_path, "encrypted")
    destination = _private(tmp_path, "other-host-recovery")

    manifest = snapshot(db, snapshots)
    archive = seal(manifest, backup_key, sealed_dir)
    ciphertext = archive.read_bytes()
    assert test_api_key.encode("ascii") not in ciphertext
    assert vault_master.encode("ascii") not in ciphertext
    assert backup_key.read_bytes() not in ciphertext

    # A fully intact archive is unusable without its separate AES-GCM key.
    wrong_dest = destination / "wrong-backup.sqlite3"
    with pytest.raises(BackupRejected):
        unseal(archive, wrong_backup_key, wrong_dest)
    assert not wrong_dest.exists()

    recovered = unseal(archive, backup_key, destination / "jobs.sqlite3")
    # An independent operator should recover the *original* systemd vault
    # credential from a distinct escrow, never from the .rbe or the RBE key.
    escrow = _private(tmp_path, "separate-vault-escrow")
    vault_file = escrow / "gemini_vault_key"
    vault_file.write_text(vault_master + "\n", encoding="ascii")
    vault_file.chmod(0o600)
    wrong_vault_file = escrow / "wrong_vault"
    wrong_vault_file.write_text(wrong_master, encoding="ascii")
    wrong_vault_file.chmod(0o600)
    assert verify_restored_vault(recovered, vault_file) == {
        "schema": "readera.gemini-vault-recovery.v1", "projects_checked": 1,
    }
    with pytest.raises(BackupRejected):
        verify_restored_vault(recovered, wrong_vault_file)

    restored_store = QueueStore(recovered)
    assert BookLibrary(restored_store).get_chapter(
        "synthetic-novel", "first",
    )["source_html"] == "<p>Safe synthetic source chapter.</p>"
    assert restored_store.get_job(queued["id"])["status"] == "queued"

    # A successful SQLite integrity check alone says nothing about whether
    # personal Gemini credentials can still be decrypted on a new host.
    missing = PersonalGeminiKeys(restored_store)
    assert missing.list_for("alice")["configured"] is False
    with pytest.raises(GeminiVaultUnavailable):
        missing.resolve("alice", "alice-project-001", fingerprint)

    incorrect = PersonalGeminiKeys(restored_store, wrong_master)
    with pytest.raises(GeminiVaultUnavailable):
        incorrect.resolve("alice", "alice-project-001", fingerprint)

    original_master = PersonalGeminiKeys(restored_store, vault_master)
    assert original_master.resolve(
        "alice", "alice-project-001", fingerprint,
    ) == test_api_key
    assert original_master.daily_cap_for("alice", "alice-project-001") == 7
    assert original_master.choices("alice") == [
        ("alice-project-001", fingerprint)
    ]
    # Failed attempts must not modify the recovered database or auto-rotate
    # credentials, and the live original is never overwritten.
    assert db.is_file() and recovered.is_file()
    assert wrong_dest.exists() is False
