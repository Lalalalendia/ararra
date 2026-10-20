#!/usr/bin/env python3
"""RUST-02b owner CAS acceptance on the SAME disposable SQLite as Python.

Verify book schemas, audit events and authorization. No actual book contents,
sources, Gemini calls or production access.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

import httpx

from compare_rust_api import launch, ready
from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore
from chaptera_server.team_sessions import TeamSessions


async def exercise(app, settings: Settings, db_path: Path, endpoint: str) -> None:
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://python") as py,
        httpx.AsyncClient(base_url=endpoint, timeout=10) as rust,
    ):
        admin = {"X-API-Key": settings.team_tokens["chief"]}
        mod = {"X-API-Key": settings.team_tokens["mod"]}
        other = {"X-API-Key": settings.team_tokens["other"]}
        missing = await rust.get("/v1/books")
        assert missing.status_code == 401
        first_list = await py.get("/v1/books", headers=mod)
        rust_list = await rust.get("/v1/books", headers=mod)
        assert first_list.status_code == rust_list.status_code == 200
        assert first_list.json() == rust_list.json() == {"books": []}

        # Bidirectional Python<->Rust creation replay, no duplicate audit.
        record = {"book_id": "книга-rust", "title": "Русский текст"}
        rust_created = await rust.post("/v1/books", headers=mod, json=record)
        python_replayed = await py.post("/v1/books", headers=mod, json=record)
        assert rust_created.status_code == python_replayed.status_code == 201
        assert rust_created.json() == python_replayed.json()
        assert rust_created.json()["owner_member_id"] == "mod"
        record2 = {"book_id": "book-py", "title": "Python reference"}
        py_created = await py.post("/v1/books", headers=other, json=record2)
        rust_replayed = await rust.post("/v1/books", headers=other, json=record2)
        assert py_created.status_code == rust_replayed.status_code == 201
        assert py_created.json() == rust_replayed.json()
        conflicting = {"book_id": "book-py", "title": "Different"}
        for client in (py, rust):
            outcome = await client.post("/v1/books", headers=other, json=conflicting)
            assert outcome.status_code == 409
            assert outcome.json()["detail"] == "Book already exists with a different title or owner"

        # Shared books, but only a real moderator may be an owner.
        denied = await rust.post("/v1/books", headers=mod, json={
            "book_id": "wrong-owner", "title": "No", "owner_member_id": "other",
        })
        assert denied.status_code == 403
        assert denied.json()["detail"] == "Only administrator can assign another owner"
        denied_admin = await rust.post("/v1/books", headers=admin, json={
            "book_id": "admin-owned", "title": "No", "owner_member_id": "chief",
        })
        assert denied_admin.status_code == 422
        created_admin = await rust.post("/v1/books", headers=admin, json={
            "book_id": "admin-assigned", "title": "Team-owned", "owner_member_id": "other",
        })
        assert created_admin.status_code == 201

        # Book directory counts: current accepted text only.
        app.state.library.put_chapter(
            "book-py", "chapter-a", chapter_index=1,
            chapter_path="Chapter 1", source_html="<p>source</p>", expected_revision=0,
        )
        for path in ("/v1/books", "/v1/books/book-py", "/v1/books/книга-rust"):
            a = await py.get(path, headers=other)
            b = await rust.get(path, headers=other)
            assert a.status_code == b.status_code == 200
            assert a.json() == b.json(), path
        missing_book = await rust.get("/v1/books/not-exists", headers=other)
        assert missing_book.status_code == 404 and missing_book.json()["detail"] == "Book not found"

        # RUST-02c: chapter directory/detail must agree with Python and must
        # never leak full prose in the list response. Text stays in a read-only
        # disposable SQLite populated exclusively by the reference library.
        chapter_root = "/v1/books/book-py/chapters"
        chapter_detail = chapter_root + "/chapter-a"

        async def assert_chapter_parity(path):
            reference = await py.get(path, headers=other)
            candidate = await rust.get(path, headers=other)
            assert reference.status_code == candidate.status_code, path
            assert reference.json() == candidate.json(), path
            return candidate.json()

        current = await assert_chapter_parity(chapter_detail)
        assert current["source_revision"] == 1
        assert current["accepted_revision"] == 0
        initial_directory = await assert_chapter_parity(chapter_root)
        assert initial_directory["chapters"][0]["accepted_current"] is False
        assert "source_html" not in initial_directory["chapters"][0]
        assert "accepted_html" not in initial_directory["chapters"][0]

        app.state.library.accept_translation(
            "book-py", "chapter-a", "<p>Перевод первой главы.</p>",
            expected_revision=0, expected_source_revision=1, actor_id="other",
        )
        accepted_detail = await assert_chapter_parity(chapter_detail)
        accepted_directory = await assert_chapter_parity(chapter_root)
        assert accepted_detail["accepted_current"] is True
        assert accepted_detail["accepted_revision"] == 1
        assert accepted_directory["chapters"][0]["accepted_current"] is True
        accepted_books = await assert_chapter_parity("/v1/books")
        assert next(item for item in accepted_books["books"] if item["book_id"] == "book-py")[
            "accepted_current_count"
        ] == 1

        app.state.library.put_chapter(
            "book-py", "chapter-a", chapter_index=1,
            chapter_path="Chapter 1",
            source_html="<p>source changed, accepted translation is now stale</p>",
            expected_revision=1,
        )
        stale_detail = await assert_chapter_parity(chapter_detail)
        stale_directory = await assert_chapter_parity(chapter_root)
        assert stale_detail["source_revision"] == 2
        assert stale_detail["accepted_revision"] == 1
        assert stale_detail["accepted_current"] is False
        assert stale_directory["chapters"][0]["accepted_current"] is False
        assert "accepted_html" not in stale_directory["chapters"][0]
        stale_books = await assert_chapter_parity("/v1/books")
        assert next(item for item in stale_books["books"] if item["book_id"] == "book-py")[
            "accepted_current_count"
        ] == 0

        for path in (
            "/v1/books/book-py/chapters/not-present",
            "/v1/books/not-exists/chapters",
        ):
            missing_py = await py.get(path, headers=other)
            missing_rust = await rust.get(path, headers=other)
            assert missing_py.status_code == missing_rust.status_code == 404
            assert missing_py.json() == missing_rust.json(), path

        # Concurrent CAS: exactly one revision 0 -> 1, exactly one owner event.
        # Genuine two-writer contention: different new moderators, same CAS
        # generation. Exactly one update and one immutable audit event.
        results = await asyncio.gather(
            py.put("/v1/books/book-py/owner", headers=admin, json={
                "owner_member_id": "mod", "expected_owner_revision": 0,
            }),
            rust.put("/v1/books/book-py/owner", headers=admin, json={
                "owner_member_id": "third", "expected_owner_revision": 0,
            }),
        )
        assert sorted([x.status_code for x in results]) == [200, 409]
        changed = (await py.get("/v1/books/book-py", headers=other)).json()
        assert changed["owner_revision"] == 1
        assert changed["owner_member_id"] in {"mod", "third"}
        stale = await rust.put("/v1/books/book-py/owner", headers=admin, json={
            "owner_member_id": "other", "expected_owner_revision": 0,
        })
        assert stale.status_code == 409
        assert stale.json()["detail"] == "Book owner revision mismatch"
        no_op = await rust.put("/v1/books/book-py/owner", headers=admin, json={
            "owner_member_id": changed["owner_member_id"], "expected_owner_revision": 1,
        })
        assert no_op.status_code == 200 and no_op.json() == changed

        with sqlite3.connect(db_path) as con:
            events = con.execute(
                "SELECT revision,actor_id FROM book_owner_events WHERE book_id='book-py' "
                "ORDER BY revision"
            ).fetchall()
        assert events == [(0, "other"), (1, "chief")], events

        # Revoked moderator cannot be new owner. Admin-only endpoint fails for mod.
        forbidden = await rust.put("/v1/books/book-py/owner", headers=mod, json={
            "owner_member_id": "other", "expected_owner_revision": 1,
        })
        assert forbidden.status_code == 403
        missing_owner = await rust.put("/v1/books/book-py/owner", headers=admin, json={
            "owner_member_id": "unknown", "expected_owner_revision": 1,
        })
        assert missing_owner.status_code == 422

        # Browser cookie must send HMAC-based CSRF token.
        cookie = app.state.team_sessions.issue(
            member_id="other", team_key=settings.team_tokens["other"], ttl_seconds=3600
        )
        browser = {"Cookie": "readera_session=" + cookie}
        no_csrf = await rust.post("/v1/books", headers=browser,
                                  json={"book_id": "cookie-denied", "title": "No"})
        assert no_csrf.status_code == 403
        valid = {**browser, "X-ReadEra-CSRF": TeamSessions.csrf_token(cookie)}
        secured = await rust.post("/v1/books", headers=valid, json={
            "book_id": "cookie-created", "title": "Session",
        })
        assert secured.status_code == 201
        replay = await py.post("/v1/books", headers=valid, json={
            "book_id": "cookie-created", "title": "Session",
        })
        assert replay.json() == secured.json()
        poisoned = await rust.post("/v1/books", headers={
            **valid, "X-API-Key": "invalid",
        }, json={"book_id": "auth-denied", "title": "No"})
        assert poisoned.status_code == 401

        print("rust_book_cross_runtime_create=PASS")
        print("rust_book_directory_json_parity=PASS")
        print("rust_book_revision_cas_and_audit=PASS")
        print("rust_book_admin_owner_policy=PASS")
        print("rust_book_cookie_csrf=PASS")
        print("production_sqlite_access=no")
        print("gemini_calls=0")


def main() -> None:
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-rust-books-") as temporary:
        root=Path(temporary)
        path=root/"isolated.sqlite3"
        settings=Settings(
            db_path=path,
            team_tokens={
                "chief": "rust-books-chief-test",
                "mod": "rust-books-mod-test",
                "other": "rust-books-other-test",
                "third": "rust-books-third-test",
            },
            admin_member_id="chief",
            browser_session_secure=False,
        )
        app=create_app(settings, QueueStore(path))
        env=dict(os.environ)
        for secret in ("CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY",
                       "CHAPTERA_GEMINI_VAULT_KEY"):
            env.pop(secret,None)
        env.update({
            "CHAPTERA_DB_PATH":str(path),
            "CHAPTERA_TEAM_TOKENS_JSON":json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID":"chief",
            "ERIDA_RUST_ADDR":"127.0.0.1:18474",
            "ERIDA_RUST_TEST_WRITES":"1",
            "ERIDA_RUST_DISPOSABLE_ROOT":str(root),
        })
        runner=launch([binary],env)
        try:
            ready("http://127.0.0.1:18474",runner)
            asyncio.run(exercise(app,settings,path,"http://127.0.0.1:18474"))
            assert runner.poll() is None
        finally:
            if runner.poll() is None:
                runner.terminate()
                try:runner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    runner.kill()
                    runner.wait(timeout=5)


if __name__=="__main__":
    main()
