#!/usr/bin/env python3
"""M1f real Rust export preview/download versus Python VerifiedBookExport.

Tests two separate HTTP runtimes on one isolated SQLite, bytes/hashes/headers
and fail-closed behavior. Never touches VPS, Gemini or external providers.
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


async def exercise(app, settings: Settings, root: Path, endpoint: str) -> None:
    book_id = "m1f-book"
    base = f"/v1/books/{book_id}"
    preview_path = base + "/export/preview"
    headers = {"X-API-Key": settings.team_tokens["mod"]}

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                          base_url="http://python", timeout=15) as py,
        httpx.AsyncClient(base_url=endpoint, timeout=15) as rs,
    ):
        book = await py.post("/v1/books", headers=headers, json={
            "book_id": book_id, "title": "Сборник <важный>",
        })
        assert book.status_code == 201, book.text
        for index, html in enumerate((
            "<h1>Глава 1</h1><p>Привет <strong>мир</strong>.</p>",
            "<p>Вторая глава.<br/>Новая строка.</p>",
        ), 1):
            path = base + f"/chapters/c{index}"
            source = await py.put(path, headers=headers, json={
                "chapter_index": index, "chapter_path": f"source{index}.xhtml",
                "source_html": f"<p>Оригинал номер {index}.</p>",
                "expected_revision": 0,
            })
            assert source.status_code == 200, source.text
            accepted = await py.put(
                path + "/accepted-translation", headers=headers,
                json={"translated_html": html, "expected_revision": 0,
                      "expected_source_revision": 1},
            )
            assert accepted.status_code == 200, accepted.text

        async def preview(client, count=2):
            return await client.get(preview_path, headers=headers,
                                    params={"expected_last_chapter": count})

        python_report = await preview(py)
        rust_report = await preview(rs)
        assert python_report.status_code == rust_report.status_code == 200
        assert python_report.json() == rust_report.json(), (
            python_report.text, rust_report.text,
        )
        model = python_report.json()
        assert model["ready"] is True and model["ready_chapter_count"] == 2
        receipt = model["manifest_sha256"]
        assert len(receipt) == 64
        assert "Привет" not in rust_report.text

        async def download(client, fmt, sha=receipt, count=2):
            return await client.get(base + "/export", headers=headers, params={
                "expected_last_chapter": count,
                "expected_manifest_sha256": sha, "format": fmt,
            })

        for fmt in ("txt", "html"):
            a, b = await asyncio.gather(download(py, fmt), download(rs, fmt))
            assert a.status_code == b.status_code == 200, (
                fmt, a.status_code, b.status_code, a.text[:500], b.text[:500],
            )
            assert a.content == b.content, (fmt, a.text, b.text)
            for key in (
                "content-type", "content-disposition", "cache-control",
                "x-content-type-options", "x-readera-manifest-sha256",
                "x-readera-content-sha256", "etag",
            ):
                assert a.headers[key] == b.headers[key], (
                    fmt, key, a.headers.get(key), b.headers.get(key),
                )
            assert hashlib.sha256(b.content).hexdigest() == (
                b.headers["x-readera-content-sha256"]
            )
            assert b.headers["x-readera-manifest-sha256"] == receipt
            assert "Оригинал номер" not in b.text
            assert "<h1>Глава 1</h1>" not in b.text
            assert "Глава 1\n\nПривет мир." in b.text if fmt == "txt" else (
                "<h2>Глава 1</h2>" in b.text)
        # Valid query is mandatory: HTTP query validation may precede auth.
        # Assert auth on a structurally valid request rather than assuming
        # malformed unauthenticated requests always return 401.
        assert (await rs.get(preview_path, params={
            "expected_last_chapter": 2,
        })).status_code == 401
        assert (await py.get(preview_path, params={
            "expected_last_chapter": 2,
        })).status_code == 401
        assert (await rs.get(base + "/export", params={
            "expected_last_chapter": 2, "expected_manifest_sha256": receipt,
            "format": "txt",
        })).status_code == 401

        # A manifest from before an acceptance change cannot authorize bytes.
        before_change = await download(rs, "txt")
        assert before_change.status_code == 200
        edited = await py.put(
            base + "/chapters/c1/accepted-translation", headers=headers,
            json={
                "translated_html": "<p>Исправленный принятый перевод.</p>",
                "expected_revision": 1, "expected_source_revision": 1,
            },
        )
        assert edited.status_code == 200
        for client in (py, rs):
            denied = await download(client, "txt")
            assert denied.status_code == 409, denied.text
            assert denied.json()["detail"]["message"] == (
                "Book export is incomplete or changed after preview"
            )
            assert "content-disposition" not in denied.headers
        new_preview_py = await preview(py)
        new_preview_rs = await preview(rs)
        assert new_preview_py.json() == new_preview_rs.json()
        new_receipt = new_preview_rs.json()["manifest_sha256"]
        assert new_receipt != receipt

        for client in (py, rs):
            missing = await preview(client, 3)
            assert missing.status_code == 200
            assert {"code": "missing_source", "chapter_index": 3} in missing.json()["issues"]
            assert missing.json()["manifest_sha256"] is None
            assert (await download(client, "html", sha=new_receipt, count=3)).status_code == 409
            assert (await client.get(
                "/v1/books/does-not-exist/export/preview",
                params={"expected_last_chapter": 1},
                headers=headers,
            )).status_code == 404

        # Rust must refuse previously accepted text when its source changes.
        changed = await py.put(
            base + "/chapters/c2", headers=headers,
            json={
                "chapter_index": 2, "chapter_path": "source2.xhtml",
                "source_html": "<p>Обновлён исходный текст.</p>",
                "expected_revision": 1,
            },
        )
        assert changed.status_code == 200
        stale_py, stale_rs = await asyncio.gather(preview(py), preview(rs))
        assert stale_py.json() == stale_rs.json(), (stale_py.text, stale_rs.text)
        assert {"code": "stale_accepted", "chapter_index": 2} in stale_rs.json()["issues"]
        assert (await download(rs, "txt", sha=new_receipt)).status_code == 409

        # Python accepts and sanitizes attribute-bearing HTML, but M1f Rust
        # does not yet prove equivalent BeautifulSoup sanitization and MUST
        # fail closed, not silently export untested bytes.
        reaccepted = await py.put(
            base + "/chapters/c2/accepted-translation", headers=headers,
            json={
                "translated_html": '<p style="color:red">Третья редакция.</p>',
                "expected_revision": 1, "expected_source_revision": 2,
            },
        )
        assert reaccepted.status_code == 200, reaccepted.text
        py_sanitizable = await preview(py)
        rust_conservative = await preview(rs)
        assert py_sanitizable.json()["ready"] is True
        assert rust_conservative.json()["ready"] is False
        assert {"code": "unsafe_accepted_html", "chapter_index": 2,
                "reason": "rust_unsupported_fragment"} in rust_conservative.json()["issues"]
        assert (await download(rs, "txt", sha=py_sanitizable.json()["manifest_sha256"])).status_code == 409

        # The render is a read-only operation: no audit changes, no draft.
        with sqlite3.connect(root) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM book_accepted_revisions WHERE book_id=?",
                (book_id,),
            ).fetchone()[0] == 4
            assert connection.execute(
                "SELECT COUNT(*) FROM editor_draft_revisions WHERE book_id=?",
                (book_id,),
            ).fetchone()[0] == 0

        print("rust_export_preview_manifest_exact_python_parity=PASS")
        print("rust_export_txt_html_bytes_headers_hashes_exact_python_parity=PASS")
        print("rust_export_stale_source_and_preview_blocked=PASS")
        print("rust_export_unproven_html_fails_closed=PASS")
        print("rust_export_no_production_sqlite_or_gemini=PASS")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_book_export.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m1f-export-") as directory:
        root = Path(directory)
        path = root / "disposable.sqlite3"
        settings = Settings(
            db_path=path, team_tokens={
                "chief": "m1f-chief", "mod": "m1f-mod",
            }, admin_member_id="chief", browser_session_secure=False,
        )
        app = create_app(settings, QueueStore(path))
        env = dict(os.environ)
        for key in (
            "CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY",
            "CHAPTERA_GEMINI_VAULT_KEY",
        ):
            env.pop(key, None)
        env.update({
            "CHAPTERA_DB_PATH": str(path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "ERIDA_RUST_ADDR": "127.0.0.1:18487",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
        })
        proc = launch([binary], env)
        try:
            ready("http://127.0.0.1:18487", proc)
            asyncio.run(exercise(app, settings, path, "http://127.0.0.1:18487"))
            assert proc.poll() is None
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
