"""Durable RE-05 editorial state and revision/CAS safety gates."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.editorial import EditorialStore
from chaptera_server.library import BookLibrary, LibraryConflict, LibraryNotFound
from chaptera_server.storage import QueueStore


def workspace(tmp_path):
    db = tmp_path / "editorial.sqlite3"
    settings = Settings(db_path=db, team_tokens={
        "admin": "admin-key", "alice": "alice-key", "bob": "bob-key"
    }, admin_member_id="admin")
    app = create_app(settings, QueueStore(db))
    library = app.state.library
    library.create_book("book", "Test book", owner_member_id="alice", actor_id="admin")
    library.put_chapter("book", "c", chapter_index=1, chapter_path="chapters/1.xhtml",
                        source_html="<p>Original source</p>", expected_revision=0)
    return app, library, app.state.editorial


def test_drafts_durable_shared_and_optimistic_across_restart(tmp_path):
    app, library, store = workspace(tmp_path)
    assert store.get_draft("book", "c")["revision"] == 0
    original = library.get_chapter("book", "c")
    first = store.save_draft(
        "book", "c", body_text="Черновик один", expected_revision=0,
        expected_source_revision=1, expected_accepted_revision=0, actor_id="alice"
    )
    assert first["revision"] == 1 and first["actor_id"] == "alice"
    recovered = EditorialStore(QueueStore(app.state.store.db_path))
    assert recovered.get_draft("book", "c")["body_text"] == "Черновик один"
    with pytest.raises(LibraryConflict, match="Draft revision"):
        recovered.save_draft(
            "book", "c", body_text="Конкурентная перезапись", expected_revision=0,
            expected_source_revision=1, expected_accepted_revision=0, actor_id="bob"
        )
    second = recovered.save_draft(
        "book", "c", body_text="Отредактировал модератор Bob", expected_revision=1,
        expected_source_revision=1, expected_accepted_revision=0, actor_id="bob"
    )
    assert second["revision"] == 2 and second["actor_id"] == "bob"
    assert library.get_chapter("book", "c") == original
    with app.state.store._connect() as con:
        drafts = con.execute(
            "SELECT revision, actor_id FROM editor_draft_revisions "
            "WHERE book_id='book' AND chapter_id='c' ORDER BY revision"
        ).fetchall()
        assert [(r["revision"], r["actor_id"]) for r in drafts] == [(1, "alice"), (2, "bob")]


def test_draft_rebase_after_source_or_acceptance_change_must_be_explicit(tmp_path):
    _, library, store = workspace(tmp_path)
    store.save_draft("book", "c", body_text="Набросок", expected_revision=0,
                     expected_source_revision=1, expected_accepted_revision=0, actor_id="alice")
    library.accept_translation("book", "c", "<p>Принят перевод.</p>",
                               expected_revision=0, expected_source_revision=1, actor_id="alice")
    with pytest.raises(LibraryConflict, match="accepted translation changed"):
        store.save_draft("book", "c", body_text="Ещё правки", expected_revision=1,
                         expected_source_revision=1, expected_accepted_revision=0, actor_id="alice")
    with pytest.raises(LibraryConflict, match="explicit rebase"):
        store.save_draft("book", "c", body_text="Ещё правки", expected_revision=1,
                         expected_source_revision=1, expected_accepted_revision=1, actor_id="alice")
    rebound = store.save_draft(
        "book", "c", body_text="Ещё правки", expected_revision=1,
        expected_source_revision=1, expected_accepted_revision=1,
        actor_id="alice", rebase=True
    )
    assert rebound["revision"] == 2 and rebound["accepted_revision"] == 1
    library.put_chapter("book", "c", chapter_index=1, chapter_path="chapters/1.xhtml",
                        source_html="<p>Updated original</p>", expected_revision=1)
    with pytest.raises(LibraryConflict, match="Source"):
        store.save_draft("book", "c", body_text="Нет", expected_revision=2,
                         expected_source_revision=1, expected_accepted_revision=1, actor_id="bob")
    assert store.get_draft("book", "c")["body_text"] == "Ещё правки"


def test_notes_are_separate_audited_and_change_only_with_note_cas(tmp_path):
    app, library, store = workspace(tmp_path)
    draft = store.save_draft("book", "c", body_text="Черновик",
                             expected_revision=0, expected_source_revision=1,
                             expected_accepted_revision=0, actor_id="alice")
    content = "Редакторское замечание <script>alert('XSS')</script>"
    note = store.add_note(
        "book", "c", body_text=content, expected_source_revision=1,
        expected_draft_revision=draft["revision"], expected_accepted_revision=0,
        actor_id="bob"
    )
    assert note["body_text"] == content and note["author_id"] == "bob"
    assert note["status"] == "open"
    with pytest.raises(LibraryConflict, match="Chapter/draft changed"):
        store.add_note("book", "c", body_text="Устарело", expected_source_revision=1,
                       expected_draft_revision=0, expected_accepted_revision=0, actor_id="bob")
    resolved = store.resolve_note("book", "c", note["id"],
                                  status="resolved", expected_revision=1, actor_id="alice")
    assert resolved["status"] == "resolved"
    assert resolved["note_revision"] == 2 and resolved["resolver_id"] == "alice"
    with pytest.raises(LibraryConflict, match="Review note changed"):
        store.resolve_note("book", "c", note["id"], status="open",
                           expected_revision=1, actor_id="bob")
    assert store.list_notes("book", "c")[0]["status"] == "resolved"
    assert library.get_chapter("book", "c")["accepted_html"] is None
    assert "Редакторское замечание" not in library.get_chapter("book", "c")["source_html"]
    with pytest.raises(LibraryNotFound):
        store.list_notes("book", "does-not-exist")
    with pytest.raises(LibraryNotFound):
        store.resolve_note("book", "c", "unknown", status="resolved",
                           expected_revision=1, actor_id="bob")


def test_restore_accepted_creates_new_revision_and_never_restores_stale_source(tmp_path):
    app, library, store = workspace(tmp_path)
    original = library.get_chapter("book", "c")["source_html"]
    library.accept_translation("book", "c", "<p>Первая редакция.</p>",
                               expected_revision=0, expected_source_revision=1, actor_id="alice")
    library.accept_translation("book", "c", "<p>Вторая редакция.</p>",
                               expected_revision=1, expected_source_revision=1, actor_id="bob")
    history = store.accepted_history("book", "c")
    assert [(r["revision"], r["actor_id"]) for r in history] == [(2, "bob"), (1, "alice")]
    assert all("translated_html" not in r for r in history)
    assert "Первая редакция" in store.accepted_version("book", "c", 1)["translated_html"]
    with pytest.raises(LibraryConflict, match="Chapter revisions changed"):
        store.restore_accepted("book", "c", revision=1, expected_current_revision=1,
                               expected_source_revision=1, actor_id="alice")
    rolled = store.restore_accepted("book", "c", revision=1, expected_current_revision=2,
                                    expected_source_revision=1, actor_id="alice")
    assert rolled["accepted_revision"] == 3
    assert "Первая редакция" in rolled["accepted_html"]
    assert library.get_chapter("book", "c")["source_html"] == original
    assert len(store.accepted_history("book", "c")) == 3
    assert store.accepted_version("book", "c", 2)["translated_html"] == "<p>Вторая редакция.</p>"
    assert store.accepted_version("book", "c", 3)["actor_id"] == "alice"
    library.put_chapter("book", "c", chapter_index=1, chapter_path="chapters/1.xhtml",
                        source_html="<p>New source</p>", expected_revision=1)
    with pytest.raises(LibraryConflict, match="different source revision"):
        store.restore_accepted("book", "c", revision=1, expected_current_revision=3,
                               expected_source_revision=2, actor_id="bob")
    assert library.get_chapter("book", "c")["accepted_revision"] == 3


def test_editorial_http_member_auth_limits_and_conflicts(tmp_path):
    app, _, _ = workspace(tmp_path)
    base = "/v1/books/book/chapters/c"

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.get(base+"/draft")).status_code == 401
            assert (await client.get(base+"/notes")).status_code == 401
            assert (await client.get(base+"/accepted-revisions")).status_code == 401
            headers = {"X-API-Key": "alice-key"}
            missing = await client.get("/v1/books/book/chapters/missing/draft", headers=headers)
            assert missing.status_code == 404
            draft = await client.put(base+"/draft", headers=headers,
                                     json={"body_text": "Сохранённый черновик",
                                           "expected_revision": 0,
                                           "expected_source_revision": 1,
                                           "expected_accepted_revision": 0})
            assert draft.status_code == 200, draft.text
            assert draft.json()["revision"] == 1
            stale = await client.put(base+"/draft", headers={"X-API-Key": "bob-key"},
                                     json={"body_text": "Чужая версия",
                                           "expected_revision": 0,
                                           "expected_source_revision": 1,
                                           "expected_accepted_revision": 0})
            assert stale.status_code == 409
            assert (await client.get(base+"/draft", headers=headers)).json()["body_text"] == "Сохранённый черновик"
            note = await client.post(base+"/notes", headers={"X-API-Key": "bob-key"},
                                     json={"body_text": "Проверить формулировку",
                                           "expected_source_revision": 1,
                                           "expected_draft_revision": 1,
                                           "expected_accepted_revision": 0})
            assert note.status_code == 201, note.text
            assert note.json()["author_id"] == "bob"
            assert len((await client.get(base+"/notes", headers=headers)).json()["notes"]) == 1
            updated = await client.patch(base+"/notes/"+note.json()["id"], headers=headers,
                                         json={"status": "resolved", "expected_revision": 1})
            assert updated.status_code == 200
            assert updated.json()["resolver_id"] == "alice"
            invalid = await client.post(base+"/restore-accepted", headers=headers,
                                        json={"revision": 1, "expected_current_revision": 0,
                                              "expected_source_revision": 1})
            assert invalid.status_code == 404
            oversized = await client.post(base+"/notes", headers=headers,
                                          json={"body_text": "A"*4001,
                                                "expected_source_revision": 1,
                                                "expected_draft_revision": 1,
                                                "expected_accepted_revision": 0})
            assert oversized.status_code == 422
    asyncio.run(scenario())
