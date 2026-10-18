"""Key-only authorization contract and safe retirement of legacy passwords."""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time

import httpx

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore


def _app(tmp_path):
    path = tmp_path / "readera-key-only.sqlite3"
    settings = Settings(
        db_path=path,
        team_tokens={"operator": "host-admin-secret", "mod": "moderator-secret"},
        admin_member_id="operator",
        browser_session_secure=False,
    )
    return create_app(settings), settings


def test_only_key_login_is_exposed_and_moderator_can_work(tmp_path):
    app, _ = _app(tmp_path)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            # No password-based login, issuance or changes remain reachable.
            assert (await client.post("/v1/session/password", json={
                "member_id": "mod", "password": "legacy",
            })).status_code == 404
            assert (await client.post("/v1/session/password/change", json={
                "current_password": "legacy", "new_password": "different",
            })).status_code == 404
            assert (await client.post(
                "/v1/admin/members/mod/password",
                headers={"X-API-Key": "host-admin-secret"},
            )).status_code == 404
            assert (await client.get("/v1/books")).status_code == 401
            key_login = await client.post("/v1/session", json={
                "api_key": "moderator-secret",
            })
            assert key_login.status_code == 201
            assert key_login.json()["role"] == "moderator"
            assert key_login.json()["auth_kind"] == "team_key"
            assert key_login.json()["must_change_password"] is False
            assert "httponly" in key_login.headers["set-cookie"].lower()
            assert (await client.get("/v1/books")).status_code == 200
            assert (await client.post("/v1/session/logout", headers={
                "X-ReadEra-CSRF": key_login.json()["csrf_token"],
            })).status_code == 204
            assert (await client.get("/v1/books")).status_code == 401

    asyncio.run(check())


def test_historical_password_cookie_is_not_accepted(tmp_path):
    app, settings = _app(tmp_path)
    raw = "rs_test-password-session-token-only"
    with sqlite3.connect(settings.db_path) as con:
        con.execute(
            """INSERT INTO team_browser_sessions
               (token_sha256,member_id,credential_sha256,auth_kind,created_at,expires_at)
               VALUES(?,?,?,?,?,?)""",
            (hashlib.sha256(raw.encode()).hexdigest(), "mod",
             hashlib.sha256(b"obsolete-password-fingerprint").hexdigest(),
             "password", time.time(), time.time() + 3600),
        )

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            client.cookies.set("readera_session", raw)
            assert (await client.get("/v1/session")).status_code == 401
            assert (await client.get("/v1/books")).status_code == 401
            assert (await client.post("/v1/session", json={
                "api_key": "moderator-secret",
            })).status_code == 201
            assert (await client.get("/v1/books")).status_code == 200

    asyncio.run(check())


def test_legacy_key_cookie_schema_stays_compatible(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    QueueStore(path)
    raw = "rs_known-key-session-for-schema-upgrade"
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE team_browser_sessions (
                token_sha256 TEXT PRIMARY KEY,member_id TEXT NOT NULL,
                credential_sha256 TEXT NOT NULL,created_at REAL NOT NULL,
                expires_at REAL NOT NULL,revoked_at REAL
            );
        """)
        con.execute(
            "INSERT INTO team_browser_sessions VALUES(?,?,?,?,?,NULL)",
            (hashlib.sha256(raw.encode()).hexdigest(), "mod",
             hashlib.sha256(b"moderator-secret").hexdigest(),
             time.time(), time.time() + 3600),
        )
    app = create_app(Settings(
        db_path=path,
        team_tokens={"operator": "host-admin-secret", "mod": "moderator-secret"},
        admin_member_id="operator",browser_session_secure=False,
    ))

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            client.cookies.set("readera_session", raw)
            assert (await client.get("/v1/session")).json()["auth_kind"] == "team_key"
            assert (await client.get("/v1/books")).status_code == 200

    asyncio.run(check())
