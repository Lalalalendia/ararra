"""RE-06D: only complete, current, preview-pinned accepted RU can be exported."""
from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

import chaptera_server.book_export as exporter
from chaptera_server.api import create_app
from chaptera_server.book_export import VerifiedBookExport
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore

ADMIN = {"X-API-Key": "admin-secret"}
MOD = {"X-API-Key": "mod-secret"}


def make_workspace(tmp_path):
    db = tmp_path / "exports.sqlite3"
    settings = Settings(
        db_path=db, team_tokens={"administrator": "admin-secret",
                                 "moderator": "mod-secret"},
        admin_member_id="administrator",
    )
    app = create_app(settings, QueueStore(db))
    lib = app.state.library
    lib.create_book("novel", "Сборник <важный>", owner_member_id="moderator",
                    actor_id="administrator")
    return app, lib


def put_source(lib, number, text=None):
    return lib.put_chapter(
        "novel", f"c{number}", chapter_index=number,
        chapter_path=f"chapters/{number}.xhtml",
        source_html=text or f"<p>原文 {number}</p>", expected_revision=0,
    )


def accept(lib, number, translated_html):
    return lib.accept_translation(
        "novel", f"c{number}", translated_html,
        expected_revision=0, expected_source_revision=1, actor_id="moderator",
    )


def preview_path(last=3):
    return f"/v1/books/novel/export/preview?expected_last_chapter={last}"


def download_path(sha, fmt="txt", last=3):
    return (
        f"/v1/books/novel/export?expected_last_chapter={last}"
        f"&format={fmt}&expected_manifest_sha256={sha}"
    )


def test_end_to_end_exact_accepted_ru_txt_html_and_hash_receipt(tmp_path):
    app, lib = make_workspace(tmp_path)
    for number in range(1, 4):
        put_source(lib, number)
    accept(lib, 1, "<h1>Глава 1</h1><p>Привет <strong>мир</strong>.</p>")
    accept(lib, 2, "<p>Вторая глава.<br>Новая строка.</p>")
    accept(lib, 3, '<p style="color:red" onclick="alert(7)">Третья глава.</p>')

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as c:
            assert (await c.get(preview_path())).status_code == 401
            for headers in (ADMIN, MOD):
                before = await c.get(preview_path(), headers=headers)
                assert before.status_code == 200, before.text
                data = before.json()
                assert data["ready"] and data["source_chapter_count"] == 3
                assert data["ready_chapter_count"] == 3
                assert len(data["manifest_sha256"]) == 64
                assert "Привет" not in before.text and "原文" not in before.text

                txt = await c.get(download_path(data["manifest_sha256"]), headers=headers)
                assert txt.status_code == 200, txt.text
                assert txt.headers["cache-control"] == "no-store, private"
                assert txt.headers["content-disposition"].endswith('.txt"')
                assert txt.headers["x-content-type-options"] == "nosniff"
                assert txt.headers["x-readera-manifest-sha256"] == data["manifest_sha256"]
                assert hashlib.sha256(txt.content).hexdigest() == txt.headers["x-readera-content-sha256"]
                assert txt.text.startswith("Глава 1\n\nПривет мир.")
                assert "\n\nГлава 2\n\nВторая глава." in txt.text
                assert "\n\nГлава 3\n\nТретья глава." in txt.text
                assert txt.text.count("Глава 1") == 1
                assert "原文" not in txt.text and "QA" not in txt.text

                rendered = await c.get(download_path(data["manifest_sha256"], "html"),
                                       headers=headers)
                assert rendered.status_code == 200, rendered.text
                assert rendered.text.startswith("<!doctype html>")
                assert "<h2>Глава 1</h2>" in rendered.text
                assert "<strong>мир</strong>" in rendered.text
                assert "<title>Сборник &lt;важный&gt;</title>" in rendered.text
                assert 'onclick=' not in rendered.text and 'style=' not in rendered.text
                assert rendered.text.count("Глава 1") == 1
                assert hashlib.sha256(rendered.content).hexdigest() == (
                    rendered.headers["x-readera-content-sha256"]
                )
            assert (await c.get(download_path("a" * 64))).status_code == 401
            assert (await c.get(download_path("not-a-sha"), headers=MOD)).status_code == 422
    asyncio.run(scenario())


def test_missing_and_unaccepted_and_extra_chapters_never_return_partial_file(tmp_path):
    app, lib = make_workspace(tmp_path)
    put_source(lib, 1)
    put_source(lib, 3)
    accept(lib, 1, "<p>Одна.</p>")
    assert app.state.store is not None

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as c:
            pre = (await c.get(preview_path(), headers=MOD)).json()
            assert not pre["ready"] and pre["manifest_sha256"] is None
            assert {"code": "missing_source", "chapter_index": 2} in pre["issues"]
            assert {"code": "not_accepted", "chapter_index": 3} in pre["issues"]
            resp = await c.get(download_path("a"*64), headers=MOD)
            assert resp.status_code == 409 and "Перевод" not in resp.text
            short = (await c.get(preview_path(1), headers=MOD)).json()
            assert not short["ready"]
            assert {"code": "source_beyond_declared_end", "chapter_index": 3} in short["issues"]
            assert (await c.get(preview_path(0), headers=MOD)).status_code == 422
            assert (await c.get("/v1/books/missing/export/preview?expected_last_chapter=1",
                                headers=MOD)).status_code == 404
    asyncio.run(scenario())


def test_stale_accepted_revision_and_changed_source_after_preview_fail_closed(tmp_path):
    app, lib = make_workspace(tmp_path)
    put_source(lib, 1)
    accept(lib, 1, "<p>Исходная версия.</p>")

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as c:
            ready = (await c.get(preview_path(1), headers=MOD)).json()
            assert ready["ready"]
            lib.put_chapter("novel", "c1", chapter_index=1,
                            chapter_path="chapters/1.xhtml",
                            source_html="<p>原文 изменён</p>",
                            expected_revision=1)
            stale = (await c.get(preview_path(1), headers=ADMIN)).json()
            assert not stale["ready"]
            assert stale["issues"] == [{"code": "stale_accepted", "chapter_index": 1}]
            blocked = await c.get(download_path(ready["manifest_sha256"],last=1),
                                  headers=MOD)
            assert blocked.status_code == 409
            assert blocked.headers.get("content-disposition") is None
            assert "Исходная версия." not in blocked.text
            # A new accepted revision must first be reviewed and explicitly
            # previewed; the former receipt cannot authorize a new download.
            lib.accept_translation("novel", "c1", "<p>Новая версия.</p>",
                expected_revision=1, expected_source_revision=2,
                actor_id="moderator")
            changed = (await c.get(preview_path(1), headers=MOD)).json()
            assert changed["ready"] and changed["manifest_sha256"] != ready["manifest_sha256"]
            assert (await c.get(download_path(ready["manifest_sha256"],last=1),
                                headers=MOD)).status_code == 409
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    "<p>Текст.</p><!-- QA internal note -->",
    "<p>Текст.</p><script>alert(1)</script>",
    "<p>Текст.</p><a href='https://internal.test'>source link</a>",
    "<p>Текст.</p><span class='qa-note'>Редакторская пометка</span>",
    "<p>Текст. TSEG_12345</p>",
    "<p>Вставлена служебная https://internal.example/qa ссылка.</p>",
    "<h1>Глава 1</h1>",
])
def test_identifiable_service_artifacts_and_unsafe_html_block_export(tmp_path, payload):
    app, lib = make_workspace(tmp_path)
    put_source(lib, 1)
    accept(lib, 1, payload)
    report, _ = VerifiedBookExport(app.state.store).inspect("novel", 1)
    assert not report["ready"]
    assert report["issues"][0]["code"] == "unsafe_accepted_html"


def test_bound_memory_and_declared_tail_are_required(tmp_path, monkeypatch):
    app, lib = make_workspace(tmp_path)
    put_source(lib, 1)
    accept(lib, 1, "<p>Много букв и символов.</p>")
    monkeypatch.setattr(exporter, "MAX_SOURCE_BYTES", 10)
    report, _ = VerifiedBookExport(app.state.store).inspect("novel", 1)
    assert not report["ready"]
    assert report["issues"][0]["code"] == "export_size_or_count_limit"
    monkeypatch.setattr(exporter, "MAX_SOURCE_BYTES", 32*1024*1024)
    # A book with only first chapter cannot claim to contain two chapters.
    report, _ = VerifiedBookExport(app.state.store).inspect("novel", 2)
    assert not report["ready"]
    assert {"code": "missing_source", "chapter_index": 2} in report["issues"]


def test_manipulated_database_bytes_change_manifest_even_without_revision_bump(tmp_path):
    app, lib = make_workspace(tmp_path)
    put_source(lib, 1)
    accept(lib, 1, "<p>Принято.</p>")
    before, _ = VerifiedBookExport(app.state.store).inspect("novel", 1)
    with app.state.store._connect() as con:
        con.execute("UPDATE book_chapters SET accepted_html='<p>Подменено.</p>' "
                    "WHERE book_id='novel' AND chapter_id='c1'")
    with pytest.raises(exporter.ExportNotReady):
        VerifiedBookExport(app.state.store).render(
            "novel", 1, before["manifest_sha256"], "txt")
