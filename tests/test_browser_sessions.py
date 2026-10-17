"""RE-03: durable browser sessions without weakening the existing API-key boundary."""
from __future__ import annotations

import asyncio
import sqlite3
import time

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore


ADMIN = "admin-secret-for-tests"
MOD = "moderator-secret-for-tests"


def make_app(tmp_path, *, secure=False):
    settings = Settings(
        db_path=tmp_path / "session.sqlite3",
        team_tokens={"chief": ADMIN, "mod": MOD},
        admin_member_id="chief",
        browser_session_secure=secure,
    )
    return create_app(settings, QueueStore(settings.db_path)), settings


def test_session_login_restore_logout_and_legacy_header(tmp_path):
    app, settings = make_app(tmp_path)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/v1/session")).status_code == 401
            assert (await client.get("/v1/books")).status_code == 401
            assert (await client.post("/v1/session", json={"api_key": "wrong"})).status_code == 401
            login = await client.post("/v1/session", json={"api_key": MOD})
            assert login.status_code == 201
            assert "httponly" in login.headers["set-cookie"].lower()
            assert "samesite=strict" in login.headers["set-cookie"].lower()
            assert "no-store" in login.headers["cache-control"]
            assert MOD not in str(app.state.store.db_path.read_bytes())
            csrf = login.json()["csrf_token"]
            assert login.json()["role"] == "moderator"
            assert len(csrf) == 64
            assert (await client.get("/v1/session")).json()["csrf_token"] == csrf
            assert (await client.get("/v1/me")).json()["role"] == "moderator"
            assert (await client.get("/v1/admin/members")).status_code == 403
            assert (await client.post("/v1/books", json={
                "book_id": "b1", "title": "Test",
            })).status_code == 403
            assert (await client.post("/v1/books", json={
                "book_id": "b1", "title": "Test",
            }, headers={"X-ReadEra-CSRF": "wrong"})).status_code == 403
            created = await client.post("/v1/books", json={
                "book_id": "b1", "title": "Test",
            }, headers={"X-ReadEra-CSRF": csrf})
            assert created.status_code == 201, created.text
            assert (await client.get("/v1/books")).status_code == 200
            # Header-authenticated clients must retain their original contract.
            assert (await client.get("/v1/me", headers={"X-API-Key": ADMIN})).json()["role"] == "admin"
            assert (await client.get("/v1/me", headers={"X-API-Key": "wrong"})).status_code == 401
            assert (await client.post("/v1/session/logout")).status_code == 403
            assert (await client.post("/v1/session/logout", headers={
                "X-ReadEra-CSRF": csrf,
            })).status_code == 204
            assert (await client.get("/v1/session")).status_code == 401
            assert (await client.get("/v1/books")).status_code == 401
            assert (await client.get("/v1/me", headers={"X-API-Key": MOD})).status_code == 200

    asyncio.run(exercise())


def test_rotated_and_disabled_moderator_immediately_lose_browser_sessions(tmp_path):
    app, settings = make_app(tmp_path)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as mod, httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as admin:
            assert (await mod.post("/v1/session", json={"api_key": MOD})).status_code == 201
            assert (await mod.get("/v1/session")).status_code == 200
            rotated = await admin.post("/v1/admin/members/mod/rotate", headers={"X-API-Key": ADMIN})
            assert rotated.status_code == 200, rotated.text
            new_key = rotated.json()["access_token"]
            assert (await mod.get("/v1/session")).status_code == 401
            assert (await mod.get("/v1/books")).status_code == 401
            assert (await mod.post("/v1/session", json={"api_key": MOD})).status_code == 401
            assert (await mod.post("/v1/session", json={"api_key": new_key})).status_code == 201
            assert (await mod.get("/v1/me")).status_code == 200
            assert (await admin.post("/v1/admin/members/mod/disable",
                                     headers={"X-API-Key": ADMIN})).status_code == 200
            assert (await mod.get("/v1/session")).status_code == 401
            assert (await mod.get("/v1/me")).status_code == 401
            # Session is not a third role; global policy remains unchanged.
            assert (await admin.get("/v1/me", headers={"X-API-Key": ADMIN})).json()["role"] == "admin"

    asyncio.run(exercise())


def test_session_persists_restart_but_expires_and_is_not_plaintext(tmp_path):
    app, settings = make_app(tmp_path)

    async def issue():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/v1/session", json={"api_key": ADMIN})
            return response.cookies.get("readera_session")

    token = asyncio.run(issue())
    assert token and token.startswith("rs_")
    with sqlite3.connect(settings.db_path) as con:
        row = con.execute("SELECT token_sha256 FROM team_browser_sessions").fetchone()
        assert row and row[0] != token
        assert token.encode() not in settings.db_path.read_bytes()

    restarted = create_app(settings, QueueStore(settings.db_path))

    async def verify():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as client:
            client.cookies.set("readera_session", token)
            assert (await client.get("/v1/session")).json()["role"] == "admin"
            with sqlite3.connect(settings.db_path) as con:
                con.execute("UPDATE team_browser_sessions SET expires_at=?", (time.time()-1,))
            assert (await client.get("/v1/session")).status_code == 401

    asyncio.run(verify())


def test_http_cookie_defaults_secure_and_reject_cross_site_login(tmp_path):
    app, _settings = make_app(tmp_path, secure=True)

    async def verify():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://readera.test"
        ) as client:
            denied = await client.post("/v1/session", json={"api_key": ADMIN},
                                       headers={"Sec-Fetch-Site": "cross-site"})
            assert denied.status_code == 403
            response = await client.post("/v1/session", json={"api_key": ADMIN})
            assert response.status_code == 201
            assert "secure" in response.headers["set-cookie"].lower()

    asyncio.run(verify())
