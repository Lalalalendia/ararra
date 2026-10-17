"""RE-06: source-proven Gemini partial response recovery, never guess missing text."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import chaptera_server.quota as quota
import chaptera_server.storage as storage
from chaptera_server.quota import DailyQuotaLedger, QuotaControlledProvider, pacific_window
from chaptera_server.storage import JobSpec, QueueStore
from chaptera_server.worker import TranslationWorker
from chaptera_translation_core import ChapterInput, TranslationCore, TranslationRejected
from chaptera_translation_core.api.errors import PartialGenerationError
from chaptera_translation_core.atom_transport import (
    salvage_partial_atom_response,
)


def _segments(payload: str) -> list[dict]:
    source = json.loads(payload)
    names = {name: i for i, name in enumerate(source["columns"])}
    rows = [{name: row[i] for name, i in names.items()} for row in source["rows"]]
    grouped = []
    for row in rows:
        if grouped and grouped[-1][0] == row["render_domain_id"]:
            grouped[-1][1].append(row)
        else:
            grouped.append((row["render_domain_id"], [row]))
    return [
        {"covers": [row["atom_id"] for row in group],
         "translation": "".join(row["source_text"] for row in group)
         .replace("世界", "Мир")
         .replace("兄弟", "братья")
         .replace("こんにちは", "Привет")}
        for _, group in grouped
    ]


def _valid(payload: str) -> str:
    return json.dumps({"segments": _segments(payload)}, ensure_ascii=False)


def _truncated(payload: str) -> str:
    segments = _segments(payload)
    assert len(segments) >= 2
    # Only the FIRST segment is a fully decoded source-owned object.
    return ('{"segments":[' + json.dumps(segments[0], ensure_ascii=False)
            + ',{"covers":["unfinished')


class TruncatingProvider:
    def __init__(self):
        self.calls = []

    async def generate(self, request):
        self.calls.append(request)
        if len(self.calls) == 1:
            raise PartialGenerationError(
                "Gemini answer stopped at MAX_TOKENS",
                _truncated(request.source_html_with_markers),
                "MAX_TOKENS",
            )
        return _valid(request.source_html_with_markers)


def test_partial_json_recovers_proven_source_and_repairs_only_missing_domains():
    provider = TruncatingProvider()
    chapter = ChapterInput(
        "chapter-one",
        "<body><p>世界。</p><p>兄弟。</p><p>こんにちは。</p></body>",
        transport="atoms_v1",
    )
    events = []
    result = asyncio.run(TranslationCore(
        provider, on_event=lambda e: events.append(e.stage),
    ).translate(chapter))
    assert result.translated_html == (
        "<body><p>Мир。</p><p>братья。</p><p>Привет。</p></body>"
    )
    assert result.unit_count == 3
    assert events.count("atom_partial_recovered") == 1
    assert len(provider.calls) == 3
    assert provider.calls[0].unit_count == 3
    assert [call.unit_count for call in provider.calls[1:]] == [1, 1]
    # Never retranslate the source domain already proved in the partial.
    first_id = _segments(provider.calls[0].source_html_with_markers)[0]["covers"][0]
    assert all(first_id not in str(_segments(call.source_html_with_markers))
               for call in provider.calls[1:])


def test_partial_parser_never_invents_mid_segment_or_accepts_arbitrary_prose():
    from chaptera_translation_core.atom_transport import (
        prepare_source_atoms, packets_for_atoms,
    )
    from chaptera_translation_core.core.worker_helpers.translation_completeness import (
        build_translation_unit_source_bundle,
    )
    marked, units = build_translation_unit_source_bundle(
        "<p>世界。</p><p>兄弟。</p>",
    )
    atoms = packets_for_atoms(prepare_source_atoms(units, chapter_id="c"))[0].atoms
    assert salvage_partial_atom_response('{"segments":[{"covers":["unfinished', atoms) is None
    assert salvage_partial_atom_response('Here are translated paragraphs', atoms) is None
    assert salvage_partial_atom_response("x" * 2_000_001, atoms) is None


def test_partial_unknown_atom_identity_fails_closed_without_repair_or_commit():
    class MaliciousProvider:
        calls = 0

        async def generate(self, request):
            self.calls += 1
            return_partial = ('{"segments":[{"covers":["forged"],'
                              '"translation":"Неизвестный текст"},'
                              '{"covers":["unfinished')
            raise PartialGenerationError("partial", return_partial, "MAX_TOKENS")

    provider = MaliciousProvider()
    with pytest.raises(TranslationRejected, match="atom_partial_identity"):
        asyncio.run(TranslationCore(provider).translate(ChapterInput(
            "c", "<p>世界。</p><p>兄弟。</p>", transport="atoms_v1",
        )))
    assert provider.calls == 1


def test_recovered_partial_is_durable_after_daily_quota_and_worker_restart(
    tmp_path, monkeypatch,
):
    clock = [datetime(2026, 10, 17, 12, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(quota, "time", SimpleNamespace(time=lambda: clock[0]))
    db = tmp_path / "partial-jobs.db"
    store = QueueStore(db)
    original = "<body><p>世界。</p><p>兄弟。</p></body>"
    job = store.create_job("moderator", JobSpec(
        "c", original, transport="atoms_v1", max_attempts=1,
    ))
    first = TruncatingProvider()
    today = QuotaControlledProvider(
        first, DailyQuotaLedger(store, "owner-one-project", "model-x", 1),
    )
    assert asyncio.run(TranslationWorker(store, today).run_once())
    waiting = store.get_job(job["id"])
    assert waiting["status"] == "retry_wait"
    assert waiting["attempts"] == 0
    assert len(first.calls) == 1
    with store._connect() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM atom_job_checkpoints WHERE job_id=?", (job["id"],)
        ).fetchone()[0] == 1

    clock[0] = pacific_window(clock[0])[1] + 1

    class RepairProvider:
        def __init__(self):
            self.calls = []

        async def generate(self, request):
            self.calls.append(request)
            return _valid(request.source_html_with_markers)

    after_reset = RepairProvider()
    restarted = QueueStore(db)
    tomorrow = QuotaControlledProvider(
        after_reset, DailyQuotaLedger(restarted, "owner-one-project", "model-x", 1),
    )
    assert asyncio.run(TranslationWorker(restarted, tomorrow).run_once())
    assert restarted.get_job(job["id"])["status"] == "completed"
    assert restarted.get_job(job["id"])["translated_html"] == (
        "<body><p>Мир。</p><p>братья。</p></body>"
    )
    assert len(after_reset.calls) == 1
    assert after_reset.calls[0].request_role == "targeted_repair"
    assert after_reset.calls[0].unit_count == 1
    assert any(e["stage"] == "atom_checkpoint_hit" for e in restarted.events(job["id"]))
    with restarted._connect() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM atom_job_checkpoints WHERE job_id=?", (job["id"],)
        ).fetchone()[0] == 0


def test_plain_html_partial_is_not_silently_published(tmp_path):
    class Broken:
        async def generate(self, _request):
            raise PartialGenerationError(
                "upstream stream broke", "<p>Partly translated</p>",
                "CONNECTION_RESET",
            )

    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("mod", JobSpec(
        "chapter", "<p>世界。</p>", max_attempts=1,
    ))
    assert asyncio.run(TranslationWorker(store, Broken()).run_once())
    state = store.get_job(job["id"])
    assert state["status"] == "retry_wait"
    assert state["attempts"] == 0
    assert state["translated_html"] is None
    assert any(e["stage"] == "transient_wait" for e in store.events(job["id"]))
