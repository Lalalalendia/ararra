"""SQLite browser key throttle: fail closed, restart, spoofed headers."""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time

import httpx

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore
from chaptera_server.team_login_throttle import MAX_ATTEMPTS, WINDOW_SECONDS


def test_failures_lock_even_valid_key_and_survive_restart(tmp_path):
    path = tmp_path / "login.sqlite3"
    settings = Settings(db_path=path, team_tokens={"administrator": "top-secret"},
                        admin_member_id="administrator", browser_session_secure=False)
    app = create_app(settings)

    async def attack():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as client:
            for i in range(8):
                response = await client.post("/v1/session", json={"api_key": "bad"})
                assert response.status_code == 401, (i, response.text)
            blocked = await client.post("/v1/session", json={"api_key": "top-secret"})
            assert blocked.status_code == 429
            assert int(blocked.headers["Retry-After"]) >= 1
            assert blocked.headers["Cache-Control"] == "private, no-store"
    asyncio.run(attack())
    assert b"top-secret" not in path.read_bytes()
    assert b"127.0.0.1" not in path.read_bytes()
    second = create_app(settings, QueueStore(path))

    async def after_restart():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second),
                                    base_url="http://test") as client:
            assert (await client.post("/v1/session", json={"api_key": "top-secret"})).status_code == 429
            with sqlite3.connect(path) as con:
                con.execute(
                    "UPDATE browser_login_rate SET locked_until=0,window_started=?,attempts=0,failures=0",
                    (time.time()-WINDOW_SECONDS-1,),
                )
            assert (await client.post("/v1/session", json={"api_key": "top-secret"})).status_code == 201

    asyncio.run(after_restart())


def test_valid_logins_cannot_reset_attempt_budget_or_spoof_peer(tmp_path):
    path = tmp_path / "flood.sqlite3"
    app = create_app(Settings(db_path=path, team_tokens={"chief": "valid-key"},
                              admin_member_id="chief", browser_session_secure=False))

    async def flood():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as client:
            for number in range(MAX_ATTEMPTS):
                response = await client.post(
                    "/v1/session", json={"api_key": "valid-key"},
                    headers={"X-Forwarded-For": f"203.0.113.{number%200+1}"},
                )
                assert response.status_code == 201, (number, response.text)
            capped = await client.post("/v1/session", json={"api_key": "valid-key"},
                                       headers={"X-Forwarded-For": "203.0.113.204"})
            assert capped.status_code == 429
            with sqlite3.connect(path) as con:
                rows = con.execute("SELECT attempts,failures FROM browser_login_rate").fetchall()
            assert rows == [(MAX_ATTEMPTS, 0)]

    asyncio.run(flood())
