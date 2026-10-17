"""RE-02: durable imports must not silently lose chapters or editor revisions."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.import_sessions import (
    ImportSessions, StageConflict, StageExpired, StageMissing, StageTooLarge,
)
from chaptera_server.library import BookLibrary, LibraryConflict, LibraryNotFound
from chaptera_server.storage import QueueStore


HEADERS = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


def make_store(tmp_path):
    store = QueueStore(tmp_path / "import-test.sqlite3")
    lib = BookLibrary(store)
    return store, lib, ImportSessions(store)


def source(prefix=""):
    return (prefix + "Глава 12. Первая\nПервое предложение.\n\n"
            "Глава 13. Вторая\nВторое предложение.\n").encode("utf-8")


def decision(sequence: int, chapter: str, index: int, *, mode="create", expected=0, path=None):
    return {
        "sequence": sequence, "action": mode, "chapter_id": chapter,
        "chapter_index": index,
        "chapter_path": path or f"chapters/{index}.xhtml",
        "expected_revision": expected,
    }


def request(choices, *, book_id="demo", book_mode="create", title="Новая книга", prefix_policy="reject"):
    return {
        "book_id": book_id, "book_mode": book_mode, "title": title,
        "prefix_policy": prefix_policy,
        "chapters": choices,
    }


def test_stage_and_commit_survive_restart_with_full_source_and_preserved_preface(tmp_path):
    store, lib, sessions = make_store(tmp_path)
    data = source("Вступление до первой главы.\n\n")
    staged = sessions.stage("alice", "роман.txt", data, idempotency_key="source-12")
    assert staged["status"] == "staged"
    assert staged["preview"]["stored"] is False
    assert staged["preview"]["chapter_count"] == 2
    assert staged["unassigned_prefix_saved"]
    assert "source_html" not in json.dumps(staged, ensure_ascii=False)
    reopened = ImportSessions(QueueStore(store.db_path))
    view = reopened.get("alice", staged["stage_id"])
    assert "Вступление до" in view["unassigned_prefix_html"]
    with pytest.raises(StageMissing):
        reopened.get("bob", staged["stage_id"])
    payload = request([decision(1, "ch12", 12), decision(2, "ch13", 13)],
                      prefix_policy="preserve")
    committed = reopened.confirm("alice", staged["stage_id"], payload)
    assert committed["imported_count"] == 2
    assert committed["unassigned_prefix_preserved"] is True
    assert committed["created_book"] is True
    first = lib.get_chapter("demo", "ch12")
    assert "Глава 12. Первая" in first["source_html"]
    assert "Первое предложение" in first["source_html"]
    assert lib.get_chapter("demo", "ch13")["chapter_index"] == 13
    assert "Вступление до первой главы" in reopened.prefixes("demo")[0]["source_html"]
    assert reopened.get("alice", staged["stage_id"])["status"] == "committed"
    assert reopened.confirm("alice", staged["stage_id"], payload) == committed
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM book_chapters").fetchone()[0] == 2
        assert con.execute("SELECT payload_json FROM book_import_stages").fetchone()[0] is None


def test_every_candidate_needs_explicit_decision_and_missing_preface_policy_is_rejected(tmp_path):
    _, lib, sessions = make_store(tmp_path)
    staged = sessions.stage("alice", "sample.txt", source("Предисловие.\n\n"))
    ident = staged["stage_id"]
    with pytest.raises(StageConflict, match="Every staged"):
        sessions.confirm("alice", ident, request([decision(1, "a", 12)]))
    with pytest.raises(StageConflict, match="preface"):
        sessions.confirm("alice", ident, request([decision(1, "a", 12), decision(2, "b", 13)]))
    payload = request([decision(1, "a", 12), {"sequence": 2, "action": "skip"}],
                      prefix_policy="discard")
    result = sessions.confirm("alice", ident, payload)
    assert (result["imported_count"], result["skipped_count"]) == (1, 1)
    assert result["unassigned_prefix_preserved"] is False
    with pytest.raises(LibraryNotFound):
        lib.get_chapter("demo", "b")
    assert lib.get_chapter("demo", "a")["source_revision"] == 1


def test_duplicate_headings_require_mapping_and_any_failure_rolls_back_whole_batch(tmp_path):
    store, _, sessions = make_store(tmp_path)
    original = "Глава 1\nТекст A.\nГлава 1\nТекст B.\n".encode()
    staged = sessions.stage("alice", "doubles.txt", original)
    assert any("Duplicate chapter number" in x for x in staged["preview"]["warnings"])
    bad = request([decision(1, "a", 1), decision(2, "b", 1)])
    with pytest.raises(StageConflict, match="batch maps"):
        sessions.confirm("alice", staged["stage_id"], bad)
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM book_chapters").fetchone()[0] == 0
    good = request([decision(1, "a", 1), decision(2, "b", 2)])
    assert sessions.confirm("alice", staged["stage_id"], good)["imported_count"] == 2


def test_two_editors_cannot_overwrite_stale_source_or_accepted_revision(tmp_path):
    store, lib, sessions = make_store(tmp_path)
    lib.create_book("demo", "Новая книга")
    lib.put_chapter("demo", "c12", chapter_index=12, chapter_path="chapters/12.xhtml",
                    source_html="<p>Исходник 1</p>", expected_revision=0)
    lib.accept_translation("demo", "c12", "<p>Принятый перевод</p>",
                           expected_revision=0, expected_source_revision=1, actor_id="bob")
    staged = sessions.stage("alice", "edit.txt", "Глава 12\nНовый исходный текст".encode())
    payload = request([decision(1, "c12", 12, mode="replace", expected=1)],
                      book_mode="existing", title="Новая книга")
    # A second editor edits the source AFTER stage creation.
    lib.put_chapter("demo", "c12", chapter_index=12, chapter_path="chapters/12.xhtml",
                    source_html="<p>Версия второго редактора</p>", expected_revision=1)
    with pytest.raises(LibraryConflict, match="revision"):
        sessions.confirm("alice", staged["stage_id"], payload)
    assert lib.get_chapter("demo", "c12")["source_revision"] == 2
    payload["chapters"][0]["expected_revision"] = 2
    result = sessions.confirm("alice", staged["stage_id"], payload)
    assert result["chapters"][0]["source_revision"] == 3
    after = lib.get_chapter("demo", "c12")
    assert after["accepted_current"] is False
    assert after["accepted_html"] == "<p>Принятый перевод</p>"
    assert after["accepted_revision"] == 1
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM book_accepted_revisions").fetchone()[0] == 1


def test_index_or_path_collision_with_other_chapter_aborts_batch(tmp_path):
    store, lib, sessions = make_store(tmp_path)
    lib.create_book("demo", "Новая книга")
    lib.put_chapter("demo", "existing", chapter_index=13, chapter_path="chapters/13.xhtml",
                    source_html="<p>Не трогать</p>", expected_revision=0)
    staged = sessions.stage("alice", "book.txt", source())
    selected = request([decision(1, "new", 12), decision(2, "other", 13)],
                       book_mode="existing", title="Новая книга")
    with pytest.raises(LibraryConflict, match="owned"):
        sessions.confirm("alice", staged["stage_id"], selected)
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM book_chapters WHERE book_id='demo'").fetchone()[0] == 1
    with pytest.raises(LibraryNotFound):
        lib.get_chapter("demo", "new")
    selected["chapters"][1]["chapter_index"] = 14
    selected["chapters"][1]["chapter_path"] = "chapters/13.xhtml"
    with pytest.raises(LibraryConflict, match="owned"):
        sessions.confirm("alice", staged["stage_id"], selected)
    selected["chapters"][1]["chapter_path"] = "chapters/14.xhtml"
    assert sessions.confirm("alice", staged["stage_id"], selected)["imported_count"] == 2



def test_actual_sqlite_write_failure_rolls_back_book_and_first_chapter(tmp_path):
    store, _, sessions = make_store(tmp_path)
    staged = sessions.stage("alice", "transaction.txt", source())
    with store._connect() as db:
        db.execute("""CREATE TRIGGER simulate_second_write_failure
                      BEFORE INSERT ON book_chapters
                      WHEN NEW.chapter_id='b'
                      BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    payload = request([decision(1, "a", 12), decision(2, "b", 13)])
    with pytest.raises(LibraryConflict, match="unavailable"):
        sessions.confirm("alice", staged["stage_id"], payload)
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM book_chapters").fetchone()[0] == 0
        assert db.execute("SELECT status FROM book_import_stages").fetchone()[0] == "staged"
        db.execute("DROP TRIGGER simulate_second_write_failure")
    assert sessions.confirm("alice", staged["stage_id"], payload)["imported_count"] == 2


def test_idempotency_replay_upload_and_confirmation_conflict(tmp_path):
    _, _, sessions = make_store(tmp_path)
    first = sessions.stage("alice", "book.txt", source(), idempotency_key="k")
    assert sessions.stage("alice", "book.txt", source(), idempotency_key="k")["stage_id"] == first["stage_id"]
    with pytest.raises(StageConflict, match="Idempotency"):
        sessions.stage("alice", "book.txt", source().replace(b"12", b"11"), idempotency_key="k")
    payload = request([decision(1, "a", 12), decision(2, "b", 13)])
    committed = sessions.confirm("alice", first["stage_id"], payload)
    assert sessions.confirm("alice", first["stage_id"], payload) == committed
    bad = request([decision(1, "a", 12), {"sequence": 2, "action": "skip"}])
    with pytest.raises(StageConflict, match="Already committed"):
        sessions.confirm("alice", first["stage_id"], bad)


def test_expired_session_is_not_committable_and_active_quota_is_bounded(tmp_path):
    store, _, sessions = make_store(tmp_path)
    ids = [sessions.stage("alice", f"file{i}.txt", source())["stage_id"] for i in range(3)]
    with pytest.raises(StageTooLarge, match="Too many"):
        sessions.stage("alice", "fourth.txt", source())
    with store._connect() as db:
        db.execute("UPDATE book_import_stages SET expires_at=? WHERE id=?", (time.time() - 1, ids[0]))
    with pytest.raises(StageExpired):
        sessions.get("alice", ids[0])
    with pytest.raises(StageExpired):
        sessions.confirm("alice", ids[0], request([decision(1, "a", 12), decision(2, "b", 13)]))
    assert sessions.stage("alice", "fourth.txt", source())["status"] == "staged"


def test_metadata_only_cannot_be_mistaken_for_real_source_text(tmp_path):
    _, _, sessions = make_store(tmp_path)
    metadata = {"format": "rulate_account_metadata", "schema_version": 1,
                "books": [{"title": "Catalog only", "chapters": [{"number": 1}]}]}
    from chaptera_server.book_import import ImportFormatError
    with pytest.raises(ImportFormatError, match="Metadata-only"):
        sessions.stage("alice", "metadata.json", json.dumps(metadata).encode())


def test_http_api_auth_selection_and_restart(tmp_path):
    store = QueueStore(tmp_path / "server.sqlite3")
    settings = Settings(db_path=store.db_path, team_tokens={"alice": "alice-key", "bob": "bob-key"})
    app = create_app(settings, store)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            file = {"file": ("novel.txt", source("Редакторское предисловие.\n\n"), "text/plain")}
            assert (await client.post("/v1/imports/stages", files=file)).status_code == 401
            uploaded = await client.post("/v1/imports/stages", files=file, headers=HEADERS)
            assert uploaded.status_code == 201, uploaded.text
            stage_id = uploaded.json()["stage_id"]
            assert uploaded.json()["preview"]["chapter_count"] == 2
            assert (await client.get(f"/v1/imports/stages/{stage_id}", headers=BOB)).status_code == 404
            assert (await client.post(f"/v1/imports/stages/{stage_id}/confirm",
                                      json=request([decision(1, "a", 12), decision(2, "b", 13)],
                                                   prefix_policy="preserve"),
                                      headers=BOB)).status_code == 404
            bad = await client.post(f"/v1/imports/stages/{stage_id}/confirm", headers=HEADERS,
                                    json=request([decision(1, "a", 12)]))
            assert bad.status_code == 409
            choices = request([decision(1, "a", 12), decision(2, "b", 13)],
                              prefix_policy="preserve")
            saved = await client.post(f"/v1/imports/stages/{stage_id}/confirm",
                                      headers=HEADERS, json=choices)
            assert saved.status_code == 200, saved.text
            assert saved.json()["imported_count"] == 2
            assert (await client.get("/v1/books/demo/chapters/a", headers=BOB)).status_code == 200
            persisted = await client.get("/v1/imports/books/demo/unassigned", headers=BOB)
            assert "Редакторское предисловие" in persisted.text
            assert (await client.get("/v1/imports/books/demo/unassigned")).status_code == 401
            assert (await client.post(f"/v1/imports/stages/{stage_id}/confirm",
                                      headers=HEADERS, json=choices)).status_code == 200
            assert (await client.post("/v1/imports/stages", files={"file": ("metadata.json",
                 json.dumps({"format": "rulate_account_metadata", "schema_version": 1,
                             "books": []}).encode())}, headers=HEADERS)).status_code == 422

    asyncio.run(scenario())
    app2 = create_app(settings, QueueStore(store.db_path))
    assert app2.state.library.get_chapter("demo", "a")["source_revision"] == 1
    assert "Редакторское предисловие" in app2.state.import_sessions.prefixes("demo")[0]["source_html"]
