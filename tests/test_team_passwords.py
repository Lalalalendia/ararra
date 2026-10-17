"""RE-03 password acceptance: Argon2, existing roles, revocation, throttling."""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time

import httpx

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore

ADMIN = {"X-API-Key": "readera-test-admin"}
MOD = {"X-API-Key": "readera-test-mod"}
CHANGED = "A private new passphrase 2026!"


def make_app(tmp_path):
    settings = Settings(db_path=tmp_path / "password.sqlite3",
                        team_tokens={"chief": ADMIN["X-API-Key"],
                                     "mod": MOD["X-API-Key"],
                                     "other": "other-key"},
                        admin_member_id="chief",
                        browser_session_secure=False)
    return create_app(settings), settings


def test_password_issue_first_change_restart_revocation(tmp_path):
    app, settings = make_app(tmp_path)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://app.test"
        ) as admin, httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://app.test"
        ) as member:
            path = "/v1/admin/members/mod/password"
            assert (await admin.post(path)).status_code == 401
            assert (await member.post(path, headers=MOD)).status_code == 403
            assert (await admin.post("/v1/admin/members/chief/password", headers=ADMIN)).status_code == 409
            assert (await admin.post("/v1/admin/members/nobody/password", headers=ADMIN)).status_code == 404

            created = await admin.post(path, headers=ADMIN)
            assert created.status_code == 200, created.text
            assert "no-store" in created.headers["cache-control"]
            temp = created.json()["temporary_password"]
            assert temp.startswith("rp_") and created.json()["must_change_password"] is True
            assert (await member.post("/v1/session/password", json={
                "member_id": "chief", "password": temp,
            })).status_code == 401
            assert (await member.post("/v1/session/password", json={
                "member_id": "mod", "password": "wrong",
            })).status_code == 401

            first = await member.post("/v1/session/password", json={
                "member_id": "mod", "password": temp,
            })
            assert first.status_code == 201, first.text
            assert first.json()["role"] == "moderator"
            assert first.json()["auth_kind"] == "password"
            assert first.json()["must_change_password"] is True
            assert "httponly" in first.headers["set-cookie"].lower()
            csrf = first.json()["csrf_token"]
            assert (await member.get("/v1/books")).status_code == 403
            assert (await member.get("/v1/me")).status_code == 403
            assert (await member.get("/v1/session")).json()["must_change_password"] is True
            assert (await member.post("/v1/session/password/change", json={
                "current_password": temp, "new_password": CHANGED,
            })).status_code == 403
            bad = await member.post("/v1/session/password/change", json={
                "current_password": "incorrect", "new_password": CHANGED,
            }, headers={"X-ReadEra-CSRF": csrf})
            assert bad.status_code == 401
            short = await member.post("/v1/session/password/change", json={
                "current_password": temp, "new_password": "short",
            }, headers={"X-ReadEra-CSRF": csrf})
            assert short.status_code == 422
            changed = await member.post("/v1/session/password/change", json={
                "current_password": temp, "new_password": CHANGED,
            }, headers={"X-ReadEra-CSRF": csrf})
            assert changed.status_code == 204, changed.text
            assert "readera_session=" in changed.headers["set-cookie"]
            assert (await member.get("/v1/session")).status_code == 401
            assert (await member.post("/v1/session/password", json={
                "member_id": "mod", "password": temp,
            })).status_code == 401
            ready = await member.post("/v1/session/password", json={
                "member_id": "mod", "password": CHANGED,
            })
            assert ready.status_code == 201, ready.text
            assert ready.json()["must_change_password"] is False
            assert (await member.get("/v1/books")).status_code == 200
            assert (await member.get("/v1/me")).json() == {
                "member_id": "mod", "role": "moderator"
            }
            # Team token remains a separate, still-supported credential.
            assert (await admin.get("/v1/me", headers=MOD)).status_code == 200
            assert (await admin.get("/v1/me", headers=ADMIN)).json()["role"] == "admin"

            reset = await admin.post(path, headers=ADMIN)
            assert reset.status_code == 200
            newer = reset.json()["temporary_password"]
            assert newer != temp
            assert (await member.get("/v1/session")).status_code == 401
            assert (await member.get("/v1/books")).status_code == 401
            return newer

    new_temp = asyncio.run(run())
    binary = settings.db_path.read_bytes()
    for leaked in (new_temp, CHANGED, "readera-test-mod"):
        assert leaked.encode() not in binary
    with sqlite3.connect(settings.db_path) as con:
        encoded, must_change = con.execute(
            "SELECT password_hash,must_change FROM team_password_credentials WHERE member_id='mod'"
        ).fetchone()
        assert encoded.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
        assert must_change == 1
        assert con.execute(
            "SELECT action FROM team_password_events ORDER BY id"
        ).fetchall() == [("issue",), ("change",), ("reset",)]
    restarted = create_app(settings, QueueStore(settings.db_path))

    async def reboot():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://app.test"
        ) as member, httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://app.test"
        ) as admin:
            login = await member.post("/v1/session/password", json={
                "member_id": "mod", "password": new_temp,
            })
            assert login.status_code == 201
            assert login.json()["must_change_password"] is True
            rotated = await admin.post("/v1/admin/members/mod/rotate", headers=ADMIN)
            assert rotated.status_code == 200
            assert (await member.get("/v1/session")).status_code == 401
            # A newer team key does not implicitly delete a configured password.
            assert (await member.post("/v1/session/password", json={
                "member_id": "mod", "password": new_temp,
            })).status_code == 201
            assert (await admin.post("/v1/admin/members/mod/disable",
                                     headers=ADMIN)).status_code == 200
            assert (await member.get("/v1/session")).status_code == 401
            assert (await member.post("/v1/session/password", json={
                "member_id": "mod", "password": new_temp,
            })).status_code == 401
            assert (await admin.post("/v1/admin/members/mod/password",
                                     headers=ADMIN)).status_code == 409

    asyncio.run(reboot())


def test_password_lockout_per_identifier_across_different_clients(tmp_path):
    app, settings = make_app(tmp_path)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://api") as admin:
            given = await admin.post("/v1/admin/members/mod/password", headers=ADMIN)
            assert given.status_code == 200
            temporary = given.json()["temporary_password"]
            other = await admin.post("/v1/admin/members/other/password", headers=ADMIN)
            assert other.status_code == 200
            other_temp = other.json()["temporary_password"]

        for i in range(6):
            ip = ("198.51.100." + str(i + 3), 3030)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=ip), base_url="http://api"
            ) as actor:
                denied = await actor.post("/v1/session/password", json={
                    "member_id": "mod", "password": "guessed-" + str(i),
                }, headers={"X-Forwarded-For": "198.18.0." + str(i)})
                assert denied.status_code == 401

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("203.0.113.20", 3030)),
            base_url="http://api",
        ) as actor:
            blocked = await actor.post("/v1/session/password", json={
                "member_id": "mod", "password": temporary,
            })
            assert blocked.status_code == 429
            assert int(blocked.headers["Retry-After"]) > 0
            assert blocked.headers["Cache-Control"] == "private, no-store"
            assert (await actor.post("/v1/session/password", json={
                "member_id": "other", "password": other_temp,
            })).status_code == 201
            with sqlite3.connect(settings.db_path) as con:
                count = con.execute("SELECT COUNT(*) FROM browser_login_rate").fetchone()[0]
                assert count >= 9
                con.execute(
                    "UPDATE browser_login_rate SET locked_until=0,window_started=?,attempts=0,failures=0",
                    (time.time() - 1000,),
                )
            assert (await actor.post("/v1/session/password", json={
                "member_id": "mod", "password": temporary,
            })).status_code == 201

    asyncio.run(scenario())


def test_old_browser_session_schema_migrates_without_logout(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    QueueStore(path)
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE team_browser_sessions (
                token_sha256 TEXT PRIMARY KEY,member_id TEXT NOT NULL,
                credential_sha256 TEXT NOT NULL,created_at REAL NOT NULL,
                expires_at REAL NOT NULL,revoked_at REAL
            );
        """)
        original = "rs_" + "known-session-token-for-migration" * 2
        con.execute(
            "INSERT INTO team_browser_sessions VALUES(?,?,?,?,?,NULL)",
            (hashlib.sha256(original.encode()).hexdigest(), "mod",
             hashlib.sha256(MOD["X-API-Key"].encode()).hexdigest(),
             time.time(), time.time() + 3600),
        )
    app = create_app(Settings(db_path=path, team_tokens={"chief": ADMIN["X-API-Key"],
                                                       "mod": MOD["X-API-Key"]},
                              admin_member_id="chief", browser_session_secure=False))

    async def verify():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://app") as actor:
            actor.cookies.set("readera_session", original)
            assert (await actor.get("/v1/session")).json()["auth_kind"] == "team_key"
            assert (await actor.get("/v1/books")).status_code == 200

    asyncio.run(verify())


def test_password_hashing_capacity_is_bounded_and_fails_closed(tmp_path):
    from chaptera_server import team_passwords

    app, _ = make_app(tmp_path)
    for _ in range(4):
        assert team_passwords._ARGON_SLOTS.acquire(blocking=False)
    try:
        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://app.test"
            ) as c:
                # Unknown account still runs an Argon2 dummy verification.
                busy = await c.post("/v1/session/password", json={
                    "member_id": "unknown", "password": "guess",
                })
                assert busy.status_code == 503
                assert busy.headers["Retry-After"] == "1"
                # Admin issuance also reserves bounded Argon2 capacity.
                issue = await c.post("/v1/admin/members/mod/password", headers=ADMIN)
                assert issue.status_code == 503
                assert issue.headers["Retry-After"] == "1"
        asyncio.run(scenario())
    finally:
        for _ in range(4):
            team_passwords._ARGON_SLOTS.release()
