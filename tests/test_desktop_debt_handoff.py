"""RE-06 desktop-style durable debt-parent handoff and strict finalize-only."""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

import chaptera_server.storage as storage
from chaptera_server.atom_checkpoints import AtomCheckpointConflict, SQLiteAtomCheckpoint
from chaptera_server.storage import JobSpec, QueueStore
from chaptera_server.worker import TranslationWorker
from chaptera_translation_core import ChapterInput, TranslationCore, TranslationRejected
import chaptera_translation_core.service as core_service


def multi_domain_source(count=14) -> str:
    return "<body>" + "".join(
        f"<p>世界。第{i}段。</p>" for i in range(count)
    ) + "</body>"


def packet_segments(payload: str) -> list[dict]:
    packet = json.loads(payload)
    indexes = {name: i for i, name in enumerate(packet["columns"])}
    rows = [
        {name: row[i] for name, i in indexes.items()}
        for row in packet["rows"]
    ]
    groups = []
    for atom in rows:
        if groups and groups[-1][0] == atom["render_domain_id"]:
            groups[-1][1].append(atom)
        else:
            groups.append((atom["render_domain_id"], [atom]))
    return [
        {
            "covers": [item["atom_id"] for item in group],
            "translation": "".join(item["source_text"] for item in group)
            .replace("世界", "Мир"),
        }
        for _, group in groups
    ]


class OneProvenDomainProvider:
    """Provider creates 13 source-owned debts; repairs each domain independently."""
    def __init__(self, *, allow_initial=True):
        self.requests = []
        self.allow_initial = allow_initial

    async def generate(self, request):
        self.requests.append(request)
        segments = packet_segments(request.source_html_with_markers)
        if request.request_role == "translation":
            assert self.allow_initial, "Initial translation was already stored"
            segments = segments[:1]
        return json.dumps({"segments": segments}, ensure_ascii=False)


def test_thirteen_debts_handoff_after_twelve_and_replay_only_last_missing(
    tmp_path, monkeypatch,
):
    clock = [1000.0]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))
    db = tmp_path / "recovery.db"
    source = multi_domain_source()
    store = QueueStore(db)
    job = store.create_job("mod", JobSpec(
        "chapter", source, transport="atoms_v1", max_attempts=1,
    ))
    first = OneProvenDomainProvider()
    assert asyncio.run(TranslationWorker(store, first).run_once())
    waiting = store.get_job(job["id"])
    assert waiting["status"] == "retry_wait"
    assert waiting["attempts"] == 0
    assert len(first.requests) == 13
    assert sum(r.request_role == "targeted_repair" for r in first.requests) == 12
    with store._connect() as con:
        state = con.execute(
            "SELECT phase, remaining_debts, debt_passes FROM atom_recovery_state WHERE job_id=?",
            (job["id"],),
        ).fetchone()
        assert (state["phase"], state["remaining_debts"], state["debt_passes"]) == (
            "debt_repair", 13, 1,
        )
        assert con.execute(
            "SELECT COUNT(*) FROM atom_job_checkpoints WHERE job_id=?", (job["id"],)
        ).fetchone()[0] == 13
    assert any(e["stage"] == "debt_wait" for e in store.events(job["id"]))

    # New worker process and SQLite handle: 13 formerly paid subrequests
    # are all replayed from disk without touching Gemini.
    clock[0] += 2
    restarted = QueueStore(db)
    last = OneProvenDomainProvider(allow_initial=False)
    assert asyncio.run(TranslationWorker(restarted, last).run_once())
    outcome = restarted.get_job(job["id"])
    assert outcome["status"] == "completed"
    assert outcome["translated_html"] == source.replace("世界", "Мир")
    assert len(last.requests) == 1
    assert last.requests[0].request_role == "targeted_repair"
    assert last.requests[0].unit_count == 1
    events = restarted.events(job["id"])
    assert any(e["stage"] == "atom_finalize_only" for e in events)
    with restarted._connect() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM atom_job_checkpoints WHERE job_id=?", (job["id"],)
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM atom_recovery_state WHERE job_id=?", (job["id"],)
        ).fetchone()[0] == 0


def test_finalize_only_survives_local_crash_without_another_gemini_call(
    tmp_path, monkeypatch,
):
    clock = [1000.0]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))
    source = "<body><p>世界。</p></body>"
    db = tmp_path / "finalize.db"
    store = QueueStore(db)
    job = store.create_job("mod", JobSpec(
        "chapter", source, transport="atoms_v1", max_attempts=3,
    ))

    class OneShot:
        calls = 0
        async def generate(self, request):
            self.calls += 1
            return json.dumps({
                "segments": packet_segments(request.source_html_with_markers),
            }, ensure_ascii=False)

    original = core_service.finalize_publication_html
    finalizations = [0]

    def crashed_after_proof(*args, **kwargs):
        finalizations[0] += 1
        if finalizations[0] == 1:
            raise RuntimeError("synthetic local process failure after proof checkpoint")
        return original(*args, **kwargs)

    monkeypatch.setattr(core_service, "finalize_publication_html", crashed_after_proof)
    provider = OneShot()
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert provider.calls == 1
    assert store.get_job(job["id"])["status"] == "retry_wait"
    with store._connect() as con:
        assert con.execute(
            "SELECT phase FROM atom_recovery_state WHERE job_id=?", (job["id"],)
        ).fetchone()["phase"] == "finalize_only"

    class Forbidden:
        calls = 0
        async def generate(self, _request):
            self.calls += 1
            pytest.fail("Finalized source proof MUST NOT be regenerated or billed")

    clock[0] += 16
    restarted = QueueStore(db)
    forbidden = Forbidden()
    assert asyncio.run(TranslationWorker(restarted, forbidden).run_once())
    assert restarted.get_job(job["id"])["status"] == "completed"
    assert restarted.get_job(job["id"])["translated_html"] == source.replace("世界", "Мир")
    assert forbidden.calls == 0
    assert finalizations[0] == 2
    assert any(e["stage"] == "atom_finalize_only_resume"
               for e in restarted.events(job["id"]))


def test_finalize_only_missing_cached_proof_fails_closed_without_model_call(
    tmp_path, monkeypatch,
):
    clock = [1000.0]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))
    db = tmp_path / "fail-closed.db"
    source = "<p>世界。</p>"
    store = QueueStore(db)
    job = store.create_job("mod", JobSpec(
        "chapter", source, transport="atoms_v1", max_attempts=3,
    ))

    class One:
        async def generate(self, request):
            return json.dumps({"segments": packet_segments(request.source_html_with_markers)},
                              ensure_ascii=False)

    original = core_service.finalize_publication_html

    def one_bad_finalize(*args, **kwargs):
        raise RuntimeError("synthetic local failure")

    monkeypatch.setattr(core_service, "finalize_publication_html", one_bad_finalize)
    assert asyncio.run(TranslationWorker(store, One()).run_once())
    with store._connect() as con:
        assert con.execute(
            "SELECT phase FROM atom_recovery_state WHERE job_id=?", (job["id"],)
        ).fetchone()["phase"] == "finalize_only"
        con.execute("DELETE FROM atom_job_checkpoints WHERE job_id=?", (job["id"],))
    monkeypatch.setattr(core_service, "finalize_publication_html", original)

    class Forbidden:
        calls = 0
        async def generate(self, request):
            self.calls += 1
            pytest.fail("Missing final proof cannot trigger paid Gemini")

    clock[0] += 16
    forbidden = Forbidden()
    assert asyncio.run(TranslationWorker(QueueStore(db), forbidden).run_once())
    outcome = store.get_job(job["id"])
    assert outcome["status"] == "failed"
    assert outcome["translated_html"] is None
    assert forbidden.calls == 0
    assert "finalize_only" in outcome["last_error"]


def test_recovery_phase_fenced_by_lease_source_and_model(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    source = "<p>世界。</p>"
    job = store.create_job("mod", JobSpec(
        "chapter", source, transport="atoms_v1",
    ))
    _, token = store.claim_next()
    digest = hashlib.sha256(source.encode()).hexdigest()
    other_digest = hashlib.sha256((source + "changed").encode()).hexdigest()
    checkpoint = SQLiteAtomCheckpoint(store, job["id"], token, model_id="gemini-a")
    checkpoint.mark_phase(digest, "debt_repair", remaining_debts=1)
    assert checkpoint.phase(digest) == "debt_repair"
    checkpoint.mark_phase(digest, "finalize_only")
    assert checkpoint.phase(digest) == "finalize_only"
    with pytest.raises(AtomCheckpointConflict, match="cannot restart"):
        checkpoint.mark_phase(digest, "debt_repair", remaining_debts=1)
    with pytest.raises(AtomCheckpointConflict, match="source/model"):
        checkpoint.phase(other_digest)
    with pytest.raises(AtomCheckpointConflict, match="source/model"):
        SQLiteAtomCheckpoint(store, job["id"], token, model_id="gemini-b").phase(digest)
    assert store.retry(job["id"], token, "simulate worker restart", delay_seconds=0)
    with pytest.raises(AtomCheckpointConflict, match="lease"):
        checkpoint.mark_phase(digest, "finalize_only")


def test_desktop_repair_bound_still_fails_closed_without_durable_queue():
    source = multi_domain_source()
    provider = OneProvenDomainProvider()
    with pytest.raises(TranslationRejected, match="too many damaged domains"):
        asyncio.run(TranslationCore(provider).translate(ChapterInput(
            "standalone", source, transport="atoms_v1",
        )))
    assert len(provider.requests) == 1


def test_debt_handoff_has_finite_32_wave_bound_and_terminal_cleanup(tmp_path):
    source = "<p>世界。</p>"
    store = QueueStore(tmp_path / "bound.db")
    job = store.create_job("mod", JobSpec("c", source, transport="atoms_v1"))
    _, token = store.claim_next()
    digest = hashlib.sha256(source.encode()).hexdigest()
    checkpoint = SQLiteAtomCheckpoint(store, job["id"], token)
    checkpoint.mark_phase(digest, "debt_repair", remaining_debts=2)
    with store._connect() as con:
        con.execute("UPDATE atom_recovery_state SET debt_passes=32 WHERE job_id=?",
                    (job["id"],))
    assert store.defer_atom_debt_handoff(job["id"], token)
    assert store.get_job(job["id"])["status"] == "failed"
    with store._connect() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM atom_recovery_state WHERE job_id=?", (job["id"],)
        ).fetchone()[0] == 0
