#!/usr/bin/env python3
"""M1d: Python ASGI/Rust loopback editorial note parity on one disposable SQLite.

No production database, live source collection, Gemini call or worker switch.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

import httpx

from compare_rust_api import launch, ready
from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.editorial import EditorialStore
from chaptera_server.storage import QueueStore
from chaptera_server.team_sessions import TeamSessions


async def exercise(app, db_path: Path, base_url: str, settings: Settings) -> None:
    chapter = "/v1/books/m1d/chapters/one"
    route = chapter + "/notes"
    draft_route = chapter + "/draft"
    member = {"X-API-Key": settings.team_tokens["mod"]}
    admin = {"X-API-Key": settings.team_tokens["chief"]}

    def payload(text: str, source: int = 1, draft: int = 0, accepted: int = 0) -> dict:
        return {
            "body_text": text,
            "expected_source_revision": source,
            "expected_draft_revision": draft,
            "expected_accepted_revision": accepted,
        }

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://python",
        ) as python,
        httpx.AsyncClient(base_url=base_url, timeout=15.0) as rust,
    ):
        book = await python.post(
            "/v1/books", headers=member,
            json={"book_id": "m1d", "title": "M1d editor review fixture"},
        )
        assert book.status_code == 201, book.text
        source = await python.put(
            chapter, headers=member,
            json={"chapter_index": 1, "chapter_path": "1.xhtml",
                  "source_html": "<p>Original source text.</p>", "expected_revision": 0},
        )
        assert source.status_code == 200, source.text
        assert (await python.get(route, headers=member)).json() == {
            "notes": []
        }
        assert (await rust.get(route, headers=member)).json() == {"notes": []}
        assert (await rust.get(route)).status_code == 401

        # Rust INSERT -> Python SELECT, exact schema, text normalization and actor.
        first = await rust.post(route, headers=member, json=payload("  Review this sentence  "))
        assert first.status_code == 201, (first.status_code, first.text)
        first_note = first.json()
        note_id = first_note["id"]
        assert re.fullmatch(r"[0-9a-f]{32}", note_id)
        assert first_note["body_text"] == "Review this sentence"
        assert first_note["note_revision"] == 1
        assert first_note["status"] == "open"
        assert first_note["author_id"] == "mod"
        assert first_note["resolver_id"] is None
        assert first_note["source_revision"] == 1
        assert first_note["draft_revision"] == first_note["accepted_revision"] == 0
        assert (await rust.get(route, headers=admin)).json() == (
            await python.get(route, headers=member)).json()

        # Python INSERT -> Rust SELECT: do not execute or normalize note markup.
        markup = "Review <script>alert('XSS')</script> safely"
        second = await python.post(route, headers=admin, json=payload(markup))
        assert second.status_code == 201, second.text
        second_id = second.json()["id"]
        assert second.json()["body_text"] == markup
        assert (await rust.get(route, headers=member)).json() == (
            await python.get(route, headers=member)).json()

        # Python and Rust both refuse stale note creation if draft changes.
        saved_draft = await python.put(
            draft_route, headers=member,
            json={"body_text": "New editorial draft",
                  "expected_revision": 0, "expected_source_revision": 1,
                  "expected_accepted_revision": 0},
        )
        assert saved_draft.status_code == 200
        stale = await rust.post(route, headers=admin, json=payload("Stale draft", draft=0))
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"] == "Chapter/draft changed; reload before recording review"
        third = await rust.post(
            route, headers=admin, json=payload("Draft review", draft=1))
        assert third.status_code == 201
        assert third.json()["draft_revision"] == 1

        # Invalid text and mismatched source/accepted versions are fail-closed.
        for client in (python, rust):
            empty = await client.post(route, headers=member, json=payload(""))
            assert empty.status_code == 422, (empty.status_code, empty.text)
            blank = await client.post(route, headers=member, json=payload("   ", draft=1))
            assert blank.status_code == 400, (blank.status_code, blank.text)
            huge = await client.post(route, headers=member, json=payload("x"*4001, draft=1))
            assert huge.status_code == 422, huge.text
            absent = await client.get(
                "/v1/books/m1d/chapters/missing/notes", headers=member)
            assert absent.status_code == 404 and absent.json()["detail"] == "Chapter not found"
            missing_create = await client.post(
                "/v1/books/m1d/chapters/missing/notes",
                headers=member, json=payload("No chapter"))
            assert missing_create.status_code == 404
            not_found = await client.patch(
                route + "/unknown", headers=member,
                json={"status": "resolved", "expected_revision": 1})
            assert not_found.status_code == 404
            assert not_found.json()["detail"] == "Review note not found"

        accepted = await python.put(
            chapter + "/accepted-translation", headers=member,
            json={"translated_html": "<p>Accepted text.</p>",
                  "expected_revision": 0, "expected_source_revision": 1},
        )
        assert accepted.status_code == 200
        stale_accepted = await rust.post(
            route, headers=member, json=payload("Stale accepted", draft=1, accepted=0))
        assert stale_accepted.status_code == 409
        accepted_note = await python.post(
            route, headers=admin, json=payload("Accepted review", draft=1, accepted=1))
        assert accepted_note.status_code == 201
        assert (await rust.get(route, headers=member)).json() == (
            await python.get(route, headers=member)).json()

        # Rust PATCH -> Python read; unchanged status is a true no-op,
        # including original actor, resolver, timestamps and note revision.
        first_path = route + "/" + note_id
        noop = await rust.patch(first_path, headers=admin,
                                json={"status": "open", "expected_revision": 1})
        assert noop.status_code == 200 and noop.json() == first_note
        resolved = await rust.patch(
            first_path, headers=admin, json={"status": "resolved", "expected_revision": 1})
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["note_revision"] == 2
        assert resolved.json()["resolver_id"] == "chief"
        assert resolved.json()["author_id"] == "mod"
        assert (await rust.patch(
            first_path, headers=member,
            json={"status": "resolved", "expected_revision": 2})).json() == resolved.json()
        stale_patch = await python.patch(
            first_path, headers=member,
            json={"status": "open", "expected_revision": 1})
        assert stale_patch.status_code == 409

        # Python PATCH -> Rust read; real two-process CAS on one note revision.
        second_path = route + "/" + second_id
        py_patch = await python.patch(second_path, headers=member,
                                      json={"status": "resolved", "expected_revision": 1})
        assert py_patch.status_code == 200 and py_patch.json()["note_revision"] == 2
        assert (await rust.get(route, headers=member)).json() == (
            await python.get(route, headers=member)).json()

        third_path = route + "/" + third.json()["id"]
        concurrent = await asyncio.gather(
            rust.patch(third_path, headers=member,
                       json={"status": "resolved", "expected_revision": 1}),
            python.patch(third_path, headers=admin,
                         json={"status": "resolved", "expected_revision": 1}),
        )
        assert sorted(result.status_code for result in concurrent) == [200, 409], [
            (result.status_code, result.text) for result in concurrent
        ]
        latest = (await rust.get(route, headers=member)).json()
        assert latest == (await python.get(route, headers=member)).json()
        third_record = next(n for n in latest["notes"] if n["id"] == third.json()["id"])
        assert third_record["note_revision"] == 2

        # Browser GET is session-authenticated, while POST/PATCH require CSRF.
        cookie = app.state.team_sessions.issue(
            member_id="mod", team_key=settings.team_tokens["mod"], ttl_seconds=1800)
        browser = {"Cookie": "readera_session=" + cookie}
        assert (await rust.get(route, headers=browser)).status_code == 200
        forbidden = await rust.post(
            route, headers=browser, json=payload("Missing CSRF", draft=1, accepted=1))
        assert forbidden.status_code == 403
        forbidden_patch = await rust.patch(
            first_path, headers=browser,
            json={"status": "open", "expected_revision": 2})
        assert forbidden_patch.status_code == 403
        proof = {**browser, "X-ReadEra-CSRF": TeamSessions.csrf_token(cookie)}
        browser_created = await rust.post(
            route, headers=proof, json=payload("Browser proof", draft=1, accepted=1))
        assert browser_created.status_code == 201, browser_created.text
        assert browser_created.json()["author_id"] == "mod"
        assert (await rust.get(route, headers={**proof, "X-API-Key": "invalid"})).status_code == 401
        assert (await rust.post(
            route, headers={**proof, "X-API-Key": "invalid"},
            json=payload("No header fallback", draft=1, accepted=1))).status_code == 401

        # A changed source invalidates old expected versions for new reviews;
        # note status operations can still safely close historical notes.
        changed = await python.put(
            chapter, headers=member,
            json={"chapter_index": 1, "chapter_path": "1.xhtml",
                  "source_html": "<p>Source updated.</p>", "expected_revision": 1},
        )
        assert changed.status_code == 200 and changed.json()["source_revision"] == 2
        stale_source = await rust.post(
            route, headers=member, json=payload("Old source", source=1, draft=1, accepted=1))
        assert stale_source.status_code == 409
        closed = await rust.patch(
            route + "/" + browser_created.json()["id"], headers=admin,
            json={"status": "resolved", "expected_revision": 1})
        assert closed.status_code == 200

        current_chapter = (await python.get(chapter, headers=member)).json()
        assert current_chapter == (await rust.get(chapter, headers=member)).json()
        assert current_chapter["source_html"] == "<p>Source updated.</p>"
        assert current_chapter["accepted_html"] == "<p>Accepted text.</p>"
        assert all(note["body_text"] not in current_chapter["source_html"]
                   for note in latest["notes"])
        assert all(note["body_text"] not in current_chapter["accepted_html"]
                   for note in latest["notes"])

        # Durable rows and no accidental mutation of the accepted journal or draft.
        reopened = EditorialStore(QueueStore(db_path))
        assert reopened.list_notes("m1d", "one") == (
            await rust.get(route, headers=member)).json()["notes"]
        with sqlite3.connect(db_path) as connection:
            notes = connection.execute(
                "SELECT id,author_id,note_revision FROM editor_review_notes "
                "WHERE book_id='m1d' AND chapter_id='one'").fetchall()
            assert len(notes) == 5
            assert len({record[0] for record in notes}) == len(notes)
            accepted_history = connection.execute(
                "SELECT revision,translated_html FROM book_accepted_revisions "
                "WHERE book_id='m1d' AND chapter_id='one' ORDER BY revision").fetchall()
            assert accepted_history == [(1, "<p>Accepted text.</p>")]
            draft_state = connection.execute(
                "SELECT revision,body_text FROM editor_drafts "
                "WHERE book_id='m1d' AND chapter_id='one'").fetchone()
            assert draft_state == (1, "New editorial draft")

        print("rust_notes_python_rust_read_create_parity=PASS")
        print("rust_notes_revision_cas_and_concurrent_status=PASS")
        print("rust_notes_stale_source_draft_accepted_fencing=PASS")
        print("rust_notes_cookie_csrf_actor_isolation=PASS")
        print("rust_notes_sqlite_restart_and_content_isolation=PASS")
        print("production_sqlite_access=no")
        print("gemini_calls=0")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_review_notes.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-rust-m1d-notes-") as directory:
        root = Path(directory)
        db_path = root / "disposable.sqlite3"
        settings = Settings(
            db_path=db_path,
            team_tokens={"chief": "m1d-chief", "mod": "m1d-mod"},
            admin_member_id="chief",
            browser_session_secure=False,
        )
        app = create_app(settings, QueueStore(db_path))
        env = dict(os.environ)
        for key in ("CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY", "CHAPTERA_GEMINI_VAULT_KEY"):
            env.pop(key, None)
        env.update({
            "CHAPTERA_DB_PATH": str(db_path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "ERIDA_RUST_ADDR": "127.0.0.1:18485",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
        })
        runner = launch([binary], env)
        try:
            ready("http://127.0.0.1:18485", runner)
            asyncio.run(exercise(app, db_path, "http://127.0.0.1:18485", settings))
            assert runner.poll() is None
        finally:
            if runner.poll() is None:
                runner.terminate()
                try:
                    runner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    runner.kill()
                    runner.wait(timeout=5)


if __name__ == "__main__":
    main()
