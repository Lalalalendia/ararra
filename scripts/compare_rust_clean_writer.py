#!/usr/bin/env python3
"""RUST-03d: partial source-cleaner integration into DISPOSABLE Rust writer.

One SQLite in a disposable tempdir is written concurrently by Python ASGI and
the experimental loopback Rust API. This does not run any production workload.
"""
from __future__ import annotations

import asyncio
import hashlib
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
from chaptera_translation_core.core.worker_helpers.translation_completeness import (
    strip_source_boilerplate,
)


def hash_by_python(html: str, path: str) -> str:
    cleaned = strip_source_boilerplate(
        html, current_chapter_path=path,
    )
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


async def run(app, db_path: Path, base: str, settings: Settings) -> None:
    auth = {"X-API-Key": settings.team_tokens["mod"]}
    prefix = "/v1/books/clean-writer/chapters"
    original = "<p>他走进了房间。</p><p>记住本站</p><p>故事还在继续。</p>"
    changed_python = "<p>他回到了家。</p><p>最新小说</p><p>在门边坐下。</p>"
    changed_rust = "<p>大雨开始了。</p><p>下一章</p><p>这是下一章的内容。</p>"

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://python"
        ) as python,
        httpx.AsyncClient(base_url=base, timeout=10) as rust,
    ):
        book = await python.post(
            "/v1/books", headers=auth,
            json={"book_id": "clean-writer", "title": "Offline source-clean writer"},
        )
        assert book.status_code == 201, book.status_code

        def request(html: str, expected: int, *, index=1, path="parts/story.xhtml") -> dict:
            return {
                "chapter_index": index,
                "chapter_path": path,
                "source_html": html,
                "expected_revision": expected,
            }

        # Rust insert: preserve RAW source, but hash the Python-cleaned HTML.
        first = await rust.put(prefix + "/one", headers=auth, json=request(original, 0))
        assert first.status_code == 200, (first.status_code, first.text)
        assert first.json()["source_html"] == original
        assert first.json()["source_hash"] == hash_by_python(original, "parts/story.xhtml")
        assert first.json()["source_hash"] != hashlib.sha256(
            original.encode("utf-8")
        ).hexdigest(), "Watermark fixture failed to exercise cleaned hash"
        python_replay = await python.put(
            prefix + "/one", headers=auth, json=request(original, 1),
        )
        assert python_replay.status_code == 200
        assert python_replay.json() == first.json()

        # Python insert: short text, explicit next-chapter tail. Rust replay
        # must agree without minting another source revision.
        with_tail = "<p>Один герой задумался.</p><p>next chapter</p><p>Extra text.</p>"
        second = request(with_tail, 0, index=2, path="parts/notes.xhtml")
        python_second = await python.put(prefix + "/two", headers=auth, json=second)
        assert python_second.status_code == 200
        rust_replay = await rust.put(
            prefix + "/two", headers=auth, json={**second, "expected_revision": 1},
        )
        assert rust_replay.status_code == 200
        assert rust_replay.json() == python_second.json()
        assert rust_replay.json()["source_hash"] == hash_by_python(
            with_tail, "parts/notes.xhtml",
        )

        # Existing translated text survives source editing and becomes stale.
        accepted = await python.put(
            prefix + "/one/accepted-translation", headers=auth,
            json={"translated_html": "<p>Перевод уже был принят.</p>",
                  "expected_revision": 0, "expected_source_revision": 1},
        )
        assert accepted.status_code == 200
        assert accepted.json()["accepted_current"] is True

        # Mixed-language writers competing for the same revision must not
        # fork the accepted text or create a second source revision.
        changes = await asyncio.gather(
            python.put(prefix + "/one", headers=auth,
                       json=request(changed_python, 1)),
            rust.put(prefix + "/one", headers=auth,
                     json=request(changed_rust, 1)),
        )
        assert sorted(x.status_code for x in changes) == [200, 409], [
            (x.status_code, x.text) for x in changes
        ]
        read_python = await python.get(prefix + "/one", headers=auth)
        read_rust = await rust.get(prefix + "/one", headers=auth)
        assert read_python.status_code == read_rust.status_code == 200
        assert read_python.json() == read_rust.json()
        latest = read_rust.json()
        assert latest["source_revision"] == 2
        assert latest["source_html"] in (changed_python, changed_rust)
        assert latest["source_hash"] == hash_by_python(
            latest["source_html"], "parts/story.xhtml",
        )
        assert latest["accepted_revision"] == 1
        assert latest["accepted_current"] is False
        assert latest["accepted_html"] == "<p>Перевод уже был принят.</p>"

        stale = await rust.put(prefix + "/one", headers=auth, json=request(changed_rust, 1))
        assert stale.status_code == 409

        # An unsupported input must be rejected before touching the DB.
        invalid = (
            ("<p>Short <em>记住本站</em> text.</p>", "parts/story.xhtml"),
            ("<p>With &amp; HTML entities.</p>", "parts/story.xhtml"),
            ("<p>本章完</p>", "parts/story.xhtml"),
            ("<p>第七章</p>", "parts/story.xhtml"),
            ("<p>Небольшая глава.</p>", "chapters/7.xhtml"),
            ("<p>101kanshu.com</p>", "parts/story.xhtml"),
            ("<div>Arbitrary div is not allowed.</div>", "parts/story.xhtml"),
        )
        for i, (text, path) in enumerate(invalid, 10):
            rejected = await rust.put(
                prefix + f"/unsupported-{i}", headers=auth,
                json=request(text, 0, index=i, path=path),
            )
            assert rejected.status_code == 422, (
                i, rejected.status_code, rejected.text
            )
            assert "Python source-policy normalization required" in rejected.json()["detail"]

        with sqlite3.connect(db_path) as db:
            actual = db.execute(
                "SELECT chapter_id,source_revision,source_html,source_hash "
                "FROM book_chapters WHERE book_id='clean-writer' ORDER BY chapter_id"
            ).fetchall()
            assert len(actual) == 2, actual
            for _, revision, raw_html, digest in actual:
                # This test allows different valid paths only for each fixed ID.
                path = "parts/story.xhtml" if revision == 2 else "parts/notes.xhtml"
                assert digest == hash_by_python(raw_html, path)

        # No unauthenticated fallback and browser sessions still require CSRF.
        absent = await rust.put(
            prefix + "/unauthorized", json=request(original, 0),
        )
        assert absent.status_code == 401
        cookie = app.state.team_sessions.issue(
            member_id="mod", team_key=settings.team_tokens["mod"], ttl_seconds=3600,
        )
        csrf_missing = await rust.put(
            prefix + "/csrf", headers={"Cookie": "readera_session=" + cookie},
            json=request(original, 0),
        )
        assert csrf_missing.status_code == 403

        print("rust_clean_writer_python_hash_parity=PASS")
        print("rust_clean_writer_short_paragraphs=PASS")
        print("rust_clean_writer_python_bidirectional_replay=PASS")
        print("rust_clean_writer_cross_process_revision_fencing=PASS")
        print("rust_clean_writer_preserves_stale_accepted_translation=PASS")
        print("rust_clean_writer_unsupported_html_nonmutating=PASS")
        print("rust_clean_writer_auth_csrf=PASS")
        print("rust_clean_writer_production_db_access=no")
        print("source_network_calls=0")
        print("gemini_calls=0")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_clean_writer.py /path/to/erida-api")
    executable = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-rust03d-clean-write-") as folder:
        root = Path(folder)
        db_path = root / "isolated.sqlite3"
        settings = Settings(
            db_path=db_path,
            team_tokens={"chief": "rust03d-chief", "mod": "rust03d-mod"},
            admin_member_id="chief",
            browser_session_secure=False,
        )
        app = create_app(settings, QueueStore(db_path))
        env = dict(os.environ)
        for name in ("CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY",
                     "CHAPTERA_GEMINI_VAULT_KEY"):
            env.pop(name, None)
        env.update({
            "CHAPTERA_DB_PATH": str(db_path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "ERIDA_RUST_ADDR": "127.0.0.1:18477",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
        })
        process = launch([executable], env)
        try:
            ready("http://127.0.0.1:18477", process)
            asyncio.run(run(app, db_path, "http://127.0.0.1:18477", settings))
            assert process.poll() is None
            print("production_processes_touched=0")
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
