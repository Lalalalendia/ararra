"""Full Cycle App v1 ZIP => operator preview => atomic canonical book import.

Source files are already split into chapters by Full Cycle's desktop parser.
Never split TXT bodies again, infer an author number from the TOC position,
overwrite a chapter silently or lose the parser-original copy.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
from zipfile import ZipFile, ZIP_DEFLATED

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.book_import import ImportFormatError, parse_book_import, preview_book_import
from chaptera_server.config import Settings
from chaptera_server.import_sessions import ImportSessions, StageConflict
from chaptera_server.library import BookLibrary, LibraryConflict
from chaptera_server.storage import QueueStore


def sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def archive(*, chapters=None, kind="source", tamper=None, extra=None) -> bytes:
    if chapters is None:
        chapters = [
            (1, "Пролог", "本章 начинается здесь.", "本章 после ручной правки."),
            (3, "Интерлюдия: 一二", "第二部分", "第二部分 исправлена."),
            (5, "Глава без номера", "第五部分", "第五部分"),
        ]
    manifest = {
        "format": "full_cycle_app.chapter_exchange",
        "version": 1,
        "kind": kind,
        "created_at": "2026-10-27T00:00:00Z",
        "project": {"id": 41, "title": "Тестовая книга", "rulate_book_id": 17},
        "source": {
            "id": 9, "source_url": "https://example.test/novel/41",
            "domain": "example.test", "external_book_id": "41",
        },
        "chapters": [],
        "chapter_count": len(chapters),
    }
    files = {"README.txt": "Full Cycle App original export"}
    for index, title, original, working in chapters:
        w = f"chapters/{index:06d}.txt"
        o = f"original/{index:06d}.txt"
        manifest["chapters"].append({
            "chapter_index": index,
            "chapter_title": title,
            "chapter_url": f"https://example.test/novel/41/{index}",
            "working_file": w,
            "original_file": o,
            "working_sha256": sha(working),
            "original_sha256": sha(original),
            "had_edited_copy": working != original,
            "had_normalized_copy": False,
        })
        files[w] = working
        files[o] = original
    if tamper:
        tamper(manifest, files)
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False)
    if extra:
        files.update(extra)
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as zf:
        for name, value in files.items():
            zf.writestr(name, value.encode("utf-8") if isinstance(value, str) else value)
    return output.getvalue()


def create_decision(seq, index, *, action="create", revision=0):
    if action == "skip":
        return {"sequence": seq, "action": action}
    return {
        "sequence": seq,
        "action": action,
        "chapter_id": f"c{index}",
        "chapter_index": index,
        "chapter_path": f"chapters/{index}.xhtml",
        "expected_revision": revision,
    }


def confirm_request(*, mode="create", actions=None):
    return {
        "book_id": "demo", "book_mode": mode,
        "title": "Тестовая книга", "prefix_policy": "reject",
        "chapters": actions or [create_decision(1, 1), create_decision(2, 3),
                                create_decision(3, 5)],
    }


def test_zip_manifest_is_authoritative_not_txt_heading_heuristics():
    preview, chapters, prefix = parse_book_import("Full Cycle.zip", archive())
    assert preview["kind"] == "book"
    assert preview["format"] == "full_cycle_app.chapter_exchange"
    assert preview["title"] == "Тестовая книга"
    assert preview["chapter_count"] == preview["story_chapter_count"] == 3
    assert [r["title"] for r in preview["chapters"]] == [
        "Пролог", "Интерлюдия: 一二", "Глава без номера",
    ]
    assert [r["detected_number"] for r in preview["chapters"]] == [None] * 3
    assert [r["suggested_index"] for r in preview["chapters"]] == [1, 3, 5]
    assert [r["destination_position"] for r in preview["chapters"]] == [1, 3, 5]
    assert preview["archive_source"]["source_coverage_verified"] is False
    assert preview["archive_source"]["missing_position_ranges"] == [[2, 2], [4, 4]]
    assert any("Gaps in exported" in w for w in preview["warnings"])
    assert any("TOC completeness is not verified" in w for w in preview["warnings"])
    assert "original_text" not in json.dumps(preview, ensure_ascii=False)
    assert prefix == ""
    assert "本章 начинается" not in chapters[0].html
    assert "本章 после ручной правки" in chapters[0].html
    assert chapters[0].exchange["original_text"] == "本章 начинается здесь."


def test_stage_confirm_preserves_original_working_and_exact_manifest_order(tmp_path):
    store = QueueStore(tmp_path / "exchange.sqlite3")
    library = BookLibrary(store)
    sessions = ImportSessions(store)
    data = archive()
    staged = sessions.stage("alice", "Книга.ZIP", data, idempotency_key="fullcycle-1")
    assert staged["status"] == "staged"
    assert staged["preview"]["chapter_count"] == 3
    assert staged["preview"]["chapters"][1]["source_position"] == 3
    assert "本章 начинается" not in json.dumps(staged, ensure_ascii=False)
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM book_import_source_originals").fetchone()[0] == 0
    reopened = ImportSessions(QueueStore(store.db_path))
    assert reopened.get("alice", staged["stage_id"])["status"] == "staged"
    request = confirm_request(actions=[
        create_decision(1, 1), create_decision(2, 3), create_decision(3, 5, action="skip")
    ])
    receipt = reopened.confirm("alice", staged["stage_id"], request)
    assert receipt["imported_count"] == 2 and receipt["skipped_count"] == 1
    assert receipt == reopened.confirm("alice", staged["stage_id"], request)
    chapter = library.get_chapter("demo", "c1")
    assert chapter["chapter_index"] == 1
    assert "<h1>Пролог</h1>" in chapter["source_html"]
    assert "本章 после ручной правки." in chapter["source_html"]
    original = reopened.source_originals("demo", "c1")
    assert len(original) == 1
    assert original[0]["original_text"] == "本章 начинается здесь."
    assert original[0]["working_text"] == "本章 после ручной правки."
    assert original[0]["original_sha256"] == sha("本章 начинается здесь.")
    assert original[0]["working_sha256"] == sha("本章 после ручной правки.")
    assert original[0]["chapter_url"] == "https://example.test/novel/41/1"
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM book_import_source_originals").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM book_chapters").fetchone()[0] == 2
        assert con.execute("SELECT payload_json FROM book_import_stages").fetchone()[0] is None


def test_failed_batch_does_not_leave_originals_or_half_book(tmp_path):
    store = QueueStore(tmp_path / "rollback.sqlite3")
    BookLibrary(store)
    sessions = ImportSessions(store)
    staged = sessions.stage("alice", "book.zip", archive())
    bad = confirm_request(actions=[
        create_decision(1, 1), create_decision(2, 1), create_decision(3, 5)
    ])
    with pytest.raises(StageConflict, match="batch maps"):
        sessions.confirm("alice", staged["stage_id"], bad)
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM book_import_source_originals").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM book_chapters").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0


def test_explicit_replacement_requires_revision_and_never_deletes_old_original(tmp_path):
    store = QueueStore(tmp_path / "replace.sqlite3")
    lib = BookLibrary(store)
    sessions = ImportSessions(store)
    staged = sessions.stage("alice", "one.zip",
                            archive(chapters=[(1, "Пролог", "Оригинал", "Рабочая")]))
    req = confirm_request(actions=[create_decision(1, 1)])
    sessions.confirm("alice", staged["stage_id"], req)
    lib.accept_translation("demo", "c1", "<p>Принятый перевод</p>",
                           expected_revision=0, expected_source_revision=1, actor_id="alice")
    newer = sessions.stage(
        "alice", "two.zip", archive(chapters=[(1, "Пролог", "Другой оригинал", "Новая рабочая")]),
    )
    updated = confirm_request(
        mode="existing", actions=[create_decision(1, 1, action="replace", revision=1)]
    )
    lib.put_chapter("demo", "c1", chapter_index=1, chapter_path="chapters/1.xhtml",
                    source_html="<p>Конкурирующая правка</p>", expected_revision=1)
    with pytest.raises(LibraryConflict, match="revision"):
        sessions.confirm("alice", newer["stage_id"], updated)
    assert len(sessions.source_originals("demo", "c1")) == 1
    updated["chapters"][0]["expected_revision"] = 2
    sessions.confirm("alice", newer["stage_id"], updated)
    after = lib.get_chapter("demo", "c1")
    assert after["accepted_html"] == "<p>Принятый перевод</p>"
    assert after["accepted_current"] is False
    assert after["source_revision"] == 3
    assert [x["original_text"] for x in sessions.source_originals("demo", "c1")] == [
        "Оригинал", "Другой оригинал",
    ]


@pytest.mark.parametrize("mutation,error", [
    (lambda m,f: m.update(kind="translation"), "source archive"),
    (lambda m,f: m.update(version=2), "source archive"),
    (lambda m,f: m.update(chapter_count=100), "chapter count"),
    (lambda m,f: m["chapters"][0].update(chapter_index=3), "Duplicate"),
    (lambda m,f: m["chapters"][0].update(working_file="../private"), "file references"),
    (lambda m,f: m["chapters"][0].update(chapter_url="file:///secret"), "invalid URL"),
    (lambda m,f: f.update({"chapters/000001.txt": "tampered"}), "SHA-256 mismatch"),
    (lambda m,f: f.pop("original/000001.txt"), "missing or corrupt"),
    (lambda m,f: f.update({"injected/hidden.txt": "junk"}), "unexpected"),
    (lambda m,f: m["chapters"][0].update(original_sha256="xx"), "Invalid original_sha256"),
])
def test_corrupt_ambiguous_or_wrong_kind_archives_are_rejected(mutation, error):
    with pytest.raises(ImportFormatError, match=error):
        preview_book_import("book.zip", archive(tamper=mutation))


def test_duplicate_zip_members_and_unsafe_paths_are_rejected():
    original = archive()
    with ZipFile(io.BytesIO(original)) as zf:
        parts = {name: zf.read(name) for name in zf.namelist()}
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as zf:
        for key, value in parts.items():
            zf.writestr(key, value)
        zf.writestr("manifest.json", parts["manifest.json"])
    with pytest.raises(ImportFormatError, match="Duplicate ZIP"):
        preview_book_import("book.zip", output.getvalue())
    with pytest.raises(ImportFormatError, match="unsafe"):
        preview_book_import("book.zip", archive(extra={"../malicious": "data"}))


def test_http_preview_stage_confirm_and_download_originals(tmp_path):
    store = QueueStore(tmp_path / "api.sqlite3")
    app = create_app(Settings(
        db_path=store.db_path, team_tokens={"chief": "chief-key", "alice": "alice-key"},
        admin_member_id="chief", browser_session_secure=False,
    ), store)
    data = archive(chapters=[(1, "Пролог", "Оригинал HTTP", "Рабочая HTTP")])
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as client:
            files = {"file": ("source.zip", data, "application/zip")}
            assert (await client.post("/v1/imports/preview", files=files)).status_code == 401
            preview = await client.post("/v1/imports/preview", files=files,
                                        headers={"X-API-Key": "alice-key"})
            assert preview.status_code == 200, preview.text
            assert preview.json()["chapters"][0]["destination_position"] == 1
            with store._connect() as con:
                assert con.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
            staged = await client.post("/v1/imports/stages", files=files,
                                       headers={"X-API-Key": "alice-key"})
            assert staged.status_code == 201, staged.text
            stage_id = staged.json()["stage_id"]
            confirm = await client.post(
                f"/v1/imports/stages/{stage_id}/confirm",
                json=confirm_request(actions=[create_decision(1, 1)]),
                headers={"X-API-Key": "alice-key"},
            )
            assert confirm.status_code == 200, confirm.text
            path = "/v1/imports/books/demo/chapters/c1/source-originals"
            assert (await client.get(path)).status_code == 401
            response = await client.get(path, headers={"X-API-Key": "alice-key"})
            assert response.status_code == 200, response.text
            originals = response.json()["originals"]
            assert originals[0]["original_text"] == "Оригинал HTTP"
            assert originals[0]["working_text"] == "Рабочая HTTP"
    asyncio.run(scenario())
