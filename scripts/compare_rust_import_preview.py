#!/usr/bin/env python3
"""M1n: authenticated Rust/Python read-only TXT upload preview parity.

All HTTP uses separate local processes on the SAME disposable SQLite.
No Rust write gate, import stage, confirmation, Gemini or VPS access.
"""
from __future__ import annotations

import asyncio
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

PREVIEW = "/v1/imports/preview"


async def exercise(app, db_path: Path, rust_url: str, settings: Settings) -> None:
    token = settings.team_tokens["mod"]
    headers = {"X-API-Key": token}
    fixtures = [
        ("novel.txt", "Глава 1\nПервый абзац.\nВторая строка.\n\n"
                      "Глава 2\nПоследний абзац.".encode()),
        ("plain.txt", b"First paragraph\n\nSecond paragraph"),
        ("duplicate.txt", "Глава 1\nОдин\nГлава 1\nОдин".encode()),
        ("gaps.txt", "Предисловие.\n\nГлава 12\nНачало\n"
                     "Глава 14\nЗавершение".encode()),
        ("utf8.txt", b"\xef\xbb\xbfChapter 7\nText & quotes 'x' and \"y\"."),
        ("literal.txt", "Глава 5\nA & B <b> — буквальный текст, не тег.\n\n"
                        "Последняя строка 😀".encode()),
        ("empty.txt", "Глава 1\n\nГлава 2\nПолный текст".encode()),
    ]
    with sqlite3.connect(db_path) as con:
        before = {
            name: con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("books", "book_chapters", "book_import_stages")
        }

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://python",
            timeout=15,
        ) as py,
        httpx.AsyncClient(base_url=rust_url, timeout=30) as rs,
    ):
        for filename, content in fixtures:
            payload = {"file": (filename, content, "text/plain")}
            reference = await py.post(PREVIEW, headers=headers, files=payload)
            candidate = await rs.post(PREVIEW, headers=headers, files=payload)
            assert reference.status_code == candidate.status_code == 200, (
                filename, reference.status_code, candidate.status_code
            )
            assert reference.json() == candidate.json(), (
                filename,
                [(k, reference.json().get(k), candidate.json().get(k))
                 for k in reference.json()
                 if reference.json().get(k) != candidate.json().get(k)],
            )
            assert candidate.json()["stored"] is False
            assert "no-store" in candidate.headers["cache-control"]
            assert candidate.headers["x-content-type-options"] == "nosniff"
            assert "source_html" not in candidate.text
            assert "secret" not in candidate.text

        unauthorized = await rs.post(
            PREVIEW, files={"file": ("book.txt", b"Chapter 1\nText", "text/plain")},
        )
        assert unauthorized.status_code == 401, unauthorized.status_code
        bad_key = await rs.post(
            PREVIEW, headers={"X-API-Key": "wrong"},
            files={"file": ("book.txt", b"Chapter 1\nText", "text/plain")},
        )
        assert bad_key.status_code == 401

        # Python supports these inputs, but Rust must NOT silently reinterpret
        # unported heading grammars or legacy encodings.
        unsupported = [
            ("title.txt", "Глава 1. Название\nТекст".encode()),
            ("markdown.txt", "# Chapter 1\nText".encode()),
            ("legacy.txt", "Глава 7. История\nТекст".encode("cp1251")),
            ("cjk.txt", "第一百零二章 旅程\n山河".encode()),
            ("chapter.html", b"<h1>Chapter 1</h1><p>Text</p>"),
        ]
        for filename, content in unsupported:
            candidate = await rs.post(
                PREVIEW, headers=headers,
                files={"file": (filename, content, "application/octet-stream")},
            )
            assert candidate.status_code == 422, (
                filename, candidate.status_code
            )
            assert "no-store" in candidate.headers["cache-control"]
            assert "stored" not in candidate.text

        oversized = await rs.post(
            PREVIEW, headers=headers,
            files={"file": ("too-big.txt", b"A" * (12 * 1024 * 1024 + 1),
                            "text/plain")},
        )
        assert oversized.status_code == 413, oversized.status_code

        # This slice does not register stage/confirm routes. A successful
        # preview is never a durable import or a claim of complete UI support.
        not_staged = await rs.post(
            "/v1/imports/stages", headers=headers,
            files={"file": ("book.txt", b"Chapter 1\nText", "text/plain")},
        )
        assert not_staged.status_code in (404, 405)

    with sqlite3.connect(db_path) as con:
        after = {
            name: con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in before
        }
        assert before == after == {
            "books": 0, "book_chapters": 0, "book_import_stages": 0,
        }, (before, after)

    print("rust_txt_import_preview_python_json_exact=PASS")
    print("rust_txt_import_preview_multipart_auth_headers=PASS")
    print("rust_txt_import_preview_unsupported_grammar_fail_closed=PASS")
    print("rust_txt_import_preview_oversized_413=PASS")
    print("rust_txt_import_preview_sqlite_zero_writes=PASS")
    print("rust_stage_and_confirm_available=NO")
    print("rust_import_ui_chromium_available=NO")
    print("production_sqlite_access=NO")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_import_preview.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m1n-import-preview-") as folder:
        root = Path(folder)
        path = root / "disposable.sqlite3"
        settings = Settings(
            db_path=path,
            team_tokens={"chief": "m1n-chief", "mod": "m1n-mod"},
            admin_member_id="chief", browser_session_secure=False,
        )
        app = create_app(settings, QueueStore(path))
        env = dict(os.environ)
        for variable in (
            "CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY",
            "CHAPTERA_GEMINI_VAULT_KEY", "ERIDA_RUST_TEST_WRITES",
            "ERIDA_RUST_DISPOSABLE_ROOT",
        ):
            env.pop(variable, None)
        env.update({
            "CHAPTERA_DB_PATH": str(path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "CHAPTERA_BROWSER_SESSION_SECURE": "0",
            "ERIDA_RUST_ADDR": "127.0.0.1:18494",
        })
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18494", process)
            asyncio.run(exercise(app, path, "http://127.0.0.1:18494", settings))
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
