#!/usr/bin/env python3
"""RUST-M1b cross-runtime editor draft CAS and rebase on disposable SQLite.

Uses one synthetic SQLite with the real Python ASGI app and Rust loopback.
No production state, source HTTP, personal secrets, or paid Gemini calls.
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


async def exercise(app, settings: Settings, path: Path, rust_url: str) -> None:
    route = "/v1/books/rust-editor/chapters/first/draft"
    chapter = "/v1/books/rust-editor/chapters/first"
    headers = {"X-API-Key": settings.team_tokens["mod"]}
    admin = {"X-API-Key": settings.team_tokens["chief"]}
    payload = lambda text, draft, source=1, accepted=0, rebase=False: {
        "body_text": text,
        "expected_revision": draft,
        "expected_source_revision": source,
        "expected_accepted_revision": accepted,
        "rebase": rebase,
    }
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url="http://python",
        ) as python,
        httpx.AsyncClient(base_url=rust_url,timeout=15.0) as rust,
    ):
        created = await rust.post(
            "/v1/books",headers=headers,
            json={"book_id":"rust-editor","title":"Synthetic editor contract"},
        )
        assert created.status_code == 201, (created.status_code,created.text)
        stored = await python.put(
            chapter,headers=headers,
            json={"chapter_index":1,"chapter_path":"chapters/first.xhtml",
                  "source_html":"<p>Синтетическая исходная глава.</p>",
                  "expected_revision":0},
        )
        assert stored.status_code == 200 and stored.json()["source_revision"] == 1

        empty_python = await python.get(route,headers=headers)
        empty_rust = await rust.get(route,headers=headers)
        assert empty_python.status_code == empty_rust.status_code == 200
        assert empty_python.json() == empty_rust.json()
        assert empty_rust.json() == {
            "revision":0,"source_revision":1,"accepted_revision":0,
            "body_text":"","actor_id":None,"updated_at":None,
        }

        # Rust save -> Python read: raw chapter and acceptance never change.
        first = await rust.put(route,headers=headers,json=payload("Первый черновик",0))
        assert first.status_code == 200, (first.status_code,first.text)
        assert first.json()["revision"] == 1 and first.json()["actor_id"] == "mod"
        assert (await python.get(route,headers=headers)).json() == first.json()
        original_chapter = (await python.get(chapter,headers=headers)).json()
        assert original_chapter["accepted_revision"] == 0

        # Python save -> Rust read; different team actors share one CAS.
        second = await python.put(
            route,headers=admin,json=payload("Редактирование администратора",1),
        )
        assert second.status_code == 200, second.text
        assert second.json()["revision"] == 2 and second.json()["actor_id"] == "chief"
        assert (await rust.get(route,headers=headers)).json() == second.json()

        # Identical replay from another actor is a no-op, including old time.
        replay = await rust.put(
            route,headers=headers,json=payload("Редактирование администратора",2),
        )
        assert replay.status_code == 200 and replay.json() == second.json()
        stale = await rust.put(route,headers=headers,json=payload("Потеря",1))
        assert stale.status_code == 409
        assert stale.json()["detail"] == "Draft revision changed; load and compare before overwriting"

        # Real two-process contention on same SQLite: one revision/event wins.
        contenders = await asyncio.gather(
            python.put(route,headers=admin,json=payload("Версия Python",2)),
            rust.put(route,headers=headers,json=payload("Версия Rust",2)),
        )
        assert sorted(r.status_code for r in contenders) == [200,409], [
            (r.status_code,r.text) for r in contenders
        ]
        current = (await rust.get(route,headers=headers)).json()
        assert current == (await python.get(route,headers=headers)).json()
        assert current["revision"] == 3
        assert current["body_text"] in ("Версия Python","Версия Rust")
        with sqlite3.connect(path) as db:
            rows = db.execute(
                "SELECT revision,actor_id,body_text FROM editor_draft_revisions "
                "WHERE book_id='rust-editor' AND chapter_id='first' ORDER BY revision"
            ).fetchall()
            assert [row[0] for row in rows] == [1,2,3]

        # Accepted translation changes independently: stale editor drafts
        # require a refresh, and rebase must be an explicit opt-in.
        accepted = await python.put(
            chapter + "/accepted-translation",headers=headers,
            json={"translated_html":"<p>Принятая редакция.</p>",
                  "expected_revision":0,"expected_source_revision":1},
        )
        assert accepted.status_code == 200, accepted.text
        old = await rust.put(
            route,headers=headers,
            json=payload("Без обновления",3,accepted=0),
        )
        assert old.status_code == 409
        assert old.json()["detail"] == "Source or accepted translation changed; refresh required"

        no_rebase = await rust.put(
            route,headers=headers,
            json=payload("Без ребейза",3,accepted=1),
        )
        assert no_rebase.status_code == 409
        assert no_rebase.json()["detail"] == "Draft was based on older revisions; explicit rebase required"

        rebased = await rust.put(
            route,headers=admin,
            json=payload(current["body_text"],3,accepted=1,rebase=True),
        )
        assert rebased.status_code == 200,rebased.text
        assert rebased.json()["revision"] == 4
        assert rebased.json()["accepted_revision"] == 1
        assert rebased.json()["actor_id"] == "chief"
        assert (await python.get(route,headers=headers)).json() == rebased.json()
        accepted_after = (await python.get(chapter,headers=headers)).json()
        assert accepted_after["accepted_html"] == "<p>Принятая редакция.</p>"
        assert accepted_after["source_html"] == original_chapter["source_html"]

        changed_source = await python.put(
            chapter,headers=headers,
            json={"chapter_index":1,"chapter_path":"chapters/first.xhtml",
                  "source_html":"<p>Обновлённая исходная глава.</p>",
                  "expected_revision":1},
        )
        assert changed_source.status_code == 200,changed_source.text
        assert changed_source.json()["source_revision"] == 2
        blocked = await rust.put(
            route,headers=headers,
            json=payload("Нельзя поверх новой главы",4,source=1,accepted=1),
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "Source or accepted translation changed; refresh required"
        assert (await rust.get(route,headers=headers)).json() == rebased.json()

        # Member key or browser-session+CSRF, with bad-header precedence.
        assert (await rust.get(route)).status_code == 401
        assert (await rust.put(route,json=payload("Нет",4,2,1))).status_code == 401
        cookie = app.state.team_sessions.issue(
            member_id="mod",team_key=settings.team_tokens["mod"],ttl_seconds=1800,
        )
        browser = {"Cookie":"readera_session="+cookie}
        assert (await rust.get(route,headers=browser)).status_code == 200
        assert (await rust.put(
            route,headers=browser,json=payload("Без CSRF",4,2,1),
        )).status_code == 403
        proof = {**browser,"X-ReadEra-CSRF":TeamSessions.csrf_token(cookie)}
        success = await rust.put(
            route,headers=proof,json=payload("С новой исходной главой",4,2,1,rebase=True),
        )
        assert success.status_code == 200, success.text
        assert success.json()["revision"] == 5
        assert success.json()["actor_id"] == "mod"
        assert (await rust.get(route,headers={
            **proof,"X-API-Key":"bad-header",
        })).status_code == 401
        assert (await rust.put(route,headers={
            **proof,"X-API-Key":"bad-header",
        },json=payload("Нет",5,2,1))).status_code == 401

        for client in (python,rust):
            missing = await client.get("/v1/books/rust-editor/chapters/unknown/draft",headers=headers)
            assert missing.status_code == 404 and missing.json()["detail"] == "Chapter not found"
            missing_write = await client.put(
                "/v1/books/rust-editor/chapters/unknown/draft",headers=headers,
                json=payload("Нет главы",0),
            )
            assert missing_write.status_code == 404
            assert missing_write.json()["detail"] == "Chapter not found"

        # Restart-like fresh Python store against the same durable image.
        from chaptera_server.editorial import EditorialStore
        restarted = EditorialStore(QueueStore(path))
        assert restarted.get_draft("rust-editor","first") == success.json()
        with sqlite3.connect(path) as db:
            rows = db.execute(
                "SELECT revision,actor_id FROM editor_draft_revisions "
                "WHERE book_id='rust-editor' AND chapter_id='first' ORDER BY revision"
            ).fetchall()
            assert rows == [(1,"mod"),(2,"chief"),
                            (3,current["actor_id"]),(4,"chief"),(5,"mod")]
        print("rust_editor_draft_empty_read=PASS")
        print("rust_editor_draft_cross_runtime_cas=PASS")
        print("rust_editor_draft_rebase_and_accepted_isolation=PASS")
        print("rust_editor_draft_cookie_csrf_and_auth=PASS")
        print("rust_editor_draft_restart_and_audit=PASS")
        print("production_sqlite_access=no")
        print("gemini_calls=0")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_editor_drafts.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-rust-m1b-editor-") as directory:
        root = Path(directory)
        db_path = root / "disposable.sqlite3"
        settings = Settings(
            db_path=db_path,
            team_tokens={"chief":"rust-editor-chief","mod":"rust-editor-mod"},
            admin_member_id="chief",browser_session_secure=False,
        )
        app = create_app(settings,QueueStore(db_path))
        env = dict(os.environ)
        for key in ("CREDENTIALS_DIRECTORY","CHAPTERA_GEMINI_API_KEY","CHAPTERA_GEMINI_VAULT_KEY"):
            env.pop(key,None)
        env.update({
            "CHAPTERA_DB_PATH":str(db_path),
            "CHAPTERA_TEAM_TOKENS_JSON":json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID":"chief",
            "ERIDA_RUST_ADDR":"127.0.0.1:18483",
            "ERIDA_RUST_TEST_WRITES":"1",
            "ERIDA_RUST_DISPOSABLE_ROOT":str(root),
        })
        runner = launch([binary],env)
        try:
            ready("http://127.0.0.1:18483",runner)
            asyncio.run(exercise(app,settings,db_path,"http://127.0.0.1:18483"))
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
