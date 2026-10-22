#!/usr/bin/env python3
"""M1h: shared-SQLite Python/Rust admin issue, rotate, disable and job revocation.

Uses actual Python ASGI and Rust HTTP loopback with a synthetic SQLite.
No production write, paid Gemini call or real administrator credential.
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
import time

import httpx
from compare_rust_api import launch, ready
from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore


async def exercise(app, path: Path, rust_url: str, settings: Settings) -> None:
    admin = {"X-API-Key": settings.team_tokens["chief"]}
    moderator = {"X-API-Key": settings.team_tokens["mod"]}
    root = "/v1/admin/members"
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                          base_url="http://python", timeout=15) as py,
        httpx.AsyncClient(base_url=rust_url, timeout=15) as rs,
    ):
        assert (await rs.post(root, json={"member_id": "unauthorized"})).status_code == 401
        assert (await rs.post(root, headers=moderator,
                              json={"member_id": "unauthorized"})).status_code == 403
        assert (await rs.post(root, headers=admin,
                              json={"member_id": "1illegal"})).status_code == 422
        assert (await rs.post(root, headers=admin,
                              json={"member_id": "chief"})).status_code == 409
        assert (await rs.post(root, headers=admin,
                              json={"member_id": "mod"})).status_code == 409
        assert (await rs.post(root, headers=admin,
                              json={"member_id": "extra", "fake": "value"})).status_code == 422
        assert (await rs.post(root + "/chief/rotate", headers=admin)).status_code == 409
        assert (await rs.post(root + "/chief/disable", headers=admin)).status_code == 409
        assert (await rs.post(root + "/missing/rotate", headers=admin)).status_code == 404
        assert (await rs.post(root + "/missing/disable", headers=admin)).status_code == 404

        # Rust issue -> Python and Rust authentication against identical rows.
        rust_created = await rs.post(root, headers=admin, json={"member_id": "scribe"})
        assert rust_created.status_code == 201, rust_created.text
        issued = rust_created.json()
        assert set(issued) == {"member_id", "role", "status", "access_token"}
        assert issued["member_id"] == "scribe" and issued["role"] == "moderator"
        assert issued["status"] == "active"
        secret = issued["access_token"]
        assert secret.startswith("re_") and len(secret) >= 45
        assert "no-store" in rust_created.headers["cache-control"]
        secret_header = {"X-API-Key": secret}
        assert (await py.get("/v1/me", headers=secret_header)).json() == {
            "member_id": "scribe", "role": "moderator",
        }
        assert (await rs.get("/v1/me", headers=secret_header)).json() == {
            "member_id": "scribe", "role": "moderator",
        }

        # Python issue -> Rust auth, and listing must not leak any tokens.
        from_python = await py.post(root, headers=admin, json={"member_id": "archivist"})
        assert from_python.status_code == 201
        py_secret = from_python.json()["access_token"]
        assert (await rs.get("/v1/me", headers={"X-API-Key": py_secret})).json() == {
            "member_id": "archivist", "role": "moderator",
        }
        py_list, rust_list = await asyncio.gather(
            py.get(root, headers=admin), rs.get(root, headers=admin))
        assert py_list.status_code == rust_list.status_code == 200
        assert py_list.json() == rust_list.json()
        assert secret not in py_list.text and py_secret not in rust_list.text
        assert (await rs.post(root, headers=admin,
                              json={"member_id": "scribe"})).status_code == 409

        # Two distinct processes compete for one new member ID: one winner,
        # one conflict; audit event created exactly once.
        concurrent = await asyncio.gather(
            py.post(root, headers=admin, json={"member_id": "racer"}),
            rs.post(root, headers=admin, json={"member_id": "racer"}),
        )
        assert sorted(result.status_code for result in concurrent) == [201, 409], [
            (x.status_code, x.text) for x in concurrent
        ]
        assert (await py.get(root, headers=admin)).json() == (
            await rs.get(root, headers=admin)).json()

        # A Python-issued cookie must be invalidated immediately after Rust
        # rotation, including on the existing Python API.
        first_login = await py.post("/v1/session", json={"api_key": secret})
        assert first_login.status_code == 201
        old_cookie = {"Cookie": "readera_session=" +
                               first_login.cookies.get("readera_session")}
        rotated = await rs.post(root + "/scribe/rotate", headers=admin)
        assert rotated.status_code == 200, rotated.text
        new_secret = rotated.json()["access_token"]
        assert new_secret != secret
        for client in (rs, py):
            assert (await client.get("/v1/me", headers=secret_header)).status_code == 401
            assert (await client.get("/v1/me", headers=old_cookie)).status_code == 401
            assert (await client.get("/v1/me", headers={
                "X-API-Key": new_secret,
            })).status_code == 200

        # Reverse direction: Python rotation invalidates a key issued by Python
        # and immediately makes its replacement available on Rust.
        rotated_py = await py.post(root + "/archivist/rotate", headers=admin)
        assert rotated_py.status_code == 200
        newer_py_secret = rotated_py.json()["access_token"]
        assert newer_py_secret != py_secret
        assert (await rs.get("/v1/me", headers={
            "X-API-Key": py_secret,
        })).status_code == 401
        assert (await rs.get("/v1/me", headers={
            "X-API-Key": newer_py_secret,
        })).status_code == 200

        # Administrator browser cookie has mutation-CSRF requirements.
        admin_login = await py.post("/v1/session", json={
            "api_key": settings.team_tokens["chief"],
        })
        assert admin_login.status_code == 201
        admin_cookie = {"Cookie": "readera_session=" +
                                 admin_login.cookies.get("readera_session")}
        forbidden = await rs.post(root, headers=admin_cookie,
                                  json={"member_id": "csrf-probe"})
        assert forbidden.status_code == 403
        allowed = await rs.post(root, headers={
            **admin_cookie, "X-ReadEra-CSRF": admin_login.json()["csrf_token"],
        }, json={"member_id": "csrf-probe"})
        assert allowed.status_code == 201, allowed.text

        # Scribe's book must be handed off before a disable; Python and Rust
        # must both enforce the accountable book owner invariant.
        owned = await rs.post("/v1/books", headers={
            "X-API-Key": new_secret,
        }, json={"book_id": "scribe-book", "title": "Owned novel"})
        assert owned.status_code == 201, owned.text
        assert owned.json()["owner_member_id"] == "scribe"
        denied = await rs.post(root + "/scribe/disable", headers=admin)
        assert denied.status_code == 409
        assert denied.json()["detail"] == (
            "Reassign owned books before disabling this moderator"
        )
        assert (await py.post(root + "/scribe/disable", headers=admin)).status_code == 409

        moved = await py.put("/v1/books/scribe-book/owner", headers=admin, json={
            "owner_member_id": "archivist", "expected_owner_revision": 0,
        })
        assert moved.status_code == 200, moved.text

        # Durable translation jobs must be cancelled/signal-cancelled atomically
        # when that moderator loses access. Synthetic jobs avoid paid work.
        with sqlite3.connect(path) as db:
            timestamp = time.time()
            for name, status in (
                ("m1h-queued", "queued"),
                ("m1h-retry", "retry_wait"),
                ("m1h-running", "running"),
                ("m1h-done", "completed"),
            ):
                db.execute(
                    "INSERT INTO jobs "
                    "(id,actor_id,chapter_id,input_json,request_hash,status,"
                    "run_after,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (name, "scribe", name, "{}", "synthetic-hash",
                     status, timestamp, timestamp, timestamp),
                )
            db.commit()

        disabled = await rs.post(root + "/scribe/disable", headers=admin)
        assert disabled.status_code == 200, disabled.text
        assert disabled.json() == {
            "member_id": "scribe", "role": "moderator", "status": "disabled",
        }
        assert (await rs.post(root + "/scribe/disable", headers=admin)).json() == disabled.json()
        for client in (rs, py):
            assert (await client.get("/v1/me", headers={
                "X-API-Key": new_secret,
            })).status_code == 401
        assert (await py.get(root, headers=admin)).json() == (
            await rs.get(root, headers=admin)).json()

        with sqlite3.connect(path) as db:
            jobs = db.execute(
                "SELECT id,status,cancel_requested FROM jobs "
                "WHERE actor_id='scribe' ORDER BY id",
            ).fetchall()
            assert jobs == [
                ("m1h-done", "completed", 0),
                ("m1h-queued", "cancelled", 1),
                ("m1h-retry", "cancelled", 1),
                ("m1h-running", "running", 1),
            ], jobs
            events = db.execute(
                "SELECT job_id,stage,detail FROM job_events "
                "WHERE job_id LIKE 'm1h-%' ORDER BY job_id",
            ).fetchall()
            assert events == [
                ("m1h-queued", "cancelled", "member_disabled"),
                ("m1h-retry", "cancelled", "member_disabled"),
                ("m1h-running", "cancel_requested", "member_disabled"),
            ], events
            history = db.execute(
                "SELECT member_id,actor_id,action FROM team_access_events "
                "ORDER BY id",
            ).fetchall()
            assert history.count(("scribe", "chief", "disable")) == 1
            assert history.count(("racer", "chief", "issue")) == 1
            assert history.count(("scribe", "chief", "rotate")) == 1
            assert history.count(("archivist", "chief", "rotate")) == 1
            assert db.execute(
                "SELECT active,token_sha256 FROM team_access_credentials "
                "WHERE member_id='scribe'",
            ).fetchone() == (0, None)
            assert all(secret not in str(row) and new_secret not in str(row)
                       for row in history)
        database = path.read_bytes()
        for raw in (secret, py_secret, new_secret, newer_py_secret):
            assert raw.encode() not in database

        print("rust_admin_issue_cross_runtime_and_reserved_ids=PASS")
        print("rust_admin_issue_concurrent_one_winner=PASS")
        print("rust_admin_rotation_cookie_and_token_revocation=PASS")
        print("rust_admin_disable_requires_owner_handoff=PASS")
        print("rust_admin_disable_cancels_pending_jobs_and_audits=PASS")
        print("rust_admin_browser_csrf_and_no_secret_storage=PASS")
        print("production_sqlite_access=no")
        print("gemini_calls=0")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_admin_credentials.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m1h-admin-") as directory:
        root = Path(directory)
        db_path = root / "disposable.sqlite3"
        settings = Settings(
            db_path=db_path,
            team_tokens={"chief": "m1h-chief", "mod": "m1h-mod"},
            admin_member_id="chief", browser_session_secure=False,
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
            "CHAPTERA_BROWSER_SESSION_SECURE": "0",
            "ERIDA_RUST_ADDR": "127.0.0.1:18489",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
        })
        runner = launch([binary], env)
        try:
            ready("http://127.0.0.1:18489", runner)
            asyncio.run(exercise(app, db_path, "http://127.0.0.1:18489", settings))
            assert runner.poll() is None
        finally:
            if runner.poll() is None:
                runner.terminate()
                try:
                    runner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    runner.kill()
                    runner.wait(timeout=5)


if __name__ == "__main__":
    main()
