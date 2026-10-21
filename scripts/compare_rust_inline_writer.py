#!/usr/bin/env python3
"""RUST-03g: bounded inline cleaner parity through real shared disposable SQLite.

Python is the source policy oracle. This test must never touch a live DB,
start source crawls, or send paid model requests.
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
from chaptera_translation_core.core.worker_helpers.translation_completeness import (
    strip_source_boilerplate,
)

BOOK = "inline-parity"
PATH = "parts/story.xhtml"
PREFIX = f"/v1/books/{BOOK}/chapters"


def python_hash(html: str, path: str = PATH) -> str:
    clean = strip_source_boilerplate(html, current_chapter_path=path)
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def put_payload(html: str, expected: int, chapter_index: int, path: str = PATH) -> dict:
    return {
        "chapter_index": chapter_index,
        "chapter_path": path,
        "source_html": html,
        "expected_revision": expected,
    }


async def run(app, db_path: Path, base: str, settings: Settings) -> None:
    headers = {"X-API-Key": settings.team_tokens["mod"]}
    inline_first = (
        "<p>Привет <strong>друг</strong>, сказал он.</p>"
        "<p>记住本站</p>"
        "<p>故事 <em>继续</em>。</p>"
    )
    inline_second = (
        "<p>A <b>bold</b> new world.</p>"
        "<p>下一章</p>"
        "<p>New <span>story</span> must be cut.</p>"
    )
    next_python = (
        "<p>История <i>продолжается</i>.</p>"
        "<p>本章完</p>"
        "<p>Следующая <strong>сцена</strong>.</p>"
    )
    next_rust = (
        "<p>Главный <em>герой</em> вернулся.</p>"
        "<p>最新小说</p>"
        "<p>Дождь <b>закончился</b>.</p>"
    )

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://python"
        ) as py,
        httpx.AsyncClient(base_url=base, timeout=15) as rust,
    ):
        created = await py.post(
            "/v1/books", headers=headers,
            json={"book_id": BOOK, "title": "RUST-03g disposable inline parity"},
        )
        assert created.status_code == 201, (created.status_code, created.text)

        # Rust writes original HTML and the Python-cleaned hash, not a hash
        # of the raw source. Python replays it without minting a revision.
        original = await rust.put(
            PREFIX + "/one", headers=headers, json=put_payload(inline_first, 0, 1),
        )
        assert original.status_code == 200, (original.status_code, original.text)
        first = original.json()
        assert first["source_html"] == inline_first
        assert first["source_hash"] == python_hash(inline_first)
        assert first["source_hash"] != hashlib.sha256(inline_first.encode()).hexdigest()
        replay = await py.put(
            PREFIX + "/one", headers=headers, json=put_payload(inline_first, 1, 1),
        )
        assert replay.status_code == 200, (replay.status_code, replay.text)
        assert replay.json() == first

        # Reverse direction: Python creates a nested+tail source; Rust must
        # reproduce the same stored record and policy hash on replay.
        python_created = await py.put(
            PREFIX + "/two", headers=headers,
            json=put_payload(inline_second, 0, 2, "parts/notes.xhtml"),
        )
        assert python_created.status_code == 200, (python_created.status_code, python_created.text)
        reverse = await rust.put(
            PREFIX + "/two", headers=headers,
            json=put_payload(inline_second, 1, 2, "parts/notes.xhtml"),
        )
        assert reverse.status_code == 200, (reverse.status_code, reverse.text)
        assert reverse.json() == python_created.json()
        assert reverse.json()["source_hash"] == python_hash(inline_second, "parts/notes.xhtml")

        # Acceptance must survive a source revision as a stale translation.
        accepted = await py.put(
            PREFIX + "/one/accepted-translation", headers=headers,
            json={
                "translated_html": "<p>Уже принятый перевод.</p>",
                "expected_revision": 0,
                "expected_source_revision": 1,
            },
        )
        assert accepted.status_code == 200, (accepted.status_code, accepted.text)

        # Independent Python and Rust writers race against one SQLite CAS.
        competing = await asyncio.gather(
            py.put(
                PREFIX + "/one", headers=headers,
                json=put_payload(next_python, 1, 1),
            ),
            rust.put(
                PREFIX + "/one", headers=headers,
                json=put_payload(next_rust, 1, 1),
            ),
        )
        assert sorted(x.status_code for x in competing) == [200, 409], [
            (x.status_code, x.text) for x in competing
        ]
        read_py = await py.get(PREFIX + "/one", headers=headers)
        read_rs = await rust.get(PREFIX + "/one", headers=headers)
        assert read_py.status_code == read_rs.status_code == 200
        assert read_py.json() == read_rs.json()
        latest = read_rs.json()
        assert latest["source_revision"] == 2
        assert latest["source_html"] in (next_python, next_rust)
        assert latest["source_hash"] == python_hash(latest["source_html"])
        assert latest["accepted_revision"] == 1
        assert latest["accepted_current"] is False
        assert latest["accepted_html"] == "<p>Уже принятый перевод.</p>"

        # Reject ambiguous nested source, parser recovery, entities, and
        # chapter-number context before any SQLite mutation.
        invalid = [
            ("<p>Hello <b>记住本站</b> friend.</p>", PATH),
            ("<p>Story <span>下一章</span> continues.</p>", PATH),
            ("<p>End <strong>without a close.</p>", PATH),
            ("<p>Story <em>text</em> &amp; more.</p>", PATH),
            ("<p>Hello <b class='x'>world</b>.</p>", PATH),
            ("<p>Normal <em>story</em>.</p>", "chapters/7.xhtml"),
        ]
        for idx, (html, path) in enumerate(invalid, 10):
            response = await rust.put(
                PREFIX + f"/invalid-{idx}", headers=headers,
                json=put_payload(html, 0, idx, path),
            )
            assert response.status_code == 422, (
                idx, response.status_code, response.text
            )
            assert "Python source-policy normalization required" in response.text

        with sqlite3.connect(db_path) as db:
            records = db.execute(
                "SELECT chapter_id,chapter_path,source_revision,source_html,source_hash,"
                "accepted_revision,accepted_source_revision FROM book_chapters "
                "WHERE book_id=? ORDER BY chapter_id", (BOOK,),
            ).fetchall()
        assert len(records) == 2, records
        for chapter_id, path, rev, raw, digest, accepted_rev, accepted_source_rev in records:
            assert digest == python_hash(raw, path)
            assert chapter_id in ("one", "two")
            assert rev in (1, 2)
            if chapter_id == "one":
                assert rev == 2 and accepted_rev == 1 and accepted_source_rev == 1

        no_auth = await rust.put(
            PREFIX + "/no-auth", json=put_payload(inline_first, 0, 20),
        )
        assert no_auth.status_code == 401

    print("rust_inline_writer_python_cleaned_sha256=PASS")
    print("rust_inline_writer_original_html=PASS")
    print("rust_inline_writer_bidirectional_replay=PASS")
    print("rust_inline_writer_concurrent_cas=PASS")
    print("rust_inline_writer_stale_accepted_translation=PASS")
    print("rust_inline_writer_rejects_ambiguous_html_without_db_mutation=PASS")
    print("rust_inline_writer_auth=PASS")
    print("rust_inline_writer_production_db_access=no")
    print("source_network_calls=0")
    print("gemini_calls=0")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_inline_writer.py /path/to/erida-api")
    executable = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-rust03g-inline-write-") as folder:
        root = Path(folder)
        db_path = root / "isolated.sqlite3"
        settings = Settings(
            db_path=db_path,
            team_tokens={"chief": "rust03g-chief", "mod": "rust03g-mod"},
            admin_member_id="chief",
            browser_session_secure=False,
        )
        app = create_app(settings, QueueStore(db_path))
        env = dict(os.environ)
        for name in (
            "CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY",
            "CHAPTERA_GEMINI_VAULT_KEY",
        ):
            env.pop(name, None)
        env.update({
            "CHAPTERA_DB_PATH": str(db_path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "ERIDA_RUST_ADDR": "127.0.0.1:18479",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
        })
        process = launch([executable], env)
        try:
            ready("http://127.0.0.1:18479", process)
            asyncio.run(run(app, db_path, "http://127.0.0.1:18479", settings))
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
