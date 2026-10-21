#!/usr/bin/env python3
"""RUST-M1: glossary CAS/replay/audit parity on shared disposable SQLite.

No live source, provider call, chapter content or production database.
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
from chaptera_server.team_sessions import TeamSessions

BOOK = "glossary-русский"
PATH = f"/v1/books/{BOOK}/glossary"


async def exercise(app, settings: Settings, sqlite_path: Path, endpoint: str) -> None:
    app.state.library.create_book(BOOK, "Dictionary fixture", owner_member_id="mod", actor_id="mod")
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://python") as py,
        httpx.AsyncClient(base_url=endpoint, timeout=12) as rust,
    ):
        mod = {"X-API-Key": settings.team_tokens["mod"]}
        admin = {"X-API-Key": settings.team_tokens["chief"]}
        original = (await py.get(f"/v1/books/{BOOK}", headers=mod)).json()
        assert original["glossary_revision"] == 0
        assert original == (await rust.get(f"/v1/books/{BOOK}", headers=mod)).json()

        # Python's sorted, spaced UTF-8 JSON encoding matters to no-op/replay
        # semantics: Rust must not use serde_json compact separators in SQLite.
        first = {
            "glossary": {"龙": "Дракон", "alpha": {"rus": "Солнце", "note": "начало"}},
            "glossary_versions": {},
            "expected_revision": 0,
        }
        r = await rust.put(PATH, headers=mod, json=first)
        assert r.status_code == 200, r.text
        assert r.json()["glossary_revision"] == 1
        p = await py.put(PATH, headers=mod, json={**first, "expected_revision": 1})
        assert p.status_code == 200 and p.json() == r.json(), (r.text,p.text)
        with sqlite3.connect(sqlite_path) as con:
            packed, revs = con.execute(
                "SELECT glossary_json,glossary_versions_json FROM books WHERE id=?",
                (BOOK,),
            ).fetchone()
            assert packed == json.dumps(first["glossary"], ensure_ascii=False, sort_keys=True)
            assert revs == "{}"
            assert con.execute(
                "SELECT COUNT(*) FROM book_glossary_revisions WHERE book_id=?", (BOOK,),
            ).fetchone()[0] == 1

        app.state.library.put_chapter(
            BOOK, "chapter-1", chapter_index=1, chapter_path="parts/1.xhtml",
            source_html="<p>故事开始，龙走进了城门。</p>", expected_revision=0,
        )
        versions = {"龙": [
            {"scope": ["parts/1.xhtml"], "override": {"rus": "Красный дракон", "note": "эпитет"}}
        ]}
        second = {
            "glossary": first["glossary"], "glossary_versions": versions,
            "glossary_authority": "READY_WITH_WARNINGS", "expected_revision": 1,
        }
        p2 = await py.put(PATH, headers=mod, json=second)
        assert p2.status_code == 200, p2.text
        r2 = await rust.put(PATH, headers=mod, json={**second, "expected_revision": 2})
        assert r2.status_code == 200 and r2.json() == p2.json(), (p2.text,r2.text)
        assert r2.json()["glossary_revision"] == 2

        # Revision mismatch is a 409 on both runtimes; no event.
        for client in (py, rust):
            stale = await client.put(PATH, headers=mod, json={**second, "expected_revision": 0})
            assert stale.status_code == 409 and stale.json()["detail"] == "Glossary revision mismatch"

        # The existing chapter makes conflicting scope overrides illegal.
        invalid = {"glossary": first["glossary"], "glossary_versions": {"龙": [
            {"scope": ["parts/1.xhtml"], "override": {"rus": "Дракон"}},
            {"scope": ["parts\\1.xhtml"], "override": {"rus": "Змей"}},
        ]}, "glossary_authority": "READY", "expected_revision": 2}
        py_bad = await py.put(PATH, headers=mod, json=invalid)
        rust_bad = await rust.put(PATH, headers=mod, json=invalid)
        assert py_bad.status_code == rust_bad.status_code == 422
        assert py_bad.json() == rust_bad.json(), (py_bad.text, rust_bad.text)
        with sqlite3.connect(sqlite_path) as con:
            assert con.execute(
                "SELECT glossary_revision FROM books WHERE id=?", (BOOK,)
            ).fetchone()[0] == 2
            assert con.execute(
                "SELECT COUNT(*) FROM book_glossary_revisions WHERE book_id=?", (BOOK,)
            ).fetchone()[0] == 2

        # 'DISABLED' skips scoped conflicts, but still stores source evidence.
        disabled = {**invalid, "glossary_authority": "DISABLED"}
        rx = await rust.put(PATH, headers=admin, json=disabled)
        assert rx.status_code == 200 and rx.json()["glossary_revision"] == 3, rx.text
        replay = await py.put(PATH, headers=mod, json={**disabled, "expected_revision": 3})
        assert replay.status_code == 200 and replay.json() == rx.json(), replay.text

        # Simultaneous independent Python/Rust writers compete for revision 3.
        left = {**first, "expected_revision": 3}
        right = {
            "glossary": {"龙": {"rus": "Лун", "authority": "required"}},
            "glossary_versions": {},
            "glossary_authority": "READY",
            "expected_revision": 3,
        }
        winners = await asyncio.gather(
            py.put(PATH, headers=mod, json=left),
            rust.put(PATH, headers=admin, json=right),
        )
        assert sorted(x.status_code for x in winners) == [200, 409], [
            (x.status_code, x.text) for x in winners
        ]
        live = (await py.get(f"/v1/books/{BOOK}", headers=mod)).json()
        assert live == (await rust.get(f"/v1/books/{BOOK}", headers=mod)).json()
        assert live["glossary_revision"] == 4
        with sqlite3.connect(sqlite_path) as con:
            events = con.execute(
                "SELECT revision,actor_id FROM book_glossary_revisions WHERE book_id=? ORDER BY revision",
                (BOOK,),
            ).fetchall()
        assert events[:3] == [(1,"mod"),(2,"mod"),(3,"chief")]
        assert len(events) == 4 and events[3][1] in {"mod","chief"}

        # A body claiming to be valid never bypasses auth or cookie-CSRF.
        assert (await rust.put(PATH, json={**first, "expected_revision": 4})).status_code == 401
        cookie = app.state.team_sessions.issue(
            member_id="mod",team_key=settings.team_tokens["mod"],ttl_seconds=1800,
        )
        browser = {"Cookie": "readera_session=" + cookie}
        assert (await rust.put(PATH, headers=browser, json={
            "glossary": live["glossary"], "glossary_versions": live["glossary_versions"],
            "glossary_authority": live["glossary_authority"], "expected_revision": 4,
        })).status_code == 403
        valid = {**browser,"X-ReadEra-CSRF":TeamSessions.csrf_token(cookie)}
        identical = {
            "glossary": live["glossary"], "glossary_versions": live["glossary_versions"],
            "glossary_authority": live["glossary_authority"], "expected_revision": 4,
        }
        assert (await rust.put(PATH, headers=valid, json=identical)).status_code == 200
        assert (await rust.put(PATH, headers={
            **valid,"X-API-Key":"invalid",
        },json=identical)).status_code == 401
        assert (await rust.put("/v1/books/missing/glossary",headers=mod,json=first)).status_code == 404

        with sqlite3.connect(sqlite_path) as con:
            assert con.execute(
                "SELECT COUNT(*) FROM book_glossary_revisions WHERE book_id=?", (BOOK,)
            ).fetchone()[0] == 4

        print("rust_glossary_python_sqlite_byte_parity=PASS")
        print("rust_glossary_revision_audit_and_cas=PASS")
        print("rust_glossary_scope_conflicts_fail_closed=PASS")
        print("rust_glossary_credential_and_csrf=PASS")
        print("production_sqlite_access=no")
        print("gemini_calls=0")


def main() -> None:
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-rust-glossary-") as directory:
        root=Path(directory)
        path=root/"disposable.sqlite3"
        settings=Settings(
            db_path=path,
            team_tokens={"chief":"rust-glossary-chief","mod":"rust-glossary-mod"},
            admin_member_id="chief",browser_session_secure=False,
        )
        app=create_app(settings,QueueStore(path))
        env=dict(os.environ)
        for key in ("CREDENTIALS_DIRECTORY","CHAPTERA_GEMINI_API_KEY","CHAPTERA_GEMINI_VAULT_KEY"):
            env.pop(key,None)
        env.update({
            "CHAPTERA_DB_PATH":str(path),
            "CHAPTERA_TEAM_TOKENS_JSON":json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID":"chief",
            "ERIDA_RUST_ADDR":"127.0.0.1:18479",
            "ERIDA_RUST_TEST_WRITES":"1",
            "ERIDA_RUST_DISPOSABLE_ROOT":str(root),
        })
        runner=launch([binary],env)
        try:
            ready("http://127.0.0.1:18479",runner)
            asyncio.run(exercise(app,settings,path,"http://127.0.0.1:18479"))
            assert runner.poll() is None
        finally:
            if runner.poll() is None:
                runner.terminate()
                try:
                    runner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    runner.kill()
                    runner.wait(timeout=5)


if __name__=="__main__":
    main()
