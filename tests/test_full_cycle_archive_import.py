"""RE-02: Full Cycle App v1 ZIP import uses manifest boundaries, never regex.

All fixtures are synthetic and offline; no Gemini, source URLs, Google Drive or
production SQLite. The original bytes and working text survive explicit commit.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.book_import import ImportFormatError, parse_book_import, preview_book_import
from chaptera_server.config import Settings
from chaptera_server.full_cycle_archive import MAX_EXCHANGE_UPLOAD_BYTES
from chaptera_server.import_sessions import ImportSessions, StageConflict
from chaptera_server.library import BookLibrary, LibraryConflict, LibraryNotFound
from chaptera_server.storage import QueueStore


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def example_rows() -> list[dict]:
    return [
        {"index": 7, "title": "Ночь наступила", "url": "https://stories.example/chapter/7",
         "original": "原文七\n第999章 внутри текста, но НЕ начало главы.",
         "working": "原文七\n第999章 внутри текста, но НЕ начало главы."},
        {"index": 9, "title": "Ночь наступила", "url": "https://stories.example/chapter/9",
         "original": "原文九.\n\nАбзац два.",
         "working": "手工 исправленный текст.\n\nАбзац два."},
        {"index": 10, "title": "Эпилог / 番外", "url": "https://stories.example/chapter/end",
         "original": "作者的话\n\nЭто полный текст.",
         "working": "作者的话\n\nЭто полный текст."},
    ]


def build_source_zip(rows=None, *, modify_manifest=None, extras=None) -> bytes:
    rows = example_rows() if rows is None else rows
    manifest = {
        "format": "full_cycle_app.chapter_exchange",
        "version": 1,
        "kind": "source",
        "project": {"id": 91, "title": "История", "rulate_book_id": 12345},
        "source": {
            "id": 80, "source_url": "https://stories.example/book/80",
            "domain": "stories.example", "external_book_id": "80",
        },
        "chapter_count": len(rows),
        "chapters": [],
    }
    members = {"README.txt": "Full Cycle App source export\n"}
    for row in rows:
        index = row["index"]
        working_file = f"chapters/{index:06d}.txt"
        original_file = f"original/{index:06d}.txt"
        members[working_file] = row["working"].encode("utf-8")
        members[original_file] = row["original"].encode("utf-8")
        manifest["chapters"].append({
            "chapter_index": index,
            "chapter_title": row["title"],
            "chapter_url": row["url"],
            "working_file": working_file,
            "original_file": original_file,
            "working_sha256": sha(row["working"]),
            "original_sha256": sha(row["original"]),
            "had_edited_copy": row["working"] != row["original"],
            "had_normalized_copy": False,
            "normalization_status": "",
            "normalization_meta_json": "{}",
            "normalized_at": "",
        })
    if modify_manifest:
        modify_manifest(manifest)
    if extras:
        members.update(extras)
    members["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return stream.getvalue()


def choose(seq, index, action="create", expected=0):
    return {
        "sequence": seq, "action": action,
        "chapter_id": f"ch{index}", "chapter_index": index,
        "chapter_path": f"chapters/{index}.xhtml",
        "expected_revision": expected,
    }


def confirm_payload(*, mode="create", items=None):
    return {
        "book_id": "imported", "book_mode": mode,
        "title": "История" if mode == "create" else None,
        "prefix_policy": "reject",
        "chapters": items if items is not None
            else [choose(1, 7), choose(2, 9), choose(3, 10)],
    }


def make_store(tmp_path):
    store = QueueStore(tmp_path / "source-zip.sqlite3")
    lib = BookLibrary(store)
    return store, lib, ImportSessions(store)


def test_archive_manifest_is_toc_not_heading_recognition():
    preview, parsed, prefix = parse_book_import("История.zip", build_source_zip())
    assert preview["format"] == "zip" and preview["kind"] == "book"
    assert preview["title"] == "История"
    assert preview["chapter_count"] == 3
    assert preview["story_chapter_count"] == 3
    assert preview["source_archive"]["boundaries"] == "manifest"
    assert preview["source_archive"]["missing_positions"] == [[8, 8]]
    assert [c["title"] for c in preview["chapters"]] == [
        "Ночь наступила", "Ночь наступила", "Эпилог / 番外",
    ]
    assert [c["suggested_index"] for c in preview["chapters"]] == [7, 9, 10]
    assert [c["destination_position"] for c in preview["chapters"]] == [7, 9, 10]
    assert [c["detected_number"] for c in preview["chapters"]] == [None] * 3
    assert [c["source_url"] for c in preview["chapters"]] == [r["url"] for r in example_rows()]
    assert all(item["has_text"] for item in preview["chapters"])
    assert prefix == ""
    assert len(parsed) == 3 and "第999章" in parsed[0].html
    assert parsed[1].archive["original_text"].startswith("原文九")
    assert parsed[1].archive["working_text"].startswith("手工")
    assert any("full source TOC" in warning for warning in preview["warnings"])
    assert any("internal gaps" in warning for warning in preview["warnings"])
    public = json.dumps(preview, ensure_ascii=False)
    assert "source_html" not in public
    assert "原文九." not in public
    assert preview["chapters"][1]["sample"].startswith("手工 исправленный")


@pytest.mark.parametrize("change,error", [
    (lambda m: m.update(kind="translation"), "source archive v1"),
    (lambda m: m.update(version=2), "source archive v1"),
    (lambda m: m.update(format="untrusted"), "source archive v1"),
    (lambda m: m.update(chapter_count=999), "chapter count"),
    (lambda m: m["chapters"][1].update(chapter_index=7), "Duplicate source"),
    (lambda m: m["chapters"][0].update(working_file="original/000007.txt"), "mismatched file"),
    (lambda m: m["chapters"][0].update(original_sha256="f" * 64), "SHA-256"),
    (lambda m: m["chapters"][0].update(working_sha256="not-a-hash"), "working_sha256"),
    (lambda m: m["chapters"][1].update(chapter_title=7), "chapter title"),
])
def test_reject_forged_or_inconsistent_manifest(change, error):
    with pytest.raises(ImportFormatError, match=error):
        preview_book_import("book.zip", build_source_zip(modify_manifest=change))


def test_reject_missing_extra_duplicate_and_traversal_members():
    payload = build_source_zip(extras={"../escape.txt": "payload"})
    with pytest.raises(ImportFormatError, match="Unsafe"):
        preview_book_import("book.zip", payload)
    payload = build_source_zip(extras={"chapters/000042.txt": "unlisted"})
    with pytest.raises(ImportFormatError, match="unreferenced"):
        preview_book_import("book.zip", payload)
    # zipfile can contain duplicate physical members; reading by name is ambiguous.
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as target:
        target.writestr("manifest.json", "{}")
        target.writestr("manifest.json", "{}")
    with pytest.raises(ImportFormatError, match="Duplicate"):
        preview_book_import("dup.zip", stream.getvalue())
    empty_manifest = io.BytesIO()
    with zipfile.ZipFile(empty_manifest, "w") as target:
        target.writestr("README.txt", "no manifest")
    with pytest.raises(ImportFormatError, match="manifest.json"):
        preview_book_import("book.zip", empty_manifest.getvalue())
    with pytest.raises(ImportFormatError, match="24 MiB"):
        preview_book_import("book.zip", b"x" * (MAX_EXCHANGE_UPLOAD_BYTES + 1))


def test_missing_or_invalid_body_fails_before_stage_storage(tmp_path):
    store, _, session = make_store(tmp_path)
    rows = example_rows()
    rows[0]["working"] = ""
    with pytest.raises(ImportFormatError, match="empty or invalid"):
        session.stage("alice", "empty.zip", build_source_zip(rows))
    rows = example_rows()
    # Construct a valid ZIP whose manifest references a chapter that is absent.
    archive = build_source_zip(rows)
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as old, zipfile.ZipFile(
        stream, "w", zipfile.ZIP_DEFLATED
    ) as target:
        for item in old.infolist():
            if item.filename != "original/000009.txt":
                target.writestr(item.filename, old.read(item))
    with pytest.raises(ImportFormatError, match="missing or shared"):
        session.stage("alice", "missing.zip", stream.getvalue())
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM book_import_stages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0


def test_stage_explicit_create_preserves_both_source_bytes_and_revisions(tmp_path):
    store, lib, session = make_store(tmp_path)
    archive = build_source_zip()
    stage = session.stage("alice", "История.zip", archive, idempotency_key="fca-book")
    assert stage["preview"]["chapter_count"] == 3
    assert session.stage("alice", "История.zip", archive, idempotency_key="fca-book")["stage_id"] == stage["stage_id"]
    assert session.get("alice", stage["stage_id"])["status"] == "staged"
    assert "原文九" not in json.dumps(stage, ensure_ascii=False)
    with pytest.raises(StageConflict, match="Every staged"):
        session.confirm("alice", stage["stage_id"], confirm_payload(
            items=[choose(1, 7), choose(2, 9)],
        ))
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
    request = confirm_payload(items=[
        choose(1, 7), choose(2, 9), {"sequence": 3, "action": "skip"},
    ])
    receipt = session.confirm("alice", stage["stage_id"], request)
    assert receipt["imported_count"] == 2 and receipt["skipped_count"] == 1
    assert receipt == session.confirm("alice", stage["stage_id"], request)
    assert "Ночь наступила" in lib.get_chapter("imported", "ch7")["source_html"]
    assert "第999章" in lib.get_chapter("imported", "ch7")["source_html"]
    assert "手工 исправленный текст" in lib.get_chapter("imported", "ch9")["source_html"]
    assert lib.get_chapter("imported", "ch9")["chapter_index"] == 9
    original = session.archive_original("imported", "ch9")
    assert original["source_revision"] == 1
    assert original["source_index"] == 9
    assert original["source_url"] == example_rows()[1]["url"]
    assert original["original_text"] == example_rows()[1]["original"]
    assert original["working_text"] == example_rows()[1]["working"]
    assert original["original_sha256"] == sha(original["original_text"])
    assert original["working_sha256"] == sha(original["working_text"])
    with pytest.raises(LibraryNotFound):
        session.archive_original("imported", "ch10")
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM book_import_exchange_originals").fetchone()[0] == 2
        assert db.execute("SELECT payload_json FROM book_import_stages").fetchone()[0] is None


def test_revision_conflict_and_immutable_original_history(tmp_path):
    store, lib, session = make_store(tmp_path)
    first = session.stage("alice", "source.zip", build_source_zip())
    session.confirm("alice", first["stage_id"], confirm_payload())
    lib.accept_translation(
        "imported", "ch7", "<p>Редакция ChatGPT</p>",
        expected_revision=0, expected_source_revision=1, actor_id="alice",
    )
    changed = example_rows()
    changed[0]["original"] = "新的原始文本"
    changed[0]["working"] = "Новый исходный текст после нормализации"
    staged = session.stage("alice", "updated.zip", build_source_zip(changed))
    replacement = confirm_payload(mode="existing", items=[
        choose(1, 7, action="replace", expected=1),
        {"sequence": 2, "action": "skip"},
        {"sequence": 3, "action": "skip"},
    ])
    # Another operator updates the source: CAS must reject the stale ZIP.
    lib.put_chapter(
        "imported", "ch7", chapter_index=7, chapter_path="chapters/7.xhtml",
        source_html="<p>Чужая правка</p>", expected_revision=1,
    )
    with pytest.raises(LibraryConflict, match="revision"):
        session.confirm("alice", staged["stage_id"], replacement)
    replacement["chapters"][0]["expected_revision"] = 2
    assert session.confirm("alice", staged["stage_id"], replacement)["imported_count"] == 1
    assert lib.get_chapter("imported", "ch7")["source_revision"] == 3
    assert lib.get_chapter("imported", "ch7")["accepted_revision"] == 1
    assert lib.get_chapter("imported", "ch7")["accepted_current"] is False
    assert "Редакция ChatGPT" in lib.get_chapter("imported", "ch7")["accepted_html"]
    assert session.archive_original("imported", "ch7", 1)["original_text"] == example_rows()[0]["original"]
    assert session.archive_original("imported", "ch7", 3)["original_text"] == "新的原始文本"
    assert session.archive_original("imported", "ch7")["source_revision"] == 3
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM book_import_exchange_originals WHERE chapter_id='ch7'"
        ).fetchone()[0] == 2

    # Identical rendered source cannot be assigned another raw original at the
    # same revision; no-op replacement must preserve old provenance.
    collision = example_rows()
    collision[0]["working"] = changed[0]["working"]
    collision[0]["original"] = "未经过确认的替换原文"
    stage2 = session.stage("alice", "other.zip", build_source_zip(collision))
    with pytest.raises(StageConflict, match="different source checkpoint"):
        session.confirm("alice", stage2["stage_id"], confirm_payload(mode="existing", items=[
            choose(1, 7, action="replace", expected=3),
            {"sequence": 2, "action": "skip"},
            {"sequence": 3, "action": "skip"},
        ]))
    assert session.archive_original("imported", "ch7", 3)["original_text"] == "新的原始文本"


def test_http_zip_preview_stage_confirm_and_readback_after_restart(tmp_path):
    store = QueueStore(tmp_path / "http-exchange.sqlite3")
    settings = Settings(
        db_path=store.db_path,
        team_tokens={"chief": "chief-secret", "alice": "alice-secret", "bob": "bob-secret"},
        admin_member_id="chief",
    )
    app = create_app(settings, store)
    headers = {"X-API-Key": "alice-secret"}
    archive = build_source_zip()
    file = {"file": ("story.zip", archive, "application/zip")}
    async def flow():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            assert (await client.post("/v1/imports/preview", files=file)).status_code == 401
            response = await client.post("/v1/imports/preview", files=file, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["source_archive"]["boundaries"] == "manifest"
            assert response.json()["stored"] is False
            with store._connect() as db:
                assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
            uploaded = await client.post(
                "/v1/imports/stages", files=file,
                headers={**headers, "Idempotency-Key": "browser-import"},
            )
            assert uploaded.status_code == 201, uploaded.text
            stage_id = uploaded.json()["stage_id"]
            assert uploaded.json()["preview"]["chapter_count"] == 3
            assert (await client.get(
                f"/v1/imports/stages/{stage_id}",
                headers={"X-API-Key": "bob-secret"},
            )).status_code == 404
            request = {**confirm_payload(), "owner_member_id": "alice"}
            complete = await client.post(
                f"/v1/imports/stages/{stage_id}/confirm",
                json=request, headers=headers,
            )
            assert complete.status_code == 200, complete.text
            assert complete.json()["imported_count"] == 3
            url = "/v1/imports/books/imported/chapters/ch9/archive-original"
            assert (await client.get(url)).status_code == 401
            found = await client.get(url, headers=headers)
            assert found.status_code == 200, found.text
            assert found.json()["original_text"] == example_rows()[1]["original"]
            assert (await client.get(url + "?source_revision=2", headers=headers)).status_code == 404
            assert (await client.get(url + "?source_revision=0", headers=headers)).status_code == 422
    asyncio.run(flow())
    app2 = create_app(settings, QueueStore(store.db_path))
    assert app2.state.import_sessions.archive_original("imported", "ch7")["source_index"] == 7
