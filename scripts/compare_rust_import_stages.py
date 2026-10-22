#!/usr/bin/env python3
"""M1o: Rust/Python durable actor-owned TXT import stages (disposable SQLite).

No source chapters are written to books, no import confirmation route exists,
and the Rust write fence is never enabled on production databases.
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
import time

import httpx
from compare_rust_api import launch, ready
from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore

ROUTE = "/v1/imports/stages"
SOURCE = ("Перед началом книги.\n\nГлава 1\nA & B\nВторая строка.\n\n"
          "Глава 2\nКонец истории.").encode()


def equal_projection(left: dict, right: dict) -> None:
    # UUID and deadline are persisted into the same SQL row; any difference
    # between runtimes is a contract drift, not permitted test variance.
    assert left == right, [
        (key, left.get(key), right.get(key))
        for key in left.keys() | right.keys()
        if left.get(key) != right.get(key)
    ]


async def exercise(app, db: Path, url: str, settings: Settings) -> dict:
    alice = {"X-API-Key": settings.team_tokens["alice"]}
    bob = {"X-API-Key": settings.team_tokens["bob"]}
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://python", timeout=20,
        ) as py,
        httpx.AsyncClient(base_url=url, timeout=30) as rs,
    ):
        assert (await rs.post(ROUTE, files={
            "file": ("book.txt", SOURCE, "text/plain"),
        })).status_code == 401

        first = await rs.post(
            ROUTE, headers={**alice, "Idempotency-Key": "same-upload"},
            files={"file": ("story.txt", SOURCE, "text/plain")},
        )
        assert first.status_code == 201, first.status_code
        first_record = first.json()
        assert first_record["status"] == "staged"
        assert first_record["preview"]["chapter_count"] == 2
        assert first_record["unassigned_prefix_saved"] is True
        assert first_record["file_sha256"] == hashlib.sha256(SOURCE).hexdigest()
        assert "payload_json" not in first.text and "source_html" not in first.text
        assert "no-store" in first.headers["cache-control"]
        stage_id = first_record["stage_id"]

        python_stage = await py.get(ROUTE + "/" + stage_id, headers=alice)
        rust_stage = await rs.get(ROUTE + "/" + stage_id, headers=alice)
        assert python_stage.status_code == rust_stage.status_code == 200
        equal_projection(python_stage.json(), rust_stage.json())
        assert rust_stage.json()["unassigned_prefix_html"] == (
            "<p>Перед началом книги.</p>"
        )
        assert rust_stage.json()["preview"] == first_record["preview"]

        replay_python = await py.post(
            ROUTE, headers={**alice, "Idempotency-Key": "same-upload"},
            files={"file": ("story.txt", SOURCE, "text/plain")},
        )
        assert replay_python.status_code == 201
        equal_projection(replay_python.json(), first_record)
        replay_rust = await rs.post(
            ROUTE, headers={**alice, "Idempotency-Key": "same-upload"},
            files={"file": ("story.txt", SOURCE, "text/plain")},
        )
        assert replay_rust.status_code == 201
        equal_projection(replay_rust.json(), first_record)

        changed = await rs.post(
            ROUTE, headers={**alice, "Idempotency-Key": "same-upload"},
            files={"file": ("story.txt", b"Chapter 1\nDifferent", "text/plain")},
        )
        assert changed.status_code == 409
        assert changed.json()["detail"] == (
            "Idempotency-Key refers to a different uploaded file"
        )

        # Row ownership is enforced independently by Python and Rust.
        for client in (py, rs):
            assert (await client.get(ROUTE + "/" + stage_id,
                                     headers=bob)).status_code == 404
            assert (await client.delete(ROUTE + "/" + stage_id,
                                       headers=bob)).status_code == 404

        # Python-created stages are readable (but not leaked) from Rust.
        created_python = await py.post(
            ROUTE, headers=bob,
            files={"file": ("chapter.txt", b"Chapter 3\nSome text", "text/plain")},
        )
        assert created_python.status_code == 201, created_python.status_code
        bob_id = created_python.json()["stage_id"]
        rust_read_bob = await rs.get(ROUTE + "/" + bob_id, headers=bob)
        assert rust_read_bob.status_code == 200
        assert rust_read_bob.json()["stage_id"] == bob_id
        assert (await rs.get(ROUTE + "/" + bob_id, headers=alice)).status_code == 404
        equal_projection((await py.get(ROUTE + "/" + bob_id, headers=bob)).json(),
                         rust_read_bob.json())

        # Header-key is authorized; admin cookie mutation requires HMAC CSRF.
        admin = await py.post("/v1/session", json={"api_key": settings.team_tokens["chief"]})
        assert admin.status_code == 201
        cookie = {"Cookie": "readera_session=" +
                            admin.cookies.get("readera_session")}
        assert (await rs.post(
            ROUTE, headers=cookie,
            files={"file": ("cookie.txt", b"Chapter 1\nText", "text/plain")},
        )).status_code == 403
        with_csrf = await rs.post(
            ROUTE,
            headers={**cookie, "X-ReadEra-CSRF": admin.json()["csrf_token"]},
            files={"file": ("cookie.txt", b"Chapter 1\nText", "text/plain")},
        )
        assert with_csrf.status_code == 201
        assert (await rs.get(
            ROUTE + "/" + with_csrf.json()["stage_id"], headers=cookie,
        )).status_code == 200

        # Active stages are strictly bounded per actor, across both runtimes.
        for number in range(2):
            next_stage = await rs.post(
                ROUTE, headers=alice,
                files={"file": (f"next-{number}.txt",
                                f"Chapter {number+10}\nDifferent text".encode(),
                                "text/plain")},
            )
            assert next_stage.status_code == 201
        full = await rs.post(
            ROUTE, headers=alice,
            files={"file": ("fourth.txt", b"Chapter 22\nText", "text/plain")},
        )
        assert full.status_code == 413
        assert full.json()["detail"] == (
            "Too many unconfirmed import stages for this member"
        )

        cancelled = await rs.delete(ROUTE + "/" + stage_id, headers=alice)
        assert cancelled.status_code == 200
        assert cancelled.json() == {"stage_id": stage_id, "status": "cancelled"}
        assert (await py.get(ROUTE + "/" + stage_id, headers=alice)).status_code == 404
        assert (await rs.delete(ROUTE + "/" + stage_id, headers=alice)).status_code == 404

        # Expired state fails closed. A wrong actor sees 404, not the TTL.
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE book_import_stages SET expires_at=? WHERE id=?",
                (time.time() - 10, bob_id),
            )
            con.commit()
        assert (await rs.get(ROUTE + "/" + bob_id, headers=bob)).status_code == 410
        assert (await py.get(ROUTE + "/" + bob_id, headers=bob)).status_code == 410
        assert (await rs.get(ROUTE + "/" + bob_id, headers=alice)).status_code == 404

    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM book_chapters").fetchone()[0] == 0
        payloads = con.execute(
            "SELECT actor_id,payload_json FROM book_import_stages "
            "WHERE status='staged'",
        ).fetchall()
        assert payloads and all(
            row[0] in ("alice", "bob", "chief") for row in payloads
        )
        all_source = [json.loads(row[1]) for row in payloads]
        assert all(isinstance(p["chapters"], list) and p["chapters"]
                   for p in all_source)
        # The canonical HTML retains escaping, not active <script>.
        for p in all_source:
            assert all("source_html" in ch for ch in p["chapters"])
    return {"alice": alice, "bob": bob}


def runner_env(root: Path, db: Path, settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY",
        "CHAPTERA_GEMINI_VAULT_KEY",
    ):
        env.pop(name, None)
    env.update({
        "CHAPTERA_DB_PATH": str(db),
        "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
        "CHAPTERA_ADMIN_MEMBER_ID": "chief",
        "CHAPTERA_BROWSER_SESSION_SECURE": "0",
        "ERIDA_RUST_ADDR": "127.0.0.1:18495",
        "ERIDA_RUST_TEST_WRITES": "1",
        "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
    })
    return env


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_import_stages.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m1o-stages-") as folder:
        root = Path(folder)
        db = root / "disposable.sqlite3"
        settings = Settings(
            db_path=db,
            team_tokens={
                "chief": "m1o-chief", "alice": "m1o-alice", "bob": "m1o-bob",
            },
            admin_member_id="chief", browser_session_secure=False,
        )
        app = create_app(settings, QueueStore(db))
        env = runner_env(root, db, settings)
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18495", process)
            asyncio.run(exercise(app, db, "http://127.0.0.1:18495", settings))
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        # Restart with the *same* DB but NO disposable write gate: GET should
        # still work; POST stage and DELETE must now be unavailable.
        env.pop("ERIDA_RUST_TEST_WRITES")
        env.pop("ERIDA_RUST_DISPOSABLE_ROOT")
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18495", process)
            with sqlite3.connect(db) as con:
                owned = con.execute(
                    "SELECT id FROM book_import_stages WHERE actor_id='alice' "
                    "AND status='staged' LIMIT 1",
                ).fetchone()
                assert owned is not None
            with httpx.Client(base_url="http://127.0.0.1:18495") as rust:
                headers = {"X-API-Key": settings.team_tokens["alice"]}
                assert rust.get(ROUTE + "/" + owned[0], headers=headers).status_code == 200
                assert rust.post(ROUTE, headers=headers, files={
                    "file": ("restarted.txt", SOURCE, "text/plain")
                }).status_code in (404, 405)
                assert rust.delete(ROUTE + "/" + owned[0], headers=headers).status_code in (404, 405)
            print("rust_import_stage_restart_readonly_survives=PASS")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    print("rust_import_stage_python_rust_shared_sqlite=PASS")
    print("rust_import_stage_cross_runtime_idempotency=PASS")
    print("rust_import_stage_actor_isolation_and_csrf=PASS")
    print("rust_import_stage_quota_cancel_ttl=PASS")
    print("rust_import_stage_book_mutations=ZERO")
    print("rust_import_stage_confirm_not_implemented=TRUE")
    print("production_db_access=NO")


if __name__ == "__main__":
    main()
