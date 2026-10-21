#!/usr/bin/env python3
"""M1c: real Python ASGI/Rust loopback accepted history and restore parity."""
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


async def exercise(app, settings, path, rust_url):
    chapter = "/v1/books/m1c/chapters/one"
    history = chapter + "/accepted-revisions"
    restore = chapter + "/restore-accepted"
    mod = {"X-API-Key": settings.team_tokens["mod"]}
    chief = {"X-API-Key": settings.team_tokens["chief"]}

    def body(revision, current, source=1):
        return {"revision": revision, "expected_current_revision": current,
                "expected_source_revision": source}

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://python") as py,
        httpx.AsyncClient(base_url=rust_url, timeout=15.0) as rs,
    ):
        created = await py.post("/v1/books", headers=mod,
                                json={"book_id": "m1c", "title": "Revision parity"})
        assert created.status_code == 201, created.text
        source = await py.put(chapter, headers=mod,
                              json={"chapter_index": 1, "chapter_path": "1.xhtml",
                                    "source_html": "<p>Source one.</p>", "expected_revision": 0})
        assert source.status_code == 200, source.text
        original = source.json()["source_html"]
        assert (await py.get(history, headers=mod)).json() == (await rs.get(history, headers=mod)).json()
        assert (await rs.get(history, headers=mod)).json() == {"revisions": []}
        for revision, html, actor in (
            (0, "<p>First version.</p>", mod),
            (1, "<p>Second version.</p>", chief),
        ):
            result = await py.put(chapter + "/accepted-translation", headers=actor,
                                  json={"translated_html": html, "expected_revision": revision,
                                        "expected_source_revision": 1})
            assert result.status_code == 200, result.text

        py_history = await py.get(history, headers=mod)
        rs_history = await rs.get(history, headers=mod)
        assert py_history.status_code == rs_history.status_code == 200
        assert py_history.json() == rs_history.json()
        assert [r["revision"] for r in rs_history.json()["revisions"]] == [2, 1]
        assert all("translated_html" not in row for row in rs_history.json()["revisions"])
        for revision in (1, 2):
            py_version = await py.get(history + f"/{revision}", headers=mod)
            rs_version = await rs.get(history + f"/{revision}", headers=mod)
            assert py_version.status_code == rs_version.status_code == 200
            assert py_version.json() == rs_version.json()

        for client in (py, rs):
            missing = await client.get(history + "/99", headers=mod)
            assert missing.status_code == 404 and missing.json()["detail"] == "Accepted revision not found"
            missing_chapter = await client.get(
                "/v1/books/m1c/chapters/missing/accepted-revisions", headers=mod)
            assert missing_chapter.status_code == 404 and missing_chapter.json()["detail"] == "Chapter not found"
        assert (await rs.get(history)).status_code == 401
        assert (await rs.post(restore, json=body(1, 2))).status_code == 401
        assert (await rs.post(restore, headers=mod, json=body(1, 1))).status_code == 409
        assert (await rs.post(restore, headers=mod, json=body(2, 2))).status_code == 409
        assert (await rs.post(restore, headers=mod, json=body(99, 2))).status_code == 404

        # Rust restore from Python history; Python read sees exact same state.
        restored = await rs.post(restore, headers=chief, json=body(1, 2))
        assert restored.status_code == 200, restored.text
        assert restored.json()["accepted_revision"] == 3
        assert restored.json()["accepted_html"] == "<p>First version.</p>"
        assert restored.json()["source_html"] == original
        assert restored.json() == (await py.get(chapter, headers=mod)).json()
        assert (await py.get(history, headers=mod)).json() == (await rs.get(history, headers=mod)).json()
        assert (await py.get(history + "/3", headers=mod)).json() == (await rs.get(history + "/3", headers=mod)).json()

        # Python restore of another historical revision; Rust read sees it.
        restored_py = await py.post(restore, headers=mod, json=body(2, 3))
        assert restored_py.status_code == 200, restored_py.text
        assert restored_py.json()["accepted_revision"] == 4
        assert restored_py.json() == (await rs.get(chapter, headers=mod)).json()
        assert (await rs.get(history + "/4", headers=mod)).json()["actor_id"] == "mod"

        # Two-process compare-and-swap: exactly one new accepted revision.
        contenders = await asyncio.gather(
            rs.post(restore, headers=chief, json=body(1, 4)),
            py.post(restore, headers=mod, json=body(3, 4)),
        )
        assert sorted(r.status_code for r in contenders) == [200, 409], [
            (r.status_code, r.text) for r in contenders]
        current = (await rs.get(chapter, headers=mod)).json()
        assert current["accepted_revision"] == 5
        assert current == (await py.get(chapter, headers=mod)).json()

        # Changing source must fence off all historical versions of prior source.
        changed = await py.put(chapter, headers=mod,
                               json={"chapter_index": 1, "chapter_path": "1.xhtml",
                                     "source_html": "<p>Source two.</p>", "expected_revision": 1})
        assert changed.status_code == 200 and changed.json()["source_revision"] == 2
        stale = await rs.post(restore, headers=mod, json=body(1, 5, 2))
        assert stale.status_code == 409
        assert stale.json()["detail"] == "Historical translation belongs to a different source revision"
        stale_base = await rs.post(restore, headers=mod, json=body(1, 5, 1))
        assert stale_base.status_code == 409
        assert stale_base.json()["detail"] == "Chapter revisions changed before restore"

        cookie = app.state.team_sessions.issue(
            member_id="mod", team_key=settings.team_tokens["mod"], ttl_seconds=1800)
        browser = {"Cookie": "readera_session=" + cookie}
        assert (await rs.get(history, headers=browser)).status_code == 200
        assert (await rs.post(restore, headers=browser, json=body(1, 5, 2))).status_code == 403
        proof = {**browser, "X-ReadEra-CSRF": TeamSessions.csrf_token(cookie)}
        assert (await rs.post(restore, headers=proof, json=body(1, 5, 2))).status_code == 409
        assert (await rs.get(history, headers={**proof, "X-API-Key": "bad"})).status_code == 401

        with sqlite3.connect(path) as db:
            rows = db.execute(
                "SELECT revision,source_revision,translated_html,actor_id "
                "FROM book_accepted_revisions WHERE book_id='m1c' "
                "AND chapter_id='one' ORDER BY revision").fetchall()
            assert [row[0] for row in rows] == [1, 2, 3, 4, 5]
            assert rows[0] == (1, 1, "<p>First version.</p>", "mod")
            assert rows[1] == (2, 1, "<p>Second version.</p>", "chief")
            assert rows[2] == (3, 1, "<p>First version.</p>", "chief")
            assert rows[3] == (4, 1, "<p>Second version.</p>", "mod")
        from chaptera_server.editorial import EditorialStore
        reopened = EditorialStore(QueueStore(path))
        assert reopened.accepted_history("m1c", "one") == (await rs.get(history, headers=mod)).json()["revisions"]
        assert reopened.accepted_version("m1c", "one", 1)["translated_html"] == "<p>First version.</p>"
        print("rust_accepted_history_and_details_python_parity=PASS")
        print("rust_accepted_restore_cross_runtime_and_cas=PASS")
        print("rust_accepted_restore_stale_source_rejected=PASS")
        print("rust_accepted_restore_auth_csrf_and_audit=PASS")
        print("production_sqlite_access=no")
        print("gemini_calls=0")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_accepted_history.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m1c-accepted-") as folder:
        root = Path(folder)
        path = root / "disposable.sqlite3"
        settings = Settings(db_path=path, team_tokens={"mod": "m1c-mod", "chief": "m1c-chief"},
                            admin_member_id="chief", browser_session_secure=False)
        app = create_app(settings, QueueStore(path))
        env = dict(os.environ)
        for key in ("CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY", "CHAPTERA_GEMINI_VAULT_KEY"):
            env.pop(key, None)
        env.update({
            "CHAPTERA_DB_PATH": str(path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "ERIDA_RUST_ADDR": "127.0.0.1:18484",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
        })
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18484", process)
            asyncio.run(exercise(app, settings, path, "http://127.0.0.1:18484"))
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
