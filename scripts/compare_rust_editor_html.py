#!/usr/bin/env python3
"""M1l: exact real Python/Rust parity for HTML produced by the current textarea.

Node executes the actual editor encoder. Python ASGI and Rust loopback share a
synthetic SQLite; raw HTML/history and serialized exports are checked separately.
No production database, live translation, external source or VPS changes.
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

ROOT = Path(__file__).resolve().parents[1]


def check(response: httpx.Response, status: int, phase: str) -> None:
    assert response.status_code == status, (
        f"{phase}: expected {status}, got {response.status_code}"
    )


async def exercise(app, path: Path, url: str, settings: Settings, corpus: dict) -> None:
    headers = {"X-API-Key": settings.team_tokens["mod"]}
    comparisons = 0
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://python") as py,
        httpx.AsyncClient(base_url=url, timeout=15) as rs,
    ):
        for case in corpus["cases"]:
            label, raw = case["id"], case["html"]
            book = "m1l-" + label
            root = "/v1/books/" + book
            chapter = root + "/chapters/one"
            acceptance = chapter + "/accepted-translation"
            check(await py.post("/v1/books", headers=headers,
                json={"book_id": book, "title": "Editor corpus " + label}), 201, label + " book")
            check(await py.put(chapter, headers=headers, json={
                "chapter_index": 1, "chapter_path": "1.xhtml",
                "source_html": "<p>Original source must never enter the accepted export.</p>",
                "expected_revision": 0,
            }), 200, label + " source")

            body = lambda html, revision: {
                "translated_html": html, "expected_revision": revision,
                "expected_source_revision": 1,
            }
            # Python writes first, Rust replays WITHOUT another revision/timestamp.
            first = await py.put(acceptance, headers=headers, json=body(raw, 0))
            check(first, 200, label + " Python acceptance")
            replay = await rs.put(acceptance, headers=headers, json=body(raw, 1))
            check(replay, 200, label + " Rust replay")
            assert first.json() == replay.json(), label + " replay differs"
            assert replay.json()["accepted_html"] == raw, label + " changed input bytes"

            # Rust writes and restores; Python must see the same exact old HTML.
            replacement = "<p>Temporary comparison revision.</p>"
            revised = await rs.put(acceptance, headers=headers, json=body(replacement, 1))
            check(revised, 200, label + " Rust write")
            assert revised.json() == (await py.get(chapter, headers=headers)).json()
            restored = await rs.post(chapter + "/restore-accepted", headers=headers, json={
                "revision": 1, "expected_current_revision": 2,
                "expected_source_revision": 1,
            })
            check(restored, 200, label + " Rust restore")
            assert restored.json()["accepted_revision"] == 3
            assert restored.json()["accepted_html"] == raw
            assert restored.json() == (await py.get(chapter, headers=headers)).json()
            for suffix in ("/accepted-revisions", "/accepted-revisions/1", "/accepted-revisions/3"):
                a, b = await py.get(chapter + suffix, headers=headers), await rs.get(chapter + suffix, headers=headers)
                check(a, 200, label + " Python history")
                check(b, 200, label + " Rust history")
                assert a.json() == b.json(), label + " history differs"

            py_preview = await py.get(root + "/export/preview", headers=headers,
                                      params={"expected_last_chapter": 1})
            rs_preview = await rs.get(root + "/export/preview", headers=headers,
                                      params={"expected_last_chapter": 1})
            check(py_preview, 200, label + " Python preview")
            check(rs_preview, 200, label + " Rust preview")
            assert py_preview.json() == rs_preview.json(), label + " preview differs"
            preview = py_preview.json()
            assert preview["ready"] is not case.get("blocked", False), label + " policy"
            digest = preview["manifest_sha256"] or "0" * 64
            for fmt in ("txt", "html"):
                params = {"expected_last_chapter": 1, "expected_manifest_sha256": digest, "format": fmt}
                a, b = await py.get(root + "/export", headers=headers, params=params), await rs.get(root + "/export", headers=headers, params=params)
                expected = 409 if case.get("blocked") else 200
                check(a, expected, label + " Python " + fmt)
                check(b, expected, label + " Rust " + fmt)
                if expected == 409:
                    assert a.json() == b.json(), label + " fail-closed body differs"
                    assert "content-disposition" not in b.headers
                else:
                    assert a.content == b.content, label + " " + fmt + " byte mismatch"
                    for key in ("content-type", "content-disposition", "cache-control",
                                "x-content-type-options", "x-readera-manifest-sha256",
                                "x-readera-content-sha256", "etag"):
                        assert a.headers[key] == b.headers[key], label + " header " + key
                    assert hashlib.sha256(b.content).hexdigest() == b.headers["x-readera-content-sha256"]
                    assert "Original source must never" not in b.text
                    if fmt == "html":
                        assert "<script>" not in b.text and "<img" not in b.text
                comparisons += 1

            with sqlite3.connect(path) as db:
                rows = db.execute(
                    "SELECT revision,translated_html FROM book_accepted_revisions "
                    "WHERE book_id=? ORDER BY revision", (book,),
                ).fetchall()
                assert rows == [(1, raw), (2, replacement), (3, raw)], label + " audit changed"

        # Unknown/active markup remains rejected; no hidden acceptance writes.
        chapter = "/v1/books/m1l-basic/chapters/one"
        before = (await rs.get(chapter, headers=headers)).json()
        for raw in corpus["rejectedHtml"]:
            response = await rs.put(chapter + "/accepted-translation", headers=headers, json={
                "translated_html": raw, "expected_revision": 3, "expected_source_revision": 1,
            })
            check(response, 422, "unsupported HTML")
            assert (await rs.get(chapter, headers=headers)).json() == before
        # Revision fencing still applies to newly supported text, not just old fixtures.
        new_raw = corpus["cases"][1]["html"]
        check(await rs.put(chapter + "/accepted-translation", headers=headers, json={
            "translated_html": new_raw, "expected_revision": 1, "expected_source_revision": 1,
        }), 409, "new grammar stale accepted CAS")
        check(await py.put(chapter, headers=headers, json={
            "chapter_index": 1, "chapter_path": "1.xhtml",
            "source_html": "<p>Changed source after the editor corpus.</p>", "expected_revision": 1,
        }), 200, "source update")
        check(await rs.post(chapter + "/restore-accepted", headers=headers, json={
            "revision": 1, "expected_current_revision": 3, "expected_source_revision": 2,
        }), 409, "old-source restore forbidden")
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT COUNT(*) FROM book_accepted_revisions").fetchone()[0] == 3 * len(corpus["cases"])
        print(f"editor_encoder_cases={len(corpus['cases'])}")
        print(f"editor_export_comparisons={comparisons}")
        print("rust_editor_raw_acceptance_restore_audit_exact=PASS")
        print("rust_editor_python_txt_html_bytes_headers_exact=PASS")
        print("rust_editor_unsupported_markup_and_stale_source_fail_closed=PASS")
        print("production_sqlite_access=NO")
        print("gemini_calls=0")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_editor_html.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    corpus = json.loads(subprocess.check_output(
        ["node", str(ROOT / "tests/browser/editor-text-cases.mjs")],
        cwd=ROOT, text=True, encoding="utf-8", timeout=20,
    ))
    with tempfile.TemporaryDirectory(prefix="erida-m1l-editor-") as folder:
        root = Path(folder)
        path = root / "disposable.sqlite3"
        settings = Settings(db_path=path,
            team_tokens={"chief": "m1l-chief", "mod": "m1l-mod"},
            admin_member_id="chief", browser_session_secure=False)
        app = create_app(settings, QueueStore(path))
        env = dict(os.environ)
        for name in ("CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY",
                     "CHAPTERA_GEMINI_VAULT_KEY", "ERIDA_RUST_TRUST_LOOPBACK_PROXY_CLIENT_IP"):
            env.pop(name, None)
        env.update({"CHAPTERA_DB_PATH": str(path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief", "ERIDA_RUST_ADDR": "127.0.0.1:18494",
            "ERIDA_RUST_TEST_WRITES": "1", "ERIDA_RUST_DISPOSABLE_ROOT": str(root)})
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18494", process)
            asyncio.run(exercise(app, path, "http://127.0.0.1:18494", settings, corpus))
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
