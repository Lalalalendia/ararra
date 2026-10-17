"""RE-06: proof-validated desktop atom packets survive quota reset and restart."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import chaptera_server.storage as storage
from chaptera_server.atom_checkpoints import (
    SQLiteAtomCheckpoint, AtomCheckpointConflict,
)
import chaptera_server.quota as quota
from chaptera_server.quota import DailyQuotaExhausted, DailyQuotaLedger, QuotaControlledProvider, pacific_window
from chaptera_server.storage import QueueStore, JobSpec
from chaptera_server.worker import TranslationWorker
from chaptera_translation_core import ChapterInput, TranslationCore, TranslationRejected
from chaptera_translation_core.atom_transport import atom_checkpoint_key


def chinese_packet_response(payload: str) -> str:
    document = json.loads(payload)
    cols = {name: pos for pos, name in enumerate(document["columns"])}
    atoms = [{name: row[pos] for name, pos in cols.items()} for row in document["rows"]]
    groups = []
    for atom in atoms:
        if groups and groups[-1][0] == atom["render_domain_id"]:
            groups[-1][1].append(atom)
        else:
            groups.append((atom["render_domain_id"], [atom]))
    segments = [
        {
            "covers": [atom["atom_id"] for atom in group],
            "translation": "".join(a["source_text"] for a in group).replace("世界", "Мир"),
        }
        for _, group in groups
    ]
    return json.dumps({"segments": segments}, ensure_ascii=False)


class TranslatingProvider:
    def __init__(self, fail_call=None, reset_at=None):
        self.requests = []
        self.fail_call = fail_call
        self.reset_at = reset_at

    async def generate(self, request):
        key = atom_checkpoint_key(
            request.prompt, request.source_html_with_markers, request.request_role,
        )
        self.requests.append(key)
        if self.fail_call == len(self.requests):
            raise DailyQuotaExhausted(self.reset_at)
        return chinese_packet_response(request.source_html_with_markers)


def big_source():
    # Matches existing atom transport test; deterministic >5 provider packets.
    return "<body><p>" + "世界。" * 17000 + "</p></body>"


def test_quota_resume_reuses_completed_desktop_atom_packets(tmp_path, monkeypatch):
    moment = [datetime(2026, 10, 17, 12, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: moment[0]))
    db = tmp_path / "jobs.db"
    source = big_source()
    store = QueueStore(db)
    job = store.create_job("moderator", JobSpec("chapter", source, transport="atoms_v1"))
    first_provider = TranslatingProvider(fail_call=2, reset_at=moment[0] + 100)
    assert asyncio.run(TranslationWorker(store, first_provider).run_once())
    waiting = store.get_job(job["id"])
    assert waiting["status"] == "retry_wait"
    assert first_provider.requests[0] != first_provider.requests[1]
    with store._connect() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM atom_job_checkpoints WHERE job_id=?", (job["id"],)
        ).fetchone()[0]
    assert count == 1
    assert any(e["stage"] == "atom_checkpoint_saved" for e in store.events(job["id"]))
    assert store.claim_next() is None

    # New store/provider/worker simulate a process restart and next quota day.
    moment[0] += 101
    second_provider = TranslatingProvider()
    new_store = QueueStore(db)
    assert asyncio.run(TranslationWorker(new_store, second_provider).run_once())
    result = new_store.get_job(job["id"])
    assert result["status"] == "completed"
    assert result["translated_html"] == source.replace("世界", "Мир")
    assert first_provider.requests[0] not in second_provider.requests
    assert any(e["stage"] == "atom_checkpoint_hit" for e in new_store.events(job["id"]))
    with new_store._connect() as con:
        n = con.execute("SELECT COUNT(*) FROM atom_job_checkpoints WHERE job_id=?",
                        (job["id"],)).fetchone()[0]
    # Terminal success deletes duplicated checkpoint text in the same commit.
    assert n == 0
    assert len(second_provider.requests) > 4


def test_checkpoint_lease_fence_integrity_and_request_scope(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("moderator", JobSpec("c1", "<p>世界。</p>", transport="atoms_v1"))
    _, token = store.claim_next()
    saved = SQLiteAtomCheckpoint(store, job["id"], token)
    key = atom_checkpoint_key("prompt", "source", "translation")
    other = atom_checkpoint_key("changed glossary", "source", "translation")
    saved.save(key, "proved response")
    assert saved.load(key) == "proved response"
    assert saved.load(other) is None
    from_other_model = SQLiteAtomCheckpoint(store, job["id"], token, model_id="another-model")
    assert from_other_model.load(key) is None
    assert saved.saved_this_run == 1
    assert saved.reused_this_run == 1
    with pytest.raises(AtomCheckpointConflict, match="Conflicting"):
        saved.save(key, "different translation")
    with store._connect() as con:
        con.execute("UPDATE atom_job_checkpoints SET response_text='tampered' WHERE job_id=?",
                    (job["id"],))
    with pytest.raises(AtomCheckpointConflict, match="integrity"):
        saved.load(key)
    assert store.retry(job["id"], token, "simulate worker restart", delay_seconds=0.0)
    with pytest.raises(AtomCheckpointConflict, match="lease"):
        saved.save(other, "outdated")
    with pytest.raises(AtomCheckpointConflict, match="lease"):
        saved.load(other)


def test_corrupt_replayed_packet_fails_closed_without_provider_call():
    class CorruptCheckpoint:
        def load(self, _key):
            return json.dumps({"segments": [
                {"covers": ["forged-atom"], "translation": "Мнимый перевод"},
            ]})
        def save(self, _key, _raw):
            pytest.fail("Cannot store corrupt response")

    provider = TranslatingProvider()
    with pytest.raises(TranslationRejected, match="atom_checkpoint"):
        asyncio.run(TranslationCore(
            provider, atom_checkpoint=CorruptCheckpoint(),
        ).translate(ChapterInput("c", "<p>世界。</p>", transport="atoms_v1")))
    assert provider.requests == []


def test_job_isolation_prevents_reuse_across_source_revisions(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    one = store.create_job("mod", JobSpec("same-chapter", "<p>世界。</p>", transport="atoms_v1"))
    two = store.create_job("mod", JobSpec("same-chapter", "<p>世界 изменился。</p>", transport="atoms_v1"))
    claim, token = store.claim_next()
    assert claim["id"] == one["id"]
    first = SQLiteAtomCheckpoint(store, one["id"], token)
    key = atom_checkpoint_key("same prompt", "identical payload", "translation")
    first.save(key, "response 1")
    assert store.retry(one["id"], token, "move aside", delay_seconds=1000)
    claimed, token2 = store.claim_next()
    assert claimed["id"] == two["id"]
    second = SQLiteAtomCheckpoint(store, two["id"], token2)
    assert second.load(key) is None


class OfflineChapterPacketProvider:
    def __init__(self, *, return_empty_segments=False):
        self.calls = []
        self.return_empty_segments = return_empty_segments

    async def generate(self, request):
        assert request.transport == "atoms_v1"
        self.calls.append(atom_checkpoint_key(
            request.prompt, request.source_html_with_markers, request.request_role,
        ))
        if self.return_empty_segments:
            return '{"segments":[]}'
        return chinese_packet_response(request.source_html_with_markers)


def test_many_pacific_days_of_durable_atom_progress_are_not_false_partial_failures(
    tmp_path, monkeypatch,
):
    clock = [datetime(2026, 10, 17, 12, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(quota, "time", SimpleNamespace(time=lambda: clock[0]))

    path = tmp_path / "atom-jobs.db"
    original = big_source()
    store = QueueStore(path)
    job = store.create_job("moderator", JobSpec(
        "chapter-42", original, transport="atoms_v1", max_attempts=1,
    ))
    remote = OfflineChapterPacketProvider()
    pauses = 0

    # Fresh DB handles, quota wrappers and workers each day: no in-memory
    # subrequests survive; all recovery evidence lives in SQLite.
    for day in range(30):
        current_store = QueueStore(path)
        current = QuotaControlledProvider(
            remote, DailyQuotaLedger(current_store, "our-project", "gemini-test", 1),
        )
        assert asyncio.run(TranslationWorker(current_store, current).run_once())
        state = current_store.get_job(job["id"])
        if state["status"] == "completed":
            assert state["translated_html"] == original.replace("世界", "Мир")
            break
        assert state["status"] == "retry_wait"
        assert state["attempts"] == 0
        with current_store._connect() as con:
            counters = con.execute(
                "SELECT quota_partial_waits FROM jobs WHERE id=?", (job["id"],)
            ).fetchone()
            assert counters["quota_partial_waits"] == 0
        pauses += 1
        clock[0] = pacific_window(clock[0])[1] + 1
    else:
        pytest.fail("Source-owned packet translation never finished across 30 days")

    # Long chapter, daily RPD=1: more than two pauses are required. The
    # original blanket 'made_calls' rule failed on the third day.
    assert pauses >= 3
    assert len(remote.calls) > 3
    assert len(remote.calls) == len(set(remote.calls))
    # QueueStore.events is intentionally capped to the most recent entries;
    # use the SQL count for an all-days assertion.
    with current_store._connect() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM job_events WHERE job_id=? AND stage='quota_wait'",
            (job["id"],),
        ).fetchone()[0] == pauses


def test_unvalidated_success_is_still_expensive_partial_work(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 17, 12, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(quota, "time", SimpleNamespace(time=lambda: clock[0]))
    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("mod", JobSpec(
        "chapter", big_source(), transport="atoms_v1", max_attempts=1,
    ))
    remote = OfflineChapterPacketProvider(return_empty_segments=True)
    guarded = QuotaControlledProvider(
        remote, DailyQuotaLedger(store, "one-project", "gemini-test", 1),
    )
    assert asyncio.run(TranslationWorker(store, guarded).run_once())
    assert store.get_job(job["id"])["status"] == "retry_wait"
    with store._connect() as con:
        row = con.execute("SELECT quota_partial_waits FROM jobs WHERE id=?",
                          (job["id"],)).fetchone()
        assert row["quota_partial_waits"] == 1
        assert con.execute("SELECT COUNT(*) FROM atom_job_checkpoints").fetchone()[0] == 0


def test_only_confirmed_durable_context_or_atom_calls_are_excluded(tmp_path):
    class Counter:
        completed_calls = 6

    worker = TranslationWorker(QueueStore(tmp_path / "jobs.db"), Counter())
    checkpoint = SimpleNamespace(saved_this_run=2)
    assert worker._uncheckpointed_completed_work(0, checkpoint, 3)
    assert not worker._uncheckpointed_completed_work(0, checkpoint, 4)
    assert worker._uncheckpointed_completed_work(0, None, 5)
