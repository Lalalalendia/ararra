from __future__ import annotations

import time

import pytest

from chaptera_server.storage import IdempotencyConflict, JobSpec, QueueStore


def test_persistence_and_idempotency_survive_reopening_database(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    store = QueueStore(db)
    job = store.create_job("alice", JobSpec("1", "<p>世界正在燃烧。</p>"), "alice-1")
    assert store.create_job("alice", JobSpec("1", "<p>世界正在燃烧。</p>"), "alice-1")["id"] == job["id"]
    assert QueueStore(db).get_job(job["id"])["status"] == "queued"
    assert QueueStore(db).list_jobs()[0]["id"] == job["id"]
    with pytest.raises(IdempotencyConflict):
        QueueStore(db).create_job("alice", JobSpec("1", "<p>Changed</p>"), "alice-1")
    other = QueueStore(db).create_job("bob", JobSpec("1", "<p>Changed</p>"), "alice-1")
    assert other["id"] != job["id"]


def test_atomic_claim_and_fencing_prevent_stale_commits(tmp_path):
    a = QueueStore(tmp_path / "jobs.db")
    b = QueueStore(tmp_path / "jobs.db")
    job = a.create_job("alice", JobSpec("7", "<p>O</p>"))
    claimed, token = a.claim_next(lease_seconds=10)
    assert claimed["id"] == job["id"]
    assert b.claim_next() is None
    assert not b.complete(job["id"], "wrong-token", "<p>T</p>", "sha", 1)
    assert a.complete(job["id"], token, "<p>T</p>", "sha", 1)
    assert b.get_job(job["id"])["translated_html"] == "<p>T</p>"
    assert not b.complete(job["id"], token, "<p>Changed</p>", "sha", 1)
    assert b.get_job(job["id"])["translated_html"] == "<p>T</p>"


def test_retry_and_lease_recovery(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("alice", JobSpec("7", "<p>O</p>", max_attempts=2))
    claimed, token = store.claim_next(lease_seconds=0.01)
    assert store.retry(job["id"], token, "Temporary outage", delay_seconds=0)
    new, token2 = store.claim_next(lease_seconds=0.01)
    assert new["attempts"] == 2
    assert store.retry(job["id"], token2, "Repeated outage", delay_seconds=0)
    assert store.get_job(job["id"])["status"] == "failed"
    assert store.claim_next() is None


def test_running_cancellation_prevents_completed_payload(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("alice", JobSpec("1", "<p>x</p>"))
    claimed, token = store.claim_next()
    assert store.request_cancel(job["id"])["status"] == "running"
    assert store.cancellation_requested(job["id"], token)
    assert store.complete(job["id"], token, "<p>T</p>", "sha", 1)
    saved = store.get_job(job["id"])
    assert saved["status"] == "cancelled"
    assert saved["translated_html"] is None


def test_pending_cancellation_is_immediate(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("alice", JobSpec("1", "<p>x</p>"))
    assert store.request_cancel(job["id"])["status"] == "cancelled"
    assert store.claim_next() is None


def test_orphaned_claim_is_recovered_without_reusing_old_owner(tmp_path, monkeypatch):
    import chaptera_server.storage as storage
    clock = [1000.0]
    monkeypatch.setattr(storage.time, "time", lambda: clock[0])
    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("alice", JobSpec("1", "<p>x</p>", max_attempts=2))
    claimed, token = store.claim_next(lease_seconds=2)
    clock[0] += 3
    renewed, new_token = store.claim_next()
    assert renewed["id"] == job["id"]
    assert new_token != token
    assert not store.complete(job["id"], token, "<p>wrong</p>", "", 1)
