#!/usr/bin/env python3
"""M1g: real Python ASGI/Rust loopback browser key-login, cookie and throttle.

Two independent HTTP implementations, one disposable SQLite, exact role and
CSRF policy. Does not claim production trusted-proxy identity or deployment.
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

ROOT = "/v1/session"
KEY_ADMIN = "m1g-admin-secret"
KEY_MOD = "m1g-mod-secret"


async def exercise(app, db_path: Path, endpoint: str) -> None:
    admin = {"X-API-Key": KEY_ADMIN}
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://python",
        ) as py,
        httpx.AsyncClient(base_url=endpoint, timeout=15) as rs,
    ):
        for client in (rs, py):
            unauth = await client.get(ROOT)
            assert unauth.status_code == 401, unauth.text
            assert unauth.json()["detail"] == "Действующая сессия необходима"
            # Session introspection must not accept an API header without cookie.
            assert (await client.get(ROOT, headers=admin)).status_code == 401

        cross_site = await rs.post(
            ROOT, headers={"Sec-Fetch-Site": "cross-site"},
            json={"api_key": KEY_MOD},
        )
        assert cross_site.status_code == 403
        assert cross_site.json()["detail"] == "Cross-site login forbidden"
        wrong = await rs.post(ROOT, json={"api_key": "invalid"})
        assert wrong.status_code == 401 and wrong.json()["detail"] == "Неверный ключ участника"

        login = await rs.post(ROOT, json={"api_key": KEY_MOD})
        assert login.status_code == 201, login.text
        assert login.json()["member_id"] == "mod"
        assert login.json()["role"] == "moderator"
        assert login.json()["auth_kind"] == "team_key"
        assert login.json()["must_change_password"] is False
        csrf = login.json()["csrf_token"]
        token = login.cookies.get("readera_session")
        assert token and token.startswith("rs_") and len(csrf) == 64
        assert "httponly" in login.headers["set-cookie"].lower()
        assert "samesite=strict" in login.headers["set-cookie"].lower()
        assert "no-store" in login.headers["cache-control"]
        assert "secure" not in login.headers["set-cookie"].lower()

        # New Rust cookie must work with the existing Python authentication.
        cookie = {"Cookie": "readera_session=" + token}
        rust_read = await rs.get(ROOT, headers=cookie)
        python_read = await py.get(ROOT, headers=cookie)
        assert rust_read.status_code == python_read.status_code == 200
        assert rust_read.json() == python_read.json() == login.json()
        assert (await rs.get("/v1/me", headers=cookie)).json()["member_id"] == "mod"
        assert (await py.get("/v1/me", headers=cookie)).json()["member_id"] == "mod"
        assert (await rs.get(ROOT, headers={
            **cookie, "X-API-Key": "invalid",
        })).json() == login.json(), "GET session must ignore X-API-Key"

        # Cookie mutations need the Python-compatible HMAC CSRF proof.
        missing_proof = await rs.post(
            "/v1/books", headers=cookie,
            json={"book_id": "m1g-cookie", "title": "Browser session"},
        )
        assert missing_proof.status_code == 403
        proof = {**cookie, "X-ReadEra-CSRF": csrf}
        created = await rs.post(
            "/v1/books", headers=proof,
            json={"book_id": "m1g-cookie", "title": "Browser session"},
        )
        assert created.status_code == 201, created.text
        assert (await py.get("/v1/books/m1g-cookie", headers=cookie)).status_code == 200

        no_csrf_logout = await rs.post(ROOT + "/logout", headers=cookie)
        assert no_csrf_logout.status_code == 403
        assert (await py.get(ROOT, headers=cookie)).status_code == 200
        logout = await rs.post(ROOT + "/logout", headers=proof)
        assert logout.status_code == 204, logout.text
        assert logout.content == b""
        assert "max-age=0" in logout.headers["set-cookie"].lower()
        assert "httponly" in logout.headers["set-cookie"].lower()
        assert (await rs.get(ROOT, headers=cookie)).status_code == 401
        assert (await py.get(ROOT, headers=cookie)).status_code == 401
        # Logout remains idempotent, even when a cookie is now revoked.
        assert (await rs.post(ROOT + "/logout", headers=cookie)).status_code == 204
        assert (await rs.post(ROOT + "/logout")).status_code == 204

        # Python-issued session must be readable and revocable from Rust.
        python_login = await py.post(ROOT, json={"api_key": KEY_MOD})
        assert python_login.status_code == 201, python_login.text
        py_token = python_login.cookies.get("readera_session")
        assert py_token.startswith("rs_")
        py_cookie = {"Cookie": "readera_session=" + py_token}
        assert (await rs.get(ROOT, headers=py_cookie)).json() == python_login.json()
        assert (await rs.post(
            ROOT + "/logout", headers={
                **py_cookie, "X-ReadEra-CSRF": python_login.json()["csrf_token"],
            },
        )).status_code == 204
        assert (await py.get(ROOT, headers=py_cookie)).status_code == 401

        # Managed-token rotation revokes a previously valid cookie at read.
        managed = await py.post(ROOT, json={"api_key": KEY_MOD})
        assert managed.status_code == 201
        managed_cookie = {"Cookie": "readera_session=" +
                                   managed.cookies.get("readera_session")}
        rotated = await py.post(
            "/v1/admin/members/mod/rotate", headers=admin,
        )
        assert rotated.status_code == 200, rotated.text
        new_key = rotated.json()["access_token"]
        assert (await rs.get(ROOT, headers=managed_cookie)).status_code == 401
        assert (await rs.post(ROOT, json={"api_key": KEY_MOD})).status_code == 401
        new_login = await rs.post(ROOT, json={"api_key": new_key})
        assert new_login.status_code == 201, new_login.text
        new_cookie = {"Cookie": "readera_session=" +
                               new_login.cookies.get("readera_session")}
        assert (await py.get(ROOT, headers=new_cookie)).status_code == 200
        disabled = await py.post("/v1/admin/members/mod/disable", headers=admin)
        assert disabled.status_code == 200, disabled.text
        assert (await rs.get(ROOT, headers=new_cookie)).status_code == 401
        assert (await py.get(ROOT, headers=new_cookie)).status_code == 401

        # Failures charged by Python and Rust share the same SQLite peer budget.
        # Two implementations cannot bypass each other's lock by alternating.
        for i in range(8):
            client = py if i % 2 else rs
            fail = await client.post(ROOT, json={"api_key": "not-valid"},
                                     headers={"X-Forwarded-For": f"198.51.100.{i+1}"})
            assert fail.status_code == 401, (i, fail.status_code, fail.text)
        blocked = await rs.post(ROOT, json={"api_key": KEY_ADMIN})
        assert blocked.status_code == 429, (blocked.status_code, blocked.text)
        assert int(blocked.headers["retry-after"]) >= 1
        assert (await py.post(ROOT, json={"api_key": KEY_ADMIN})).status_code == 429

        with sqlite3.connect(db_path) as con:
            row = con.execute(
                "SELECT attempts,failures FROM browser_login_rate",
            ).fetchall()
            assert len(row) == 1 and row[0][1] >= 8, row
            keys = con.execute(
                "SELECT token_sha256,credential_sha256,auth_kind FROM team_browser_sessions",
            ).fetchall()
            assert all(len(item[0]) == 64 and len(item[1]) == 64
                       and item[2] == "team_key" for item in keys)
            assert all(token not in str(row) for row in keys)
        file_contents = db_path.read_bytes()
        assert KEY_ADMIN.encode() not in file_contents
        assert KEY_MOD.encode() not in file_contents
        assert token.encode() not in file_contents
        assert b"127.0.0.1" not in file_contents

        print("rust_browser_session_login_python_rust_cookies=PASS")
        print("rust_browser_session_get_logout_bidirectional=PASS")
        print("rust_browser_session_cookie_csrf_and_header_precedence=PASS")
        print("rust_browser_session_rotation_disable_invalidation=PASS")
        print("rust_browser_session_cross_runtime_rate_limit=PASS")
        print("rust_browser_session_no_raw_secret_sqlite=PASS")
        print("rust_session_production_writer=disabled")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_browser_sessions.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m1g-session-") as directory:
        root = Path(directory)
        db_path = root / "disposable.sqlite3"
        settings = Settings(
            db_path=db_path,
            team_tokens={"chief": KEY_ADMIN, "mod": KEY_MOD},
            admin_member_id="chief", browser_session_secure=False,
        )
        app = create_app(settings, QueueStore(db_path))
        env = dict(os.environ)
        for key in (
            "CREDENTIALS_DIRECTORY", "CHAPTERA_GEMINI_API_KEY",
            "CHAPTERA_GEMINI_VAULT_KEY",
        ):
            env.pop(key, None)
        env.update({
            "CHAPTERA_DB_PATH": str(db_path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "CHAPTERA_BROWSER_SESSION_SECURE": "0",
            "ERIDA_RUST_ADDR": "127.0.0.1:18488",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
        })
        runner = launch([binary], env)
        try:
            ready("http://127.0.0.1:18488", runner)
            asyncio.run(exercise(app, db_path, "http://127.0.0.1:18488"))
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
