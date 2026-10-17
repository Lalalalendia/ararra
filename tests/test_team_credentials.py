"""Security and functional regressions for two-role managed moderator credentials."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore

ADMIN = {"X-API-Key": "test-chief-key"}
OLD_MOD = {"X-API-Key": "test-moderator-key"}


def make_app(tmp_path, *, admin="chief"):
    settings = Settings(
        db_path=tmp_path / "roles.sqlite3",
        team_tokens={"chief": "test-chief-key", "mod": "test-moderator-key"},
        admin_member_id=admin,
    )
    return create_app(settings), settings


def test_admin_issues_one_time_access_code_and_revocation_survives_restart(tmp_path):
    app, settings = make_app(tmp_path)

    async def work():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as client:
            denied = await client.post("/v1/admin/members", json={"member_id": "guest"})
            assert denied.status_code == 401
            denied = await client.post("/v1/admin/members", json={"member_id": "guest"},
                                       headers=OLD_MOD)
            assert denied.status_code == 403
            issued = await client.post("/v1/admin/members",
                                       headers=ADMIN, json={"member_id": "maria"})
            assert issued.status_code == 201, issued.text
            payload = issued.json()
            token = payload.pop("access_token")
            assert token.startswith("re_") and len(token) >= 45
            assert payload == {"member_id": "maria", "role": "moderator", "status": "active"}
            assert "no-store" in issued.headers.get("Cache-Control", "")
            secret = {"X-API-Key": token}
            assert (await client.get("/v1/me", headers=secret)).json() == {
                "member_id": "maria", "role": "moderator"
            }
            created = await client.post("/v1/books", headers=secret,
                                        json={"book_id": "shared", "title": "Общая книга"})
            assert created.status_code == 201, created.text
            assert (await client.get("/v1/books/shared", headers=OLD_MOD)).status_code == 200
            assert (await client.get("/v1/admin/members", headers=secret)).status_code == 403
            members = await client.get("/v1/admin/members", headers=ADMIN)
            assert members.status_code == 200
            assert members.json()["members"] == [
                {"member_id": "chief", "role": "admin",
                 "status": "active", "credential_source": "configuration"},
                {"member_id": "maria", "role": "moderator",
                 "status": "active", "credential_source": "managed"},
                {"member_id": "mod", "role": "moderator",
                 "status": "active", "credential_source": "configuration"},
            ]
            assert token not in members.text
            with app.state.store._connect() as con:
                rows = [
                    tuple(row) for row in con.execute(
                        "SELECT member_id,token_sha256 FROM team_access_credentials"
                    )
                ]
                events = [tuple(row) for row in con.execute("SELECT * FROM team_access_events")]
            assert token not in str(rows) and token not in str(events)
            assert token.encode() not in settings.db_path.read_bytes()
            assert (await client.post("/v1/admin/members", headers=ADMIN,
                                      json={"member_id": "maria"})).status_code == 409

            disabled = await client.post("/v1/admin/members/maria/disable", headers=ADMIN)
            assert disabled.status_code == 200, disabled.text
            assert disabled.json()["status"] == "disabled"
            assert (await client.get("/v1/me", headers=secret)).status_code == 401
            assert (await client.get("/v1/admin/members", headers=ADMIN)).json()["members"][1]["status"] == "disabled"
            assert (await client.post("/v1/admin/members/chief/disable", headers=ADMIN)).status_code == 409

            restored = await client.post("/v1/admin/members/maria/rotate", headers=ADMIN)
            assert restored.status_code == 200, restored.text
            new = restored.json()["access_token"]
            assert new != token
            assert (await client.get("/v1/me", headers=secret)).status_code == 401
            assert (await client.get("/v1/me", headers={"X-API-Key": new})).status_code == 200
            assert (await client.post("/v1/admin/members/chief/rotate", headers=ADMIN)).status_code == 409
            return token, new

    before, current = asyncio.run(work())
    restarted = create_app(settings, QueueStore(settings.db_path))

    async def reboot():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://api"
        ) as client:
            assert (await client.get("/v1/me", headers={"X-API-Key": before})).status_code == 401
            assert (await client.get("/v1/me", headers={"X-API-Key": current})).json()["role"] == "moderator"

    asyncio.run(reboot())


def test_rotate_static_moderator_overrides_configured_token_without_restart(tmp_path):
    app, _ = make_app(tmp_path)

    async def work():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as c:
            assert (await c.get("/v1/me", headers=OLD_MOD)).status_code == 200
            rotated = await c.post("/v1/admin/members/mod/rotate", headers=ADMIN)
            assert rotated.status_code == 200, rotated.text
            new_key = rotated.json()["access_token"]
            assert (await c.get("/v1/me", headers=OLD_MOD)).status_code == 401
            assert (await c.get("/v1/me", headers={"X-API-Key": new_key})).json() == {
                "member_id": "mod", "role": "moderator"
            }
            assert (await c.post("/v1/admin/members/mod/disable", headers=ADMIN)).status_code == 200
            assert (await c.get("/v1/me", headers={"X-API-Key": new_key})).status_code == 401
            assert (await c.get("/v1/me", headers=OLD_MOD)).status_code == 401
            assert (await c.post("/v1/admin/members/unknown/rotate", headers=ADMIN)).status_code == 404
            assert (await c.post("/v1/admin/members/unknown/disable", headers=ADMIN)).status_code == 404
            assert (await c.post("/v1/admin/members", headers=ADMIN,
                                 json={"member_id": "chief"})).status_code == 409
            assert (await c.post("/v1/admin/members", headers=ADMIN,
                                 json={"member_id": "invalid/slash"})).status_code == 422

    asyncio.run(work())


def test_moderator_revoke_cancels_queued_and_requests_cancel_running_jobs(tmp_path):
    app, _ = make_app(tmp_path)
    store = app.state.store

    async def work():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as c:
            for index in [1, 2]:
                result = await c.post("/v1/jobs", headers=OLD_MOD, json={
                    "chapter_id": str(index), "source_html": "<p>你好。</p>",
                })
                assert result.status_code == 201, result.text
            with store._connect() as con:
                con.execute(
                    "UPDATE jobs SET status='running',lease_owner='test-lease' WHERE chapter_id='2'"
                )
            disabled = await c.post("/v1/admin/members/mod/disable", headers=ADMIN)
            assert disabled.status_code == 200, disabled.text
            assert (await c.get("/v1/me", headers=OLD_MOD)).status_code == 401
            with store._connect() as con:
                rows = [
                    tuple(row) for row in con.execute(
                        "SELECT chapter_id,status,cancel_requested FROM jobs ORDER BY chapter_id"
                    )
                ]
                events = [
                    tuple(row) for row in con.execute(
                        "SELECT stage,detail FROM job_events WHERE detail='member_disabled' ORDER BY job_id"
                    )
                ]
            assert rows == [("1", "cancelled", 1), ("2", "running", 1)]
            assert sorted(e[0] for e in events) == ["cancel_requested", "cancelled"]
            assert (await c.get("/v1/jobs", headers=ADMIN)).status_code == 200

    asyncio.run(work())


def test_disable_owner_requires_explicit_reassignment_if_owner_schema_exists(tmp_path):
    app, _ = make_app(tmp_path)
    lib = app.state.library
    lib.create_book("owned", "Owner must be deliberate")
    with app.state.store._connect() as con:
        columns = {row["name"] for row in con.execute("PRAGMA table_info(books)")}
        if "owner_member_id" not in columns:
            con.execute("ALTER TABLE books ADD COLUMN owner_member_id TEXT")
        con.execute("UPDATE books SET owner_member_id='mod' WHERE id='owned'")

    async def work():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as c:
            blocked = await c.post("/v1/admin/members/mod/disable", headers=ADMIN)
            assert blocked.status_code == 409, blocked.text
            assert "Reassign" in blocked.text
            assert (await c.get("/v1/me", headers=OLD_MOD)).status_code == 200
            # This emulates prior reviewed reassignment (PR #17 owns that API).
            with app.state.store._connect() as con:
                con.execute("UPDATE books SET owner_member_id='chief' WHERE id='owned'")
            assert (await c.post("/v1/admin/members/mod/disable",
                                 headers=ADMIN)).status_code == 200

    asyncio.run(work())


def test_admin_unset_cannot_issue_or_list_moderator_credentials(tmp_path):
    app, _ = make_app(tmp_path, admin="")

    async def work():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as c:
            assert (await c.get("/v1/me", headers=ADMIN)).json()["role"] == "moderator"
            assert (await c.get("/v1/admin/members", headers=ADMIN)).status_code == 403
            assert (await c.post("/v1/admin/members", headers=ADMIN,
                                 json={"member_id": "test"})).status_code == 403
            assert (await c.post("/v1/admin/members/mod/rotate", headers=ADMIN)).status_code == 403

    asyncio.run(work())
