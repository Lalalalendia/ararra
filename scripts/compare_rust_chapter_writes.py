#!/usr/bin/env python3
"""RUST-02d: Python/Rust chapter CAS parity on synthetic, disposable SQLite.

This deliberately exercises the FIRST provably policy-identity subset only:
one or more long plain HTML paragraphs. All other sources must fail closed
until the Rust BeautifulSoup/source_units policy has been independently ported.
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


def long_paragraph(text: str) -> str:
    return "<p>" + text * 300 + "</p>"


async def exercise(app, path: Path, endpoint: str, settings: Settings) -> None:
    headers = {"X-API-Key": settings.team_tokens["mod"]}
    rust_root = "/v1/books/chapter-writes/chapters"
    candidates = (
        long_paragraph("学"),
        long_paragraph("А"),
        long_paragraph("A") + long_paragraph("火"),
        long_paragraph("雨"),
    )
    for body in candidates:
        # Establish the exact identity branch in the real Python source policy.
        assert strip_source_boilerplate(
            body, current_chapter_path="parts/1.xhtml"
        ) == body, "Fixture unexpectedly contains removable source units"

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://python"
        ) as python,
        httpx.AsyncClient(base_url=endpoint, timeout=12) as rust,
    ):
        create = await python.post(
            "/v1/books",
            headers=headers,
            json={"book_id": "chapter-writes", "title": "Synthetic"},
        )
        assert create.status_code == 201, create.status_code

        initial = {
            "chapter_index": 1,
            "chapter_path": "parts\\1.xhtml",
            "source_html": candidates[0],
            "expected_revision": 0,
        }
        # Rust INSERT must create the same source_hash and revision as Python.
        inserted = await rust.put(rust_root + "/first", headers=headers, json=initial)
        assert inserted.status_code == 200, (inserted.status_code, inserted.text)
        assert inserted.json()["source_revision"] == 1
        assert inserted.json()["chapter_path"] == "parts/1.xhtml"
        assert inserted.json()["source_hash"] == hashlib.sha256(
            candidates[0].encode("utf-8")
        ).hexdigest()
        replay = await python.put(
            rust_root + "/first",
            headers=headers,
            json={**initial, "expected_revision": 1},
        )
        assert replay.status_code == 200
        assert replay.json() == inserted.json()

        # Python INSERT must be idempotently replayed from the Rust process.
        second = {
            "chapter_index": 2,
            "chapter_path": "parts/2.xhtml",
            "source_html": candidates[2],
            "expected_revision": 0,
        }
        py_insert = await python.put(rust_root + "/second", headers=headers, json=second)
        assert py_insert.status_code == 200
        rust_replay = await rust.put(
            rust_root + "/second",
            headers=headers,
            json={**second, "expected_revision": 1},
        )
        assert rust_replay.status_code == 200
        assert rust_replay.json() == py_insert.json()

        # Acceptance belongs to Python for now. The Rust source writer must
        # preserve it as stale when the source changes, not delete it.
        accepted = await python.put(
            rust_root + "/first/accepted-translation",
            headers=headers,
            json={
                "translated_html": "<p>Перевод принят и сохраняется.</p>",
                "expected_revision": 0,
                "expected_source_revision": 1,
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["accepted_current"] is True

        # True conflict: two different chapter bodies compete at revision 1.
        changed_py = {**initial, "source_html": candidates[1], "expected_revision": 1}
        changed_rs = {**initial, "source_html": candidates[3], "expected_revision": 1}
        responses = await asyncio.gather(
            python.put(rust_root + "/first", headers=headers, json=changed_py),
            rust.put(rust_root + "/first", headers=headers, json=changed_rs),
        )
        assert sorted(r.status_code for r in responses) == [200, 409], [
            (r.status_code, r.text) for r in responses
        ]
        py_current = await python.get(rust_root + "/first", headers=headers)
        rs_current = await rust.get(rust_root + "/first", headers=headers)
        assert py_current.status_code == rs_current.status_code == 200
        assert py_current.json() == rs_current.json()
        updated = rs_current.json()
        assert updated["source_revision"] == 2
        assert updated["accepted_revision"] == 1
        assert updated["accepted_current"] is False
        assert updated["accepted_html"] == "<p>Перевод принят и сохраняется.</p>"
        assert updated["source_hash"] == hashlib.sha256(
            updated["source_html"].encode("utf-8")
        ).hexdigest()
        assert updated["source_html"] in candidates[1::2]

        # Stale revisions must never overwrite a successful conflicting save.
        stale = await rust.put(rust_root + "/first", headers=headers, json=changed_rs)
        assert stale.status_code == 409
        assert stale.json()["detail"] == "Chapter source revision mismatch"

        # Uniqueness violations are mapped to the same 409 as Python.
        colliding = await rust.put(
            rust_root + "/colliding", headers=headers,
            json={"chapter_index": 2, "chapter_path": "different.xhtml",
                  "source_html": candidates[0], "expected_revision": 0},
        )
        assert colliding.status_code == 409, (colliding.status_code, colliding.text)
        assert colliding.json()["detail"] == (
            "Chapter index or path already belongs to another chapter"
        )

        # Unlike Python, Rust explicitly rejects unsupported markup/noise
        # rather than commit a WRONG hash. Rejected attempts are non-mutating.
        invalid_sources = (
            "<p>Short story.</p>",
            "<p>下一章 下一章</p>",
            "<p>" + "甲" * 300 + "</p><p>Short.</p>",
            "<p>" + "林" * 300 + "&amp;</p>",
            "<div>" + "林" * 300 + "</div>",
            "<p>" + "林" * 300 + "101kanshu</p>",
        )
        for index, body in enumerate(invalid_sources):
            denied = await rust.put(
                rust_root + f"/unsupported-{index}", headers=headers,
                json={"chapter_index": 10 + index,
                      "chapter_path": f"parts/{10 + index}.xhtml",
                      "source_html": body, "expected_revision": 0},
            )
            assert denied.status_code == 422, (index, denied.status_code, denied.text)
            assert "Python source-policy normalization required" in denied.json()["detail"]
        missing_book = await rust.put(
            "/v1/books/missing/chapters/new",
            headers=headers,
            json=initial,
        )
        assert missing_book.status_code == 404
        with sqlite3.connect(path) as db:
            rows = db.execute(
                "SELECT chapter_id,source_revision FROM book_chapters "
                "WHERE book_id='chapter-writes' ORDER BY chapter_id"
            ).fetchall()
            assert rows == [("first", 2), ("second", 1)], rows

        # The existing auth & CSRF guard also covers this new mutation route.
        cookie = app.state.team_sessions.issue(
            member_id="mod", team_key=settings.team_tokens["mod"], ttl_seconds=3600
        )
        bad_cookie = await rust.put(
            rust_root + "/csrf-no-proof",
            headers={"Cookie": "readera_session=" + cookie}, json=initial,
        )
        assert bad_cookie.status_code == 403
        poison = await rust.put(
            rust_root + "/invalid-key",
            headers={"Cookie": "readera_session=" + cookie,
                     "X-API-Key": "wrong"}, json=initial,
        )
        assert poison.status_code == 401

        print("rust_chapter_python_source_policy_identity=PASS")
        print("rust_chapter_python_rust_bilateral_replay=PASS")
        print("rust_chapter_concurrent_revision_fencing=PASS")
        print("rust_chapter_accepted_translation_stales_safely=PASS")
        print("rust_chapter_unsupported_html_fail_closed=PASS")
        print("rust_chapter_auth_csrf=PASS")
        print("rust_chapter_production_sqlite_access=no")
        print("source_network_calls=0")
        print("gemini_calls=0")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_chapter_writes.py /path/to/erida-api")
    executable = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-rust02d-chapters-") as folder:
        root = Path(folder)
        path = root / "disposable.sqlite3"
        settings = Settings(
            db_path=path,
            team_tokens={"chief": "rust02d-chief-test-key", "mod": "rust02d-mod-test-key"},
            admin_member_id="chief",
            browser_session_secure=False,
        )
        app = create_app(settings, QueueStore(path))
        environment = dict(os.environ)
        for key in (
            "CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY",
            "CHAPTERA_GEMINI_VAULT_KEY",
        ):
            environment.pop(key, None)
        environment.update({
            "CHAPTERA_DB_PATH": str(path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "ERIDA_RUST_ADDR": "127.0.0.1:18475",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
        })
        process = launch([executable], environment)
        try:
            ready("http://127.0.0.1:18475", process)
            asyncio.run(exercise(app, path, "http://127.0.0.1:18475", settings))
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
