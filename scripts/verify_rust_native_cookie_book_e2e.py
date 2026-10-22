#!/usr/bin/env python3
"""M1i: one complete cookie-authenticated book HTTP path handled by Rust alone.

Python is used ONLY to initialize an empty disposable SQLite using QueueStore.
No Python ASGI application or HTTP endpoint is launched in this test.
This is an API-cookie journey, NOT a real Chromium UI or paid translation worker.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

import httpx

from compare_rust_api import launch, ready
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore
from chaptera_server.library import BookLibrary
from chaptera_server.editorial import EditorialStore
from chaptera_server.team_credentials import TeamCredentials
from chaptera_server.team_sessions import TeamSessions
from chaptera_server.team_login_throttle import BrowserLoginThrottle


def ok(response: httpx.Response, expected: int, phase: str) -> None:
    # Do not print responses: credential actions return one-time secret tokens.
    assert response.status_code == expected, (
        f"{phase}: expected HTTP {expected}, got {response.status_code}"
    )


def source_paragraph(text: str) -> str:
    return "<p>" + text * 12 + "</p>"


async def scenario(url: str, db_path: Path, settings: Settings) -> None:
    root = "/v1/books/m1i-native-book"
    first = root + "/chapters/first"
    second = root + "/chapters/second"
    session = "/v1/session"
    members = "/v1/admin/members"

    a_source = source_paragraph(
        "Перед путником открывается широкая равнина, вокруг шумит ночной ветер. "
    )
    b_source = source_paragraph(
        "В городе продолжается обычная жизнь, жители встречаются на площади. "
    )
    b_changed = source_paragraph(
        "В новом путешествии герои находят безопасную дорогу через долину. "
    )
    a_accepted = "<p>Первый опубликованный вариант перевода.</p>"
    b_accepted = "<p>Второй готовый перевод.</p>"
    private_text = "НЕ ПУБЛИКОВАТЬ — внутренний редакторский черновик."
    private_note = "Редакторское замечание: уточнить формулировку."

    async with (
        httpx.AsyncClient(base_url=url, timeout=15) as anonymous,
        httpx.AsyncClient(base_url=url, timeout=15) as administrator,
        httpx.AsyncClient(base_url=url, timeout=15) as editor,
        httpx.AsyncClient(base_url=url, timeout=15) as renewed_editor,
    ):
        ok(await anonymous.get(root), 401, "unauthenticated book")
        ok(await anonymous.get(session), 401, "unauthenticated session")

        admin_login = await administrator.post(
            session, json={"api_key": settings.team_tokens["chief"]}
        )
        ok(admin_login, 201, "Rust admin login")
        assert admin_login.json()["role"] == "admin"
        assert admin_login.json()["member_id"] == "chief"
        administrator.headers["X-ReadEra-CSRF"] = admin_login.json()["csrf_token"]
        admin_cookie = administrator.cookies.get("readera_session")
        assert admin_cookie and admin_cookie.startswith("rs_")
        ok(await administrator.get(session), 200, "Rust admin cookie restore")

        ok(await administrator.post(
            members, headers={"X-ReadEra-CSRF": ""},
            json={"member_id": "scribe"},
        ), 403, "admin mutation without CSRF")
        issued = await administrator.post(
            members, json={"member_id": "scribe"},
        )
        ok(issued, 201, "Rust issues moderator key")
        key = issued.json()["access_token"]
        assert key.startswith("re_") and len(key) >= 45
        assert issued.json()["member_id"] == "scribe"
        assert "no-store" in issued.headers["cache-control"]
        assert key not in (await administrator.get(members)).text

        editor_login = await editor.post(session, json={"api_key": key})
        ok(editor_login, 201, "Rust editor login using Rust-issued token")
        assert editor_login.json()["member_id"] == "scribe"
        assert editor_login.json()["role"] == "moderator"
        editor_cookie = editor.cookies.get("readera_session")
        assert editor_cookie and editor_cookie != admin_cookie
        assert "httponly" in editor_login.headers["set-cookie"].lower()
        assert "samesite=strict" in editor_login.headers["set-cookie"].lower()
        editor.headers["X-ReadEra-CSRF"] = editor_login.json()["csrf_token"]
        ok(await editor.get(session), 200, "Rust editor cookie restore")
        ok(await editor.post(
            members, json={"member_id": "not-allowed"},
        ), 403, "moderator cannot issue members")
        ok(await editor.post(
            "/v1/books", headers={"X-ReadEra-CSRF": ""},
            json={"book_id": "forbidden", "title": "CSRF"},
        ), 403, "editor book creation requires CSRF")

        created = await editor.post(
            "/v1/books", json={"book_id": "m1i-native-book", "title": "Книга Rust M1i"},
        )
        ok(created, 201, "Rust book create by cookie")
        assert created.json()["owner_member_id"] == "scribe"
        assert created.json()["owner_revision"] == 0

        glossary = await editor.put(
            root + "/glossary",
            json={"glossary": {"герой": "герой"},
                  "glossary_versions": {},
                  "glossary_authority": "READY",
                  "expected_revision": 0},
        )
        ok(glossary, 200, "Rust glossary write")
        assert glossary.json()["glossary_revision"] == 1
        ok(await editor.put(
            root + "/glossary",
            json={"glossary": {"герой": "злодей"},
                  "glossary_versions": {}, "glossary_authority": "READY",
                  "expected_revision": 0},
        ), 409, "Rust glossary stale CAS")

        for index, route, source in (
            (1, first, a_source),
            (2, second, b_source),
        ):
            write = await editor.put(
                route, json={"chapter_index": index,
                             "chapter_path": f"chapter{index}.xhtml",
                             "source_html": source,
                             "expected_revision": 0},
            )
            ok(write, 200, f"Rust chapter {index} source create")
            assert write.json()["source_revision"] == 1
            assert write.json()["source_html"] == source
            view = await editor.get(route)
            ok(view, 200, "Rust chapter readback")
            assert view.json() == write.json()
        book_view = await editor.get(root)
        ok(book_view, 200, "Rust book read")
        assert book_view.json()["glossary_revision"] == 1
        listing = await editor.get(root + "/chapters")
        ok(listing, 200, "Rust chapter directory")
        assert len(listing.json()["chapters"]) == 2

        draft = await editor.put(first + "/draft", json={
            "body_text": private_text, "expected_revision": 0,
            "expected_source_revision": 1, "expected_accepted_revision": 0,
        })
        ok(draft, 200, "Rust private editor draft")
        assert draft.json()["revision"] == 1
        assert (await editor.get(first + "/draft")).json()["body_text"] == private_text

        note = await editor.post(first + "/notes", json={
            "body_text": private_note, "expected_source_revision": 1,
            "expected_draft_revision": 1, "expected_accepted_revision": 0,
        })
        ok(note, 201, "Rust editor note create")
        note_id = note.json()["id"]
        assert note.json()["author_id"] == "scribe" and note.json()["status"] == "open"
        resolved = await editor.patch(
            first + "/notes/" + note_id,
            json={"status": "resolved", "expected_revision": 1},
        )
        ok(resolved, 200, "Rust note resolve")
        assert resolved.json()["note_revision"] == 2
        assert resolved.json()["status"] == "resolved"
        assert resolved.json()["resolver_id"] == "scribe"
        ok(await editor.patch(
            first + "/notes/" + note_id,
            json={"status": "open", "expected_revision": 1},
        ), 409, "Rust note stale CAS")

        for route, accepted in ((first, a_accepted), (second, b_accepted)):
            result = await editor.put(route + "/accepted-translation", json={
                "translated_html": accepted, "expected_revision": 0,
                "expected_source_revision": 1,
            })
            ok(result, 200, "Rust accepts translation")
            assert result.json()["accepted_revision"] == 1
            assert result.json()["accepted_current"] is True
        stale_translation = await editor.put(
            first + "/accepted-translation",
            json={"translated_html": "<p>Недействительная правка.</p>",
                  "expected_revision": 0, "expected_source_revision": 1},
        )
        ok(stale_translation, 409, "Rust accepted stale CAS")

        revised = await editor.put(
            first + "/accepted-translation",
            json={"translated_html": "<p>Исправленный текст первой главы.</p>",
                  "expected_revision": 1, "expected_source_revision": 1},
        )
        ok(revised, 200, "Rust accepted revision two")
        assert revised.json()["accepted_revision"] == 2

        restored = await editor.post(first + "/restore-accepted", json={
            "revision": 1, "expected_current_revision": 2,
            "expected_source_revision": 1,
        })
        ok(restored, 200, "Rust restore accepted revision one")
        assert restored.json()["accepted_revision"] == 3
        assert restored.json()["accepted_html"] == a_accepted
        history = await editor.get(first + "/accepted-revisions")
        ok(history, 200, "Rust accepted history")
        assert [r["revision"] for r in history.json()["revisions"]] == [3, 2, 1]
        assert (await editor.get(first + "/accepted-revisions/1")).json()[
            "translated_html"
        ] == a_accepted

        def preview_request():
            return {"expected_last_chapter": 2}

        ready_report = await editor.get(root + "/export/preview", params=preview_request())
        ok(ready_report, 200, "Rust native export preview")
        report = ready_report.json()
        assert report["ready"] is True
        assert report["issue_count"] == 0 and report["ready_chapter_count"] == 2
        manifest = report["manifest_sha256"]
        assert len(manifest) == 64 and all(ch in "0123456789abcdef" for ch in manifest)

        async def download(receipt: str, fmt: str):
            return await editor.get(root + "/export", params={
                "expected_last_chapter": 2,
                "expected_manifest_sha256": receipt,
                "format": fmt,
            })

        for fmt in ("txt", "html"):
            file = await download(manifest, fmt)
            ok(file, 200, "Rust native " + fmt + " export")
            assert file.headers["x-readera-manifest-sha256"] == manifest
            assert hashlib.sha256(file.content).hexdigest() == file.headers[
                "x-readera-content-sha256"
            ]
            assert file.headers["etag"] == f'"{file.headers["x-readera-content-sha256"]}"'
            assert "no-store" in file.headers["cache-control"]
            assert "nosniff" == file.headers["x-content-type-options"]
            assert file.headers["content-disposition"].endswith("." + fmt + '"')
            assert private_text not in file.text and private_note not in file.text
            assert "Перед путником" not in file.text and "В городе продолжается" not in file.text
            assert "Первый опубликованный вариант" in file.text
            assert "Второй готовый перевод" in file.text
            if fmt == "txt":
                assert "Глава 1" in file.text and "Глава 2" in file.text
            else:
                assert "<h2>Глава 1</h2>" in file.text
                assert "<h2>Глава 2</h2>" in file.text
        ok(await download("0" * 64, "txt"), 409, "Rust export wrong manifest")
        incomplete = await editor.get(
            root + "/export/preview", params={"expected_last_chapter": 3},
        )
        ok(incomplete, 200, "Rust export missing chapter preview")
        assert incomplete.json()["ready"] is False
        assert {"code": "missing_source", "chapter_index": 3} in incomplete.json()["issues"]

        changed = await editor.put(second, json={
            "chapter_index": 2, "chapter_path": "chapter2.xhtml",
            "source_html": b_changed, "expected_revision": 1,
        })
        ok(changed, 200, "Rust chapter source update")
        assert changed.json()["accepted_current"] is False
        stale_preview = await editor.get(root + "/export/preview", params=preview_request())
        ok(stale_preview, 200, "Rust export refuses stale accepted chapter")
        assert stale_preview.json()["ready"] is False
        assert {"code": "stale_accepted", "chapter_index": 2} in stale_preview.json()["issues"]
        stale_download = await download(manifest, "txt")
        ok(stale_download, 409, "Rust export refuses stale source")
        assert "content-disposition" not in stale_download.headers

        second_updated = await editor.put(second + "/accepted-translation", json={
            "translated_html": "<p>Новый перевод второй главы.</p>",
            "expected_revision": 1, "expected_source_revision": 2,
        })
        ok(second_updated, 200, "Rust reaccepts after source update")
        assert second_updated.json()["accepted_revision"] == 2
        restored_ready = await editor.get(
            root + "/export/preview", params=preview_request(),
        )
        ok(restored_ready, 200, "Rust export preview after reaccept")
        assert restored_ready.json()["ready"] is True
        latest_manifest = restored_ready.json()["manifest_sha256"]
        assert latest_manifest != manifest
        ok(await download(manifest, "txt"), 409, "Rust rejects former receipt")
        ok(await download(latest_manifest, "txt"), 200, "Rust new receipt downloads")

        # Rotation invalidates the OLD Rust-created browser session.
        rotated = await administrator.post(members + "/scribe/rotate")
        ok(rotated, 200, "Rust admin rotates issued moderator")
        new_key = rotated.json()["access_token"]
        assert new_key != key
        ok(await editor.get(session), 401, "old editor cookie invalid after rotate")
        ok(await editor.get(root), 401, "old editor cannot read book")

        renewed = await renewed_editor.post(session, json={"api_key": new_key})
        ok(renewed, 201, "new Rust editor login after rotation")
        assert renewed.json()["member_id"] == "scribe"
        renewed_editor.headers["X-ReadEra-CSRF"] = renewed.json()["csrf_token"]
        ok(await renewed_editor.get(root), 200, "renewed editor book read")

        denied_disable = await administrator.post(members + "/scribe/disable")
        ok(denied_disable, 409, "Rust refuses to orphan owned book")
        assert denied_disable.json()["detail"] == (
            "Reassign owned books before disabling this moderator"
        )
        owner_changed = await administrator.put(root + "/owner", json={
            "owner_member_id": "mod", "expected_owner_revision": 0,
        })
        ok(owner_changed, 200, "Rust CAS transfers book owner")
        assert owner_changed.json()["owner_member_id"] == "mod"
        assert owner_changed.json()["owner_revision"] == 1
        disabled = await administrator.post(members + "/scribe/disable")
        ok(disabled, 200, "Rust disables moderator after owner transfer")
        assert disabled.json()["status"] == "disabled"
        ok(await renewed_editor.get(session), 401, "disabled editor cookie revoked")
        ok(await renewed_editor.get(root), 401, "disabled editor cannot read")
        ok(await administrator.get(root), 200, "admin retains book access")

        logout = await administrator.post(session + "/logout")
        ok(logout, 204, "Rust admin logout")
        ok(await administrator.get(session), 401, "logged-out admin session invalid")

    # Verify SQLite durability and audit WITHOUT using Python application API.
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT owner_member_id,owner_revision FROM books "
            "WHERE id='m1i-native-book'"
        ).fetchone() == ("mod", 1)
        assert db.execute(
            "SELECT chapter_id,accepted_revision FROM book_chapters "
            "WHERE book_id='m1i-native-book' ORDER BY chapter_index"
        ).fetchall() == [("first", 3), ("second", 2)]
        assert db.execute(
            "SELECT COUNT(*) FROM book_accepted_revisions "
            "WHERE book_id='m1i-native-book'"
        ).fetchone()[0] == 5
        assert db.execute(
            "SELECT status,note_revision,author_id,resolver_id "
            "FROM editor_review_notes WHERE book_id='m1i-native-book'"
        ).fetchone() == ("resolved", 2, "scribe", "scribe")
        assert db.execute(
            "SELECT body_text FROM editor_drafts "
            "WHERE book_id='m1i-native-book'"
        ).fetchone()[0] == private_text
        assert db.execute(
            "SELECT active,token_sha256 FROM team_access_credentials "
            "WHERE member_id='scribe'"
        ).fetchone() == (0, None)
        events = db.execute(
            "SELECT action FROM team_access_events WHERE member_id='scribe' "
            "ORDER BY id"
        ).fetchall()
        assert events == [("issue",), ("rotate",), ("disable",)]
        assert db.execute(
            "SELECT COUNT(*) FROM book_glossary_revisions "
            "WHERE book_id='m1i-native-book'"
        ).fetchone()[0] == 1
    # Do not persist the one-time generated browser/credential tokens.
    for file in (db_path, db_path.with_name(db_path.name + "-wal")):
        if file.exists():
            contents = file.read_bytes()
            assert key.encode() not in contents
            assert new_key.encode() not in contents
            assert editor_cookie.encode() not in contents
            assert admin_cookie.encode() not in contents

    print("rust_only_m1i_cookie_login_and_admin_issue=PASS")
    print("rust_only_m1i_book_glossary_chapter_draft_note=PASS")
    print("rust_only_m1i_accept_restore_and_export_bytes=PASS")
    print("rust_only_m1i_stale_source_and_manifest_fail_closed=PASS")
    print("rust_only_m1i_rotation_disable_owner_handoff_logout=PASS")
    print("rust_only_m1i_sqlite_audit_and_no_raw_keys=PASS")
    print("python_http_api_used=NO")
    print("chromium_ui_test=NOT_COVERED")
    print("production_sqlite_access=NO")
    print("gemini_calls=0")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_rust_native_cookie_book_e2e.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-rust-m1i-cookie-") as directory:
        root = Path(directory)
        db_path = root / "disposable.sqlite3"
        settings = Settings(
            db_path=db_path,
            team_tokens={"chief": "m1i-admin-secret", "mod": "m1i-mod-secret"},
            admin_member_id="chief",
            browser_session_secure=False,
        )
        # Schema bootstrap only. These constructors apply the documented
        # Python SQLite migrations; NO Python ASGI app or HTTP handler is run.
        queue = QueueStore(db_path)
        BookLibrary(queue)
        EditorialStore(queue)
        TeamCredentials(queue)
        TeamSessions(queue)
        BrowserLoginThrottle(queue)
        with sqlite3.connect(db_path) as db:
            for table in ("books", "book_chapters", "book_accepted_revisions",
                          "editor_drafts", "editor_review_notes",
                          "team_access_credentials", "team_browser_sessions",
                          "browser_login_rate", "book_glossary_revisions"):
                assert db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone(), f"SQLite bootstrap did not create {table}"
        env = dict(os.environ)
        for key in (
            "CREDENTIALS_DIRECTORY",
            "CHAPTERA_GEMINI_API_KEY",
            "CHAPTERA_GEMINI_VAULT_KEY",
        ):
            env.pop(key, None)
        env.update({
            "CHAPTERA_DB_PATH": str(db_path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "CHAPTERA_BROWSER_SESSION_SECURE": "0",
            "ERIDA_RUST_ADDR": "127.0.0.1:18490",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
        })
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18490", process)
            asyncio.run(scenario("http://127.0.0.1:18490", db_path, settings))
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
