"""RE-06 revision-fenced post-translation QA and team review integration.

The desktop gate is strictly diagnostic. Persisted findings always refer to
one accepted source/glossary revision; a human decision never changes text.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.library import LibraryConflict
from chaptera_server.storage import QueueStore


def workspace(tmp_path):
    db = tmp_path / "qa.sqlite3"
    settings = Settings(
        db_path=db, team_tokens={
            "admin": "admin-local", "alice": "alice-local", "bob": "bob-local",
        }, admin_member_id="admin",
    )
    app = create_app(settings, QueueStore(db))
    library = app.state.library
    library.create_book("novel", "Test novel", owner_member_id="alice", actor_id="admin")
    library.put_chapter(
        "novel", "c1", chapter_index=1, chapter_path="chapters/c1.xhtml",
        source_html="<p>老蘭斯洛留下了文字。</p>", expected_revision=0,
    )
    library.put_glossary(
        "novel", glossary={
            "老蘭斯洛": {"rus": "Старик Ланселот", "note": "Персонаж"},
            "考爾·蘭斯洛": {"rus": "Коул Ланселот", "note": "Персонаж"},
        }, versions={}, authority="READY", expected_revision=0, actor_id="alice",
    )
    library.accept_translation(
        "novel", "c1", "<p>Коул Ланселот оставил запись.</p>",
        expected_revision=0, expected_source_revision=1, actor_id="alice",
    )
    return app, library


def revision_req(*, source=1, accepted=1, glossary=1):
    return dict(expected_source_revision=source,
                expected_accepted_revision=accepted,
                expected_glossary_revision=glossary)


def test_authenticated_qa_snapshot_manual_acceptance_and_durable_human_review(tmp_path):
    app, library = workspace(tmp_path)
    base = "/v1/books/novel/chapters/c1/qa"

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url="http://test",
        ) as client:
            alice = {"X-API-Key": "alice-local"}
            bob = {"X-API-Key": "bob-local"}
            assert (await client.get(base)).status_code == 401
            assert (await client.post(base, json=revision_req())).status_code == 401
            before = await client.get(base, headers=alice)
            assert before.status_code == 200
            assert before.json()["report"] is None
            assert before.json()["accepted_current"] is True
            report = await client.post(base, headers=alice, json=revision_req())
            assert report.status_code == 200, report.text
            data = report.json()
            assert data["source_revision"] == 1
            assert data["accepted_revision"] == 1
            assert data["glossary_revision"] == 1
            assert len(data["source_hash"]) == len(data["accepted_hash"]) == 64
            assert data["finding_count"] >= 1
            assert not data["alignment_proven"] and data["diagnostic_only"]
            assert data["findings"][0]["code"] == "ALIGNMENT_UNPROVEN_TERM_QA"
            assert not data["findings"][0]["would_block"]
            finding = data["findings"][0]
            update = dict(
                status="waived", note="Проверено вручную, допустимая форма.",
                expected_review_revision=1, **revision_req(),
            )
            change = await client.patch(
                base + "/findings/" + finding["finding_id"], headers=bob, json=update,
            )
            assert change.status_code == 200, change.text
            reviewed = change.json()["findings"][0]
            assert reviewed["review_status"] == "waived"
            assert reviewed["reviewer_id"] == "bob"
            assert reviewed["review_revision"] == 2
            assert reviewed["review_note"] == update["note"]
            assert (await client.patch(
                base + "/findings/" + finding["finding_id"],
                headers=alice, json=update,
            )).status_code == 409
            assert (await client.patch(
                base + "/findings/" + finding["finding_id"],
                headers=alice, json={**update, "expected_review_revision": 2, "note": ""},
            )).status_code == 400
            # Idempotent repeated scan NEVER resets a human decision.
            repeated = await client.post(base, headers=bob, json=revision_req())
            assert repeated.status_code == 200
            assert repeated.json()["report_id"] == data["report_id"]
            assert repeated.json()["findings"][0]["review_status"] == "waived"
            assert library.get_chapter("novel", "c1")["accepted_html"] == (
                "<p>Коул Ланселот оставил запись.</p>"
            )
            assert (await client.get(base+"/bad",headers=alice)).status_code == 404
            return data["report_id"]

    report_id = asyncio.run(scenario())
    recovered = create_app(app.state.settings, QueueStore(app.state.settings.db_path))
    assert recovered.state.chapter_qa.current("novel", "c1")["report"]["report_id"] == report_id
    with recovered.state.store._connect() as con:
        events = con.execute(
            "SELECT from_status,to_status,actor_id,review_note "
            "FROM chapter_qa_review_events ORDER BY created_at",
        ).fetchall()
    assert len(events) == 1
    assert (events[0]["from_status"], events[0]["to_status"], events[0]["actor_id"]) == (
        "open", "waived", "bob",
    )


def test_accepted_and_glossary_changes_invalidate_review_without_data_loss(tmp_path):
    app, lib = workspace(tmp_path)
    qa = app.state.chapter_qa
    first = qa.scan("novel", "c1", actor_id="alice",
                    **revision_req())
    finding = first["findings"][0]
    qa.review("novel", "c1", finding["finding_id"], status="reviewed",
              expected_review_revision=1, note="Сверено", actor_id="bob",
              **revision_req())
    lib.accept_translation(
        "novel", "c1", "<p>Старик Ланселот оставил запись.</p>",
        expected_revision=1, expected_source_revision=1, actor_id="bob",
    )
    assert qa.current("novel", "c1")["report"] is None
    with pytest.raises(LibraryConflict, match="revision changed"):
        qa.review("novel","c1",finding["finding_id"],status="waived",
                  expected_review_revision=2,note="Old waiver",
                  actor_id="alice",**revision_req())
    second = qa.scan("novel","c1",actor_id="alice",**revision_req(accepted=2))
    assert second["report_id"] != first["report_id"]
    assert all(item["review_status"] == "open" for item in second["findings"])
    lib.put_glossary(
        "novel", glossary={"老蘭斯洛": {"rus": "Ланселот", "note": "Персонаж"}},
        versions={}, authority="READY", expected_revision=1, actor_id="alice",
    )
    assert qa.current("novel", "c1")["report"] is None
    with pytest.raises(LibraryConflict, match="revision changed"):
        qa.review("novel","c1",second["findings"][0]["finding_id"],
                  status="reviewed",note="",expected_review_revision=1,
                  actor_id="bob",**revision_req(accepted=2))
    third = qa.scan("novel","c1",actor_id="bob",**revision_req(accepted=2,glossary=2))
    assert third["report_id"] != second["report_id"]
    with qa.store._connect() as con:
        preserved = con.execute(
            "SELECT id FROM chapter_qa_reports ORDER BY created_at"
        ).fetchall()
        assert len(preserved) == 3


def test_source_change_makes_accepted_translation_stale_and_blocks_qa(tmp_path):
    app, lib = workspace(tmp_path)
    qa = app.state.chapter_qa
    report = qa.scan("novel","c1",actor_id="alice",**revision_req())
    finding = report["findings"][0]
    lib.put_chapter(
        "novel", "c1", chapter_index=1, chapter_path="chapters/c1.xhtml",
        source_html="<p>老蘭斯洛 изменился.</p>", expected_revision=1,
    )
    state = qa.current("novel", "c1")
    assert state["accepted_current"] is False
    assert state["report"] is None
    with pytest.raises(LibraryConflict, match="missing or stale"):
        qa.scan("novel","c1",actor_id="bob",**revision_req(source=2))
    with pytest.raises(LibraryConflict, match="missing or stale"):
        qa.review(
            "novel","c1",finding["finding_id"],status="reviewed",
            expected_review_revision=1,actor_id="bob",**revision_req(source=2),
        )
    with qa.store._connect() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM chapter_qa_reports"
        ).fetchone()[0] == 1


def test_qa_scan_optimistic_cas_rechecks_after_local_gate_before_persist(tmp_path, monkeypatch):
    app, lib = workspace(tmp_path)
    qa = app.state.chapter_qa
    import chaptera_server.chapter_qa as qa_mod
    original = qa_mod.run_translation_gates

    def conflicting_accept(*args, **kwargs):
        lib.accept_translation(
            "novel","c1","<p>Новая ручная редакция.</p>",
            expected_revision=1,expected_source_revision=1,actor_id="alice",
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(qa_mod, "run_translation_gates", conflicting_accept)
    with pytest.raises(LibraryConflict, match="revision changed"):
        qa.scan("novel","c1",actor_id="bob",**revision_req())
    with qa.store._connect() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM chapter_qa_reports"
        ).fetchone()[0] == 0


def test_mismatched_same_revision_hash_fails_closed_not_reuse_waiver(tmp_path):
    app, lib = workspace(tmp_path)
    qa = app.state.chapter_qa
    report = qa.scan("novel","c1",actor_id="alice",**revision_req())
    # Direct DB modification simulates an unsafe external writer, not an
    # authorized API path. A hash fence detects it despite equal revision.
    with qa.store._connect() as con:
        con.execute(
            "UPDATE book_chapters SET accepted_html=? WHERE book_id=? AND chapter_id=?",
            ("<p>Скрытая подмена текста.</p>", "novel", "c1"),
        )
    with pytest.raises(LibraryConflict, match="fingerprints mismatch"):
        qa.current("novel", "c1")
    with pytest.raises(LibraryConflict, match="fingerprints mismatch"):
        qa.scan("novel","c1",actor_id="alice",**revision_req())


def test_qa_findings_inaccessible_without_team_token_or_cross_book_id(tmp_path):
    app, lib = workspace(tmp_path)
    qa = app.state.chapter_qa
    r = qa.scan("novel","c1",actor_id="alice",**revision_req())
    finding = r["findings"][0]
    base = "/v1/books/novel/chapters/c1/qa"
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url="http://test"
        ) as client:
            body = dict(status="reviewed",note="",expected_review_revision=1,
                        **revision_req())
            assert (await client.patch(
                base+"/findings/"+finding["finding_id"],json=body
            )).status_code == 401
            assert (await client.get(base,headers={"X-API-Key":"fake"})).status_code == 401
            assert (await client.patch(
                "/v1/books/other/chapters/c1/qa/findings/"+finding["finding_id"],
                json=body,headers={"X-API-Key":"bob-local"}
            )).status_code == 404
            assert (await client.patch(
                base+"/findings/not-a-known-id",json=body,
                headers={"X-API-Key":"bob-local"}
            )).status_code == 404
    asyncio.run(check())
