#!/usr/bin/env python3
"""Read-only Python/Rust parity: preserved import prefaces, team auth, no writes."""
from __future__ import annotations
import asyncio
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.import_sessions import ImportSessions
from chaptera_server.library import BookLibrary
from chaptera_server.storage import QueueStore
from compare_rust_api import launch, ready
from compare_rust_import_stages import runner_env

ROUTE = "/v1/imports/books"


def rows(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as con:
        return con.execute(
            "SELECT book_id,stage_id,source_html,source_sha256,created_at "
            "FROM book_import_unassigned ORDER BY created_at"
        ).fetchall()


async def verify(app, db: Path, settings: Settings) -> None:
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                          base_url="http://python") as py,
        httpx.AsyncClient(base_url="http://127.0.0.1:18495") as rust,
    ):
        before = rows(db)
        for client in (py, rust):
            assert (await client.get(ROUTE + "/book-one/unassigned")).status_code == 401
            assert (await client.get(ROUTE + "/missing/unassigned",
                headers={"X-API-Key": settings.team_tokens["alice"]})).status_code == 404
            assert (await client.post(ROUTE + "/book-one/unassigned",
                headers={"X-API-Key": settings.team_tokens["alice"]})).status_code in (404, 405)
        for member in ("alice", "bob"):
            headers = {"X-API-Key": settings.team_tokens[member]}
            route = ROUTE + "/book-one/unassigned"
            p, r = await asyncio.gather(py.get(route, headers=headers),
                                        rust.get(route, headers=headers))
            assert p.status_code == r.status_code == 200, (member, p.text, r.text)
            assert "no-store" in r.headers["cache-control"]
            assert p.json() == r.json(), (member, p.json(), r.json())
            assert [x["stage_id"] for x in r.json()["prefixes"]] == ["one", "two"]
            for item in r.json()["prefixes"]:
                assert item["source_sha256"] == hashlib.sha256(
                    item["source_html"].encode("utf-8")
                ).hexdigest()
            route = ROUTE + "/book-empty/unassigned"
            assert (await rust.get(route, headers=headers)).json() == (
                await py.get(route, headers=headers)
            ).json() == {"book_id": "book-empty", "prefixes": []}
        assert rows(db) == before, "Read endpoint modified preserved source"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_import_prefixes.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m2-prefix-") as tmp:
        root = Path(tmp)
        db = root / "disposable.sqlite3"
        settings = Settings(
            db_path=db, team_tokens={
                "chief": "chief-key", "alice": "alice-key", "bob": "bob-key"
            }, admin_member_id="chief", browser_session_secure=False,
        )
        store = QueueStore(db)
        app = create_app(settings, store)
        ImportSessions(store)
        lib = BookLibrary(store)
        for book in ("book-one", "book-empty"):
            lib.create_book(book, book, owner_member_id="alice", actor_id="alice")
        with sqlite3.connect(db) as con:
            for stage, source, stamp in (
                ("two", "<p>前言 &amp; вступление</p>", 2.0),
                ("one", "<p>Перед началом.</p>", 1.0),
            ):
                con.execute(
                    "INSERT INTO book_import_unassigned "
                    "(book_id,stage_id,filename,source_html,source_sha256,"
                    "actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    ("book-one", stage, "story.txt", source,
                     hashlib.sha256(source.encode()).hexdigest(), "alice", stamp),
                )
        env = runner_env(root, db, settings)
        env.pop("ERIDA_RUST_TEST_WRITES", None)
        env.pop("ERIDA_RUST_DISPOSABLE_ROOT", None)
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18495", process)
            asyncio.run(verify(app, db, settings))
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print("rust_unassigned_preface_python_http_parity=PASS")
    print("rust_unassigned_preface_requires_session=PASS")
    print("rust_unassigned_preface_without_write_gate=PASS")
    print("production_sqlite_access=NO")


if __name__ == "__main__":
    main()
