#!/usr/bin/env python3
"""RUST-02e translation acceptance parity, disposable Python/Rust SQLite only."""
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


async def exercise(app, db_path: Path, endpoint: str, settings: Settings) -> None:
    member = {"X-API-Key": settings.team_tokens["mod"]}
    route = "/v1/books/acceptance/chapters/one/accepted-translation"
    second_route = "/v1/books/acceptance/chapters/two/accepted-translation"
    payload = lambda html, rev, source: {
        "translated_html": html,
        "expected_revision": rev,
        "expected_source_revision": source,
    }
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://python") as python,
        httpx.AsyncClient(base_url=endpoint, timeout=12) as rust,
    ):
        book = await python.post(
            "/v1/books", headers=member,
            json={"book_id": "acceptance", "title": "Translation acceptance fixture"},
        )
        assert book.status_code == 201
        for index, name in enumerate(("one", "two"), 1):
            original = await python.put(
                f"/v1/books/acceptance/chapters/{name}",
                headers=member,
                json={"chapter_index": index, "chapter_path": f"{index}.xhtml",
                      "source_html": f"<p>Source chapter {index}.</p>",
                      "expected_revision": 0},
            )
            assert original.status_code == 200

        # Rust creates first acceptance; Python idempotently replays it.
        first = payload("<p>Перевод первой главы.</p>", 0, 1)
        inserted = await rust.put(route, headers=member, json=first)
        assert inserted.status_code == 200, (inserted.status_code, inserted.text)
        assert inserted.json()["accepted_current"] is True
        assert inserted.json()["accepted_revision"] == 1
        replay = await python.put(route, headers=member, json=payload(first["translated_html"], 1, 1))
        assert replay.status_code == 200 and replay.json() == inserted.json()

        # Python-created acceptance replays in Rust, without extra audit rows.
        two = payload("<p>Вторая глава.</p>", 0, 1)
        py_two = await python.put(second_route, headers=member, json=two)
        rust_two = await rust.put(
            second_route, headers=member,
            json=payload(two["translated_html"], 1, 1),
        )
        assert py_two.status_code == rust_two.status_code == 200
        assert py_two.json() == rust_two.json()

        # Two independent revisions contend for accepted_revision=1.
        contenders = await asyncio.gather(
            python.put(route, headers=member,
                       json=payload("<p>Редакция A.</p>", 1, 1)),
            rust.put(route, headers=member,
                     json=payload("<p>Редакция B.</p>", 1, 1)),
        )
        assert sorted(r.status_code for r in contenders) == [200, 409], [
            (r.status_code, r.text) for r in contenders
        ]
        original = await python.get(
            "/v1/books/acceptance/chapters/one", headers=member,
        )
        rust_current = await rust.get(
            "/v1/books/acceptance/chapters/one", headers=member,
        )
        assert original.json() == rust_current.json()
        assert rust_current.json()["accepted_revision"] == 2
        assert rust_current.json()["accepted_html"] in (
            "<p>Редакция A.</p>", "<p>Редакция B.</p>",
        )
        with sqlite3.connect(db_path) as con:
            versions = con.execute(
                "SELECT revision,source_revision,translated_html,actor_id "
                "FROM book_accepted_revisions WHERE book_id='acceptance' AND chapter_id='one' "
                "ORDER BY revision"
            ).fetchall()
        assert len(versions) == 2, versions
        assert versions[0] == (1, 1, "<p>Перевод первой главы.</p>", "mod")
        assert versions[1][0:2] == (2, 1)
        assert versions[1][3] == "mod"

        # Changing source invalidates the old acceptance; Rust must refuse a
        # stale expected_source_revision and allow the current one.
        source_mutation = await python.put(
            "/v1/books/acceptance/chapters/one", headers=member,
            json={"chapter_index": 1, "chapter_path": "1.xhtml",
                  "source_html": "<p>Changed source.</p>", "expected_revision": 1},
        )
        assert source_mutation.status_code == 200
        assert source_mutation.json()["source_revision"] == 2
        assert source_mutation.json()["accepted_current"] is False
        stale = await rust.put(route, headers=member,
                               json=payload("<p>Нельзя принять.</p>", 2, 1))
        assert stale.status_code == 409
        assert stale.json()["detail"] == "Source revision changed before translation acceptance"
        accepted = await rust.put(route, headers=member,
                                  json=payload("<p>Переведено заново.</p>", 2, 2))
        assert accepted.status_code == 200 and accepted.json()["accepted_current"] is True
        assert accepted.json()["accepted_revision"] == 3
        assert accepted.json()["accepted_source_revision"] == 2
        assert (await python.get(
            "/v1/books/acceptance/chapters/one", headers=member
        )).json() == accepted.json()

        # M1l proves editor &amp; and punctuation as visible text. Keep negative
        # coverage for UNKNOWN entities and actual executable attributes.
        for index, html in enumerate((
            "<p> </p>", "<p>&unknown;</p>", "<p>Принят <b>перевод</b>.</p>",
            "<div>Перевод</div>", "Текст без разметки", "<p onclick='x'>Текст.</p>",
        )):
            refused = await rust.put(
                route, headers=member, json=payload(html, 3, 2),
            )
            assert refused.status_code == 422, (index, refused.status_code, refused.text)
            assert "visible text proof required" in refused.json()["detail"]
        missing = await rust.put(
            "/v1/books/acceptance/chapters/missing/accepted-translation",
            headers=member, json=payload("<p>Готово.</p>", 0, 1),
        )
        assert missing.status_code == 404
        assert missing.json()["detail"] == "Chapter not found"

        # A cookie user has the same mandatory CSRF proof.
        cookie = app.state.team_sessions.issue(
            member_id="mod", team_key=settings.team_tokens["mod"], ttl_seconds=3600
        )
        browser = {"Cookie": "readera_session=" + cookie}
        no_csrf = await rust.put(route, headers=browser,
                                 json=payload("<p>Браузерная редакция.</p>", 3, 2))
        assert no_csrf.status_code == 403
        browser_valid = {
            **browser, "X-ReadEra-CSRF": TeamSessions.csrf_token(cookie),
        }
        with_csrf = await rust.put(
            route, headers=browser_valid,
            json=payload("<p>Браузерная редакция.</p>", 3, 2),
        )
        assert with_csrf.status_code == 200
        assert with_csrf.json()["accepted_revision"] == 4
        bad_key = await rust.put(
            route, headers={**browser_valid, "X-API-Key": "wrong"},
            json=payload("<p>Нельзя.</p>", 4, 2),
        )
        assert bad_key.status_code == 401

        with sqlite3.connect(db_path) as con:
            trail = con.execute(
                "SELECT revision,source_revision FROM book_accepted_revisions "
                "WHERE book_id='acceptance' AND chapter_id='one' ORDER BY revision"
            ).fetchall()
            assert trail == [(1, 1), (2, 1), (3, 2), (4, 2)]
        print("rust_acceptance_cross_runtime_replay=PASS")
        print("rust_acceptance_concurrent_revision_fencing=PASS")
        print("rust_acceptance_immutable_history=PASS")
        print("rust_acceptance_rejects_stale_source=PASS")
        print("rust_acceptance_unsupported_html_fail_closed=PASS")
        print("rust_acceptance_csrf_and_auth=PASS")
        print("rust_acceptance_production_sqlite_access=no")
        print("gemini_calls=0")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_acceptance.py /path/to/erida-api")
    executable = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-rust02e-accepted-") as folder:
        root = Path(folder)
        db_path = root / "isolated.sqlite3"
        settings = Settings(
            db_path=db_path,
            team_tokens={"chief": "rust02e-chief", "mod": "rust02e-moderator"},
            admin_member_id="chief",
            browser_session_secure=False,
        )
        app = create_app(settings, QueueStore(db_path))
        env = dict(os.environ)
        for key in ("CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY",
                    "CHAPTERA_GEMINI_VAULT_KEY"):
            env.pop(key, None)
        env.update({
            "CHAPTERA_DB_PATH": str(db_path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "ERIDA_RUST_ADDR": "127.0.0.1:18476",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
        })
        process = launch([executable], env)
        try:
            ready("http://127.0.0.1:18476", process)
            asyncio.run(exercise(app, db_path, "http://127.0.0.1:18476", settings))
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
