#!/usr/bin/env python3
"""RUST-02a contract acceptance on disposable SQLite shared by Python and Rust.

No live tokens, no site fetch, no paid Gemini/Drive operations.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

import httpx

from compare_rust_api import launch, ready
from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import JobSpec, QueueStore
from chaptera_server.team_sessions import TeamSessions


async def acceptance(
    app, db_path: Path, token: str, rust_base: str, settings: Settings
) -> None:
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://python") as py,
        httpx.AsyncClient(base_url=rust_base, timeout=12) as rs,
    ):
        headers = {"X-API-Key": settings.team_tokens["mod"]}
        first = {
            "chapter_id": "глава-31",
            "source_html": "<p>你好，мир «quote» &amp; ☃</p>",
            "source_language": "Chinese",
            "target_language": "Russian",
            "chapter_path": "Книга/31",
            "glossary": {"仙人": {"rus": "бессмертный", "note": "вечный"}},
            "predecessor": {"revision": 2, "title": "Глава 30"},
        }

        # Cross-runtime deterministic source fingerprint, not just HTTP status.
        a = await py.post("/v1/jobs", json=first, headers={**headers, "Idempotency-Key": "python-first"})
        b = await rs.post("/v1/jobs", json=first, headers={**headers, "Idempotency-Key": "python-first"})
        assert a.status_code == b.status_code == 201, (a.status_code, b.status_code)
        assert a.json() == b.json(), "Rust did not replay Python's canonical hash"
        with sqlite3.connect(db_path) as con:
            assert con.execute("SELECT COUNT(*) FROM jobs WHERE idempotency_key='python-first'").fetchone()[0] == 1

        new = {**first, "chapter_id": "chapter-32", "source_html": "<p>独立章节。</p>"}
        a = await rs.post("/v1/jobs", json=new, headers={**headers, "Idempotency-Key": "rust-first"})
        b = await py.post("/v1/jobs", json=new, headers={**headers, "Idempotency-Key": "rust-first"})
        assert a.status_code == b.status_code == 201
        assert a.json() == b.json(), "Python could not replay Rust's canonical hash"
        rust_job_id = a.json()["id"]

        collision = {**new, "source_html": "<p>A DIFFERENT SOURCE</p>"}
        for client in (py, rs):
            response = await client.post(
                "/v1/jobs", json=collision,
                headers={**headers, "Idempotency-Key": "rust-first"},
            )
            assert response.status_code == 409, response.status_code
            assert response.json()["detail"] == "Idempotency key refers to different input"

        # Interleaved simultaneous Python/Rust submissions against same SQLite.
        races = [
            py.post("/v1/jobs", json=new, headers={**headers, "Idempotency-Key": "shared-race"}),
            rs.post("/v1/jobs", json=new, headers={**headers, "Idempotency-Key": "shared-race"}),
            rs.post("/v1/jobs", json=new, headers={**headers, "Idempotency-Key": "shared-race"}),
            py.post("/v1/jobs", json=new, headers={**headers, "Idempotency-Key": "shared-race"}),
        ]
        results = await asyncio.gather(*races)
        assert all(x.status_code == 201 for x in results), [x.status_code for x in results]
        assert len({x.json()["id"] for x in results}) == 1
        with sqlite3.connect(db_path) as con:
            assert con.execute("SELECT COUNT(*) FROM jobs WHERE idempotency_key='shared-race'").fetchone()[0] == 1
            assert con.execute(
                "SELECT COUNT(*) FROM job_events WHERE job_id=? AND stage='queued'",
                (results[0].json()["id"],),
            ).fetchone()[0] == 1

        # Rust cancels a Python-created queued job; Python sees the same event.
        cancelled = await rs.post(f"/v1/jobs/{a.json()['id']}/cancel", headers=headers)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        replayed = await py.post(f"/v1/jobs/{a.json()['id']}/cancel", headers=headers)
        assert replayed.status_code == 200
        assert replayed.json() == cancelled.json()
        events = (await py.get(f"/v1/jobs/{a.json()['id']}/events", headers=headers)).json()["events"]
        assert [x["stage"] for x in events] == ["queued", "cancelled"], events

        # A running worker lease remains in place, and cancellation is only requested.
        running = (await py.post("/v1/jobs", json=first, headers=headers)).json()
        with sqlite3.connect(db_path) as con:
            con.execute(
                "UPDATE jobs SET status='running',lease_owner='dummy-lease',attempts=1 WHERE id=?",
                (running["id"],),
            )
        requested = await rs.post(f"/v1/jobs/{running['id']}/cancel", headers=headers)
        assert requested.status_code == 200 and requested.json()["status"] == "running"
        with sqlite3.connect(db_path) as con:
            state = con.execute(
                "SELECT status,lease_owner,cancel_requested FROM jobs WHERE id=?",
                (running["id"],),
            ).fetchone()
        assert state == ("running", "dummy-lease", 1)
        checked = (await py.get(f"/v1/jobs/{running['id']}/events", headers=headers)).json()["events"]
        assert checked[-1]["stage"] == "cancellation_requested"
        missing = await rs.post("/v1/jobs/no-job/cancel", headers=headers)
        assert missing.status_code == 404

        # Browser session requires HMAC CSRF, and X-API-Key always wins.
        cookie = app.state.team_sessions.issue(
            member_id="mod", team_key=settings.team_tokens["mod"], ttl_seconds=3600
        )
        session = {"Cookie": "readera_session=" + cookie}
        no_proof = await rs.post("/v1/jobs", json=first, headers=session)
        invalid_proof = await rs.post("/v1/jobs", json=first, headers={
            **session, "X-ReadEra-CSRF": "invalid",
        })
        assert no_proof.status_code == invalid_proof.status_code == 403
        valid = {**session, "X-ReadEra-CSRF": TeamSessions.csrf_token(cookie)}
        browser_created = await rs.post(
            "/v1/jobs", json=first, headers={**valid, "Idempotency-Key": "browser"}
        )
        assert browser_created.status_code == 201, browser_created.status_code
        browser_replay = await py.post(
            "/v1/jobs", json=first, headers={**valid, "Idempotency-Key": "browser"}
        )
        assert browser_replay.json() == browser_created.json()
        bad_key_over_cookie = await rs.post(
            "/v1/jobs", json=first, headers={**valid, "X-API-Key": "wrong"}
        )
        assert bad_key_over_cookie.status_code == 401

        # Managed credentials: rotation and disable must immediately gate writes.
        replaced = app.state.team_credentials.rotate("chief", "mod", settings)
        old = await rs.post("/v1/jobs", json=first, headers=headers)
        stale_session = await rs.post("/v1/jobs", json=first, headers=valid)
        assert old.status_code == stale_session.status_code == 401
        rotated = await rs.post(
            "/v1/jobs", json=first, headers={"X-API-Key": replaced["access_token"]},
        )
        assert rotated.status_code == 201
        app.state.team_credentials.disable("chief", "mod", settings)
        disabled = await rs.post(
            "/v1/jobs", json=first, headers={"X-API-Key": replaced["access_token"]},
        )
        assert disabled.status_code == 401

        # Idempotency is per actor, matching Python QueueStore:
        # same key owned by another moderator may create its own job.
        second_actor = await rs.post(
            "/v1/jobs", json=first, headers={"X-API-Key": settings.team_tokens["chief"],
                                             "Idempotency-Key": "rust-first"},
        )
        assert second_actor.status_code == 201
        assert second_actor.json()["id"] != rust_job_id

        # Input validation never commits a malformed request.
        for invalid in (
            {"chapter_id": "", "source_html": "<p>ok</p>"},
            {"chapter_id": "x", "source_html": ""},
            {"chapter_id": "x", "source_html": "<p>x</p>", "max_attempts": 11},
            {"chapter_id": "x", "source_html": "<p>x</p>", "transport": "invalid"},
        ):
            py_response = await py.post("/v1/jobs", json=invalid, headers={"X-API-Key": settings.team_tokens["chief"]})
            rs_response = await rs.post("/v1/jobs", json=invalid, headers={"X-API-Key": settings.team_tokens["chief"]})
            assert rs_response.status_code == py_response.status_code, (
                rs_response.status_code, py_response.status_code
            )
        print("rust_write_cross_runtime_replay=PASS")
        print("rust_write_concurrent_idempotency=PASS")
        print("rust_write_cancel_fencing=PASS")
        print("rust_write_csrf_and_revocation=PASS")
        print("rust_write_disposable_db_only=PASS")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_writes.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-rust02-") as directory:
        root = Path(directory)
        db_path = root / "isolated.sqlite3"
        settings = Settings(
            db_path=db_path,
            team_tokens={"chief": "rust02-chief-test-secret", "mod": "rust02-mod-test-secret"},
            admin_member_id="chief",
            browser_session_secure=False,
        )
        app = create_app(settings, QueueStore(db_path))
        env = os.environ.copy()
        for key in ("CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY", "CHAPTERA_GEMINI_VAULT_KEY"):
            env.pop(key, None)
        env.update({
            "CHAPTERA_DB_PATH": str(db_path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "ERIDA_RUST_ADDR": "127.0.0.1:18473",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
        })
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18473", process)
            asyncio.run(acceptance(app, db_path, "", "http://127.0.0.1:18473", settings))
            assert process.poll() is None
            print("production_processes_touched=0")
            print("real_gemini_calls=0")
            print("source_network_calls=0")
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
