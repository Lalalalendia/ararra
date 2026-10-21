#!/usr/bin/env python3
"""RUST-M1e vertical book scenario with a clearly identified Python export boundary.

Runs real Python ASGI and Rust loopback on the SAME temporary SQLite.
This is a token-authenticated user path, not Rust browser-login or export parity.
No live Gemini, remote sources, production DB or VPS writes.
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
from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore
from chaptera_server.team_sessions import TeamSessions


async def scenario(app, settings: Settings, db_path: Path, rust_url: str) -> None:
    book_id = "m1e-full-book"
    root = f"/v1/books/{book_id}"
    first = root + "/chapters/alpha"
    second = root + "/chapters/beta"
    export_preview = root + "/export/preview?expected_last_chapter=2"
    mod = {"X-API-Key": settings.team_tokens["mod"]}
    chief = {"X-API-Key": settings.team_tokens["chief"]}

    # Long, plain paragraphs exercise the independently proved Rust source
    # normalization grammar; short source paragraphs may be rejected fail-closed.
    first_source = "<p>" + (
        "Перед путником открывается широкая равнина, вокруг шумит ночной ветер. " * 12
    ) + "</p>"
    second_source = "<p>" + (
        "В городе продолжается обычная жизнь, жители встречаются на площади. " * 12
    ) + "</p>"
    updated_source = "<p>" + (
        "В новом путешествии герои находят безопасную дорогу через долину. " * 12
    ) + "</p>"

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://python",
        ) as py,
        httpx.AsyncClient(base_url=rust_url, timeout=15.0) as rs,
    ):
        # The same real team-credential gates protect the two APIs.
        assert (await rs.get(root)).status_code == 401
        assert (await rs.get("/v1/me", headers=mod)).json()["member_id"] == "mod"

        # A real Rust write is visible through Python with the same book JSON.
        created = await rs.post(
            "/v1/books", headers=mod,
            json={"book_id": book_id, "title": "Синтетическая книга M1e"},
        )
        assert created.status_code == 201, created.text
        assert (await py.get(root, headers=mod)).json() == (await rs.get(root, headers=mod)).json()

        # Change glossary, preserve exact shared SQLite revisions.
        glossary = await rs.put(
            root + "/glossary", headers=mod,
            json={
                "glossary": {"герой": "герой"}, "glossary_versions": {},
                "glossary_authority": "READY", "expected_revision": 0,
            },
        )
        assert glossary.status_code == 200, glossary.text
        assert glossary.json()["glossary_revision"] == 1
        assert glossary.json() == (await py.get(root, headers=mod)).json()

        source_payloads = (
            {
                "chapter_index": 1, "chapter_path": "alpha.xhtml",
                "source_html": first_source,
                "expected_revision": 0,
            },
            {
                "chapter_index": 2, "chapter_path": "beta.xhtml",
                "source_html": second_source,
                "expected_revision": 0,
            },
        )
        # Rust creates the first chapter, Python creates the second.
        rust_source = await rs.put(first, headers=mod, json=source_payloads[0])
        python_source = await py.put(second, headers=chief, json=source_payloads[1])
        assert rust_source.status_code == python_source.status_code == 200, (
            rust_source.text, python_source.text,
        )
        assert rust_source.json()["source_revision"] == python_source.json()["source_revision"] == 1
        for route in (first, second, root + "/chapters", root):
            p = await py.get(route, headers=mod)
            r = await rs.get(route, headers=mod)
            assert p.status_code == r.status_code == 200
            assert p.json() == r.json(), route

        # The shared editorial draft is written by Rust, but cannot be
        # mistaken for accepted manuscript content.
        private_draft_text = "НЕ ПУБЛИКОВАТЬ — редакторский рабочий черновик"
        draft = await rs.put(
            first + "/draft", headers=mod,
            json={
                "body_text": private_draft_text, "expected_revision": 0,
                "expected_source_revision": 1, "expected_accepted_revision": 0,
            },
        )
        assert draft.status_code == 200, draft.text
        assert draft.json()["revision"] == 1
        assert draft.json() == (await py.get(first + "/draft", headers=chief)).json()

        # Rust accepts a chapter, Python accepts another, then Python revises
        # the first. The revision history is reconciled from both runtimes.
        first_translation = "<p>Первый опубликованный вариант перевода.</p>"
        second_translation = "<p>Второй готовый перевод.</p>"
        accepted_a = await rs.put(
            first + "/accepted-translation", headers=chief,
            json={
                "translated_html": first_translation,
                "expected_revision": 0, "expected_source_revision": 1,
            },
        )
        accepted_b = await py.put(
            second + "/accepted-translation", headers=mod,
            json={
                "translated_html": second_translation,
                "expected_revision": 0, "expected_source_revision": 1,
            },
        )
        assert accepted_a.status_code == accepted_b.status_code == 200, (
            accepted_a.text, accepted_b.text,
        )
        assert accepted_a.json()["accepted_current"]
        assert accepted_b.json()["accepted_current"]

        # An outdated expected accepted revision cannot silently overwrite.
        stale = await rs.put(
            first + "/accepted-translation", headers=mod,
            json={
                "translated_html": "<p>Нельзя перезаписать старое.</p>",
                "expected_revision": 0, "expected_source_revision": 1,
            },
        )
        assert stale.status_code == 409
        revised = await py.put(
            first + "/accepted-translation", headers=mod,
            json={
                "translated_html": "<p>Исправленная редакция перевода.</p>",
                "expected_revision": 1, "expected_source_revision": 1,
            },
        )
        assert revised.status_code == 200 and revised.json()["accepted_revision"] == 2

        before_restore = await py.get(export_preview, headers=mod)
        assert before_restore.status_code == 200, before_restore.text
        assert before_restore.json()["ready"]
        manifest_before = before_restore.json()["manifest_sha256"]

        restored = await rs.post(
            first + "/restore-accepted", headers=chief,
            json={
                "revision": 1, "expected_current_revision": 2,
                "expected_source_revision": 1,
            },
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["accepted_revision"] == 3
        assert restored.json()["accepted_html"] == first_translation
        assert restored.json() == (await py.get(first, headers=mod)).json()
        history_route = first + "/accepted-revisions"
        assert (await py.get(history_route, headers=mod)).json() == (
            await rs.get(history_route, headers=mod)).json()
        assert [row["revision"] for row in (
            await rs.get(history_route, headers=mod)).json()["revisions"]] == [3, 2, 1]

        # The existing Python export engine proves the user outcome from
        # Rust-authored data. This is NOT a Rust export implementation.
        ready_preview = await py.get(export_preview, headers=mod)
        assert ready_preview.status_code == 200
        preview = ready_preview.json()
        assert preview["ready"] is True, preview
        assert preview["ready_chapter_count"] == 2
        assert preview["issue_count"] == 0
        assert preview["manifest_sha256"] != manifest_before
        assert len(preview["manifest_sha256"]) == 64
        assert private_draft_text not in ready_preview.text
        # This composition test also runs before the separately gated M1f
        # exporter lands. Do not hardcode absence forever: once it exists,
        # demand exact Rust/Python preview parity for this supported fixture.
        native_preview = await rs.get(export_preview, headers=mod)
        if native_preview.status_code == 404:
            native_export_enabled = False
        else:
            native_export_enabled = True
            assert native_preview.status_code == 200, native_preview.text
            assert native_preview.json() == preview

        # Pin and check actual download receipts, not merely the ready flag.
        async def download(fmt: str) -> httpx.Response:
            return await py.get(
                root + "/export", headers=mod,
                params={
                    "expected_last_chapter": 2,
                    "expected_manifest_sha256": preview["manifest_sha256"],
                    "format": fmt,
                },
            )

        assert (await py.get(export_preview)).status_code == 401
        txt, html = await asyncio.gather(download("txt"), download("html"))
        for result, suffix in ((txt, ".txt"), (html, ".html")):
            assert result.status_code == 200, (result.status_code, result.text)
            assert result.headers["x-readera-manifest-sha256"] == preview["manifest_sha256"]
            assert hashlib.sha256(result.content).hexdigest() == (
                result.headers["x-readera-content-sha256"]
            )
            assert result.headers["content-disposition"].endswith(suffix + '"')
            assert result.headers["x-content-type-options"] == "nosniff"
            assert private_draft_text not in result.text
            assert "Перед путником открывается" not in result.text
            assert "В городе продолжается" not in result.text
        assert "Глава 1" in txt.text and "Глава 2" in txt.text
        assert "Первый опубликованный вариант" in txt.text
        assert "Второй готовый перевод" in txt.text
        assert html.text.startswith("<!doctype html>")
        assert "<h2>Глава 1</h2>" in html.text
        assert "<h2>Глава 2</h2>" in html.text

        if native_export_enabled:
            for format_name, python_download in (("txt", txt), ("html", html)):
                native = await rs.get(root + "/export", headers=mod, params={
                    "expected_last_chapter": 2,
                    "expected_manifest_sha256": preview["manifest_sha256"],
                    "format": format_name,
                })
                assert native.status_code == 200, native.text
                assert native.content == python_download.content
                for header_name in ("content-type", "content-disposition",
                                    "x-readera-manifest-sha256",
                                    "x-readera-content-sha256"):
                    assert native.headers[header_name] == python_download.headers[header_name]

        # A previously valid preview stops authorizing export after a change.
        old_download = await py.get(
            root + "/export", headers=mod,
            params={
                "expected_last_chapter": 2, "format": "txt",
                "expected_manifest_sha256": manifest_before,
            },
        )
        assert old_download.status_code == 409
        assert "content-disposition" not in old_download.headers

        missing = await py.get(
            root + "/export/preview?expected_last_chapter=3", headers=mod)
        assert missing.status_code == 200
        assert not missing.json()["ready"]
        assert {"code": "missing_source", "chapter_index": 3} in missing.json()["issues"]

        # Cookie session passes reads, but requires CSRF for the write path.
        cookie = app.state.team_sessions.issue(
            member_id="mod", team_key=settings.team_tokens["mod"], ttl_seconds=1800)
        browser = {"Cookie": "readera_session=" + cookie}
        assert (await rs.get(first, headers=browser)).status_code == 200
        blocked = await rs.put(
            first + "/draft", headers=browser,
            json={
                "body_text": "Forbidden from browser without CSRF",
                "expected_revision": 1,
                "expected_source_revision": 1,
                "expected_accepted_revision": 3,
                "rebase": True,
            },
        )
        assert blocked.status_code == 403
        proof = {**browser, "X-ReadEra-CSRF": TeamSessions.csrf_token(cookie)}
        allowed = await rs.put(
            first + "/draft", headers=proof,
            json={
                "body_text": private_draft_text,
                "expected_revision": 1,
                "expected_source_revision": 1,
                "expected_accepted_revision": 3,
                "rebase": True,
            },
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["revision"] == 2
        assert (await rs.get(first, headers={**proof, "X-API-Key": "incorrect"})).status_code == 401

        # Changing a source invalidates its accepted revision and must fail
        # closed in both the preview and actual download routes.
        changed = await rs.put(
            second, headers=mod,
            json={
                "chapter_index": 2, "chapter_path": "beta.xhtml",
                "source_html": updated_source,
                "expected_revision": 1,
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["source_revision"] == 2
        assert changed.json()["accepted_current"] is False
        assert changed.json() == (await py.get(second, headers=mod)).json()
        stale_preview = (await py.get(export_preview, headers=mod)).json()
        assert not stale_preview["ready"]
        assert {"code": "stale_accepted", "chapter_index": 2} in stale_preview["issues"]
        denied = await download("txt")
        assert denied.status_code == 409
        assert "content-disposition" not in denied.headers

        with sqlite3.connect(db_path) as db:
            assert db.execute(
                "SELECT COUNT(*) FROM book_accepted_revisions WHERE book_id=?",
                (book_id,),
            ).fetchone()[0] == 4
            assert db.execute(
                "SELECT COUNT(*) FROM editor_draft_revisions WHERE book_id=?",
                (book_id,),
            ).fetchone()[0] == 2
            assert db.execute(
                "SELECT COUNT(*) FROM book_glossary_revisions WHERE book_id=?",
                (book_id,),
            ).fetchone()[0] == 1

        print("rust_m1e_real_book_glossary_chapter_draft_accept_restore=PASS")
        print("rust_m1e_python_export_txt_html_exact_receipts=PASS")
        print("rust_m1e_source_change_stale_manifest_blocked=PASS")
        print("rust_m1e_browser_cookie_csrf_and_key_precedence=PASS")
        print("rust_m1e_RUST_export_not_implemented=TRUE")
        print("rust_m1e_RUST_browser_login_not_implemented=TRUE")
        print("production_sqlite_access=no")
        print("gemini_calls=0")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_book_vertical.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-rust-m1e-vertical-") as directory:
        root = Path(directory)
        db_path = root / "disposable.sqlite3"
        settings = Settings(
            db_path=db_path,
            team_tokens={"chief": "m1e-chief", "mod": "m1e-mod"},
            admin_member_id="chief",
            browser_session_secure=False,
        )
        app = create_app(settings, QueueStore(db_path))
        env = dict(os.environ)
        for key in (
            "CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY",
            "CHAPTERA_GEMINI_VAULT_KEY",
        ):
            env.pop(key, None)
        env.update({
            "CHAPTERA_DB_PATH": str(db_path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "ERIDA_RUST_ADDR": "127.0.0.1:18486",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
        })
        runner = launch([binary], env)
        try:
            ready("http://127.0.0.1:18486", runner)
            asyncio.run(scenario(app, settings, db_path, "http://127.0.0.1:18486"))
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
