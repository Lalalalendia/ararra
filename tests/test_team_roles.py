"""ReadEra only supports administrator and moderator as GLOBAL workspace roles."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.auth import InvalidTeamToken, role_for
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore

ADMIN = {"X-API-Key": "a-secret"}
MOD = {"X-API-Key": "m-secret"}


def settings(tmp_path, *, admin_id="alice"):
    return Settings(
        db_path=tmp_path / "jobs.sqlite3",
        team_tokens={"alice": "a-secret", "mod": "m-secret"},
        admin_member_id=admin_id,
    )


def test_global_roles_are_explicit_and_fail_closed(tmp_path):
    cfg = settings(tmp_path)
    assert role_for("alice", cfg) == "admin"
    assert role_for("mod", cfg) == "moderator"
    with pytest.raises(InvalidTeamToken):
        role_for("unknown", cfg)
    with pytest.raises(ValueError, match="admin member"):
        settings(tmp_path, admin_id="unknown")
    unset = settings(tmp_path, admin_id="")
    assert role_for("alice", unset) == "moderator"
    assert role_for("mod", unset) == "moderator"


def test_admin_from_environment_does_not_auto_promote_first_member(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAPTERA_DB_PATH", str(tmp_path / "configured.sqlite3"))
    monkeypatch.setenv("CHAPTERA_TEAM_TOKENS_JSON", json.dumps({"alice": "a-secret", "mod": "m-secret"}))
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("CHAPTERA_ADMIN_MEMBER_ID", raising=False)
    no_admin = Settings.from_env()
    assert no_admin.admin_member_id == ""
    assert role_for("alice", no_admin) == "moderator"
    monkeypatch.setenv("CHAPTERA_ADMIN_MEMBER_ID", "mod")
    configured = Settings.from_env()
    assert configured.admin_member_id == "mod"
    assert role_for("mod", configured) == "admin"
    monkeypatch.setenv("CHAPTERA_ADMIN_MEMBER_ID", "stranger")
    with pytest.raises(ValueError, match="admin member"):
        Settings.from_env()


def test_api_reports_only_two_global_roles_and_admin_roster_is_private(tmp_path):
    cfg = settings(tmp_path)
    store = QueueStore(cfg.db_path)
    app = create_app(cfg, store)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            assert (await c.get("/healthz")).status_code == 200
            assert (await c.get("/v1/me")).status_code == 401
            assert (await c.get("/v1/admin/members")).status_code == 401
            assert (await c.get("/v1/me", headers={"X-API-Key": "unknown"})).status_code == 401
            assert (await c.get("/v1/me", headers=ADMIN)).json() == {
                "member_id": "alice", "role": "admin"
            }
            assert (await c.get("/v1/me", headers=MOD)).json() == {
                "member_id": "mod", "role": "moderator"
            }
            assert (await c.get("/v1/admin/members", headers=MOD)).status_code == 403
            response = await c.get("/v1/admin/members", headers=ADMIN)
            assert response.status_code == 200
            assert response.json() == {"members": [
                {"member_id": "alice", "role": "admin"},
                {"member_id": "mod", "role": "moderator"},
            ]}
            assert "a-secret" not in response.text
            assert "m-secret" not in response.text
            assert "team_tokens" not in response.text

            # All books and jobs are a SHARED technical workspace, not per-book ACL.
            created = await c.post("/v1/books", headers=ADMIN, json={
                "book_id": "shared", "title": "Техническая книга", "owner_member_id": "mod"
            })
            assert created.status_code == 201, created.text
            assert (await c.get("/v1/books/shared", headers=MOD)).status_code == 200
            chapter = await c.put("/v1/books/shared/chapters/c1", headers=MOD, json={
                "chapter_index": 1, "chapter_path": "chapters/1.xhtml",
                "source_html": "<p>你好。</p>", "expected_revision": 0,
            })
            assert chapter.status_code == 200, chapter.text
            assert (await c.get("/v1/books/shared/chapters/c1", headers=ADMIN)).status_code == 200
            posted = await c.post("/v1/books/shared/chapters/c1/translate",
                                  headers=MOD, json={})
            assert posted.status_code == 201, posted.text
            jid = posted.json()["id"]
            assert (await c.get("/v1/jobs/" + jid, headers=ADMIN)).status_code == 200
            assert (await c.get("/v1/jobs/" + jid + "/events", headers=ADMIN)).status_code == 200
            assert any(job["id"] == jid for job in
                       (await c.get("/v1/jobs", headers=ADMIN)).json()["jobs"])

    asyncio.run(scenario())

    # Role is determined from current explicit config on restart, not persisted
    # guessed book ownership or a role embedded in a client token.
    app2 = create_app(cfg, QueueStore(cfg.db_path))
    assert app2.state.settings.admin_member_id == "alice"


def test_no_implicit_admin_in_existing_configuration(tmp_path):
    cfg = settings(tmp_path, admin_id="")
    app = create_app(cfg)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            assert (await c.get("/v1/me", headers=ADMIN)).json()["role"] == "moderator"
            assert (await c.get("/v1/admin/members", headers=ADMIN)).status_code == 403
            assert (await c.post("/v1/books", headers=ADMIN,
                                 json={"book_id": "shared", "title": "Book"})).status_code == 201

    asyncio.run(scenario())


def test_book_ownership_is_not_book_access_control(tmp_path):
    cfg = settings(tmp_path)
    app = create_app(cfg)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            made = await c.post("/v1/books", headers=MOD,
                                json={"book_id": "a", "title": "A"})
            assert made.status_code == 201, made.text
            assert made.json()["owner_member_id"] == "mod"
            assert made.json()["owner_revision"] == 0
            assert (await c.get("/v1/books", headers=ADMIN)).json()["books"][0]["owner_member_id"] == "mod"
            assert (await c.put("/v1/books/a/owner", headers=MOD, json={
                "owner_member_id": "mod", "expected_owner_revision": 0,
            })).status_code == 403
            invalid = await c.put("/v1/books/a/owner", headers=ADMIN, json={
                "owner_member_id": "ghost", "expected_owner_revision": 0,
            })
            assert invalid.status_code == 422
            assert (await c.get("/v1/books/a", headers=MOD)).status_code == 200
            assert (await c.post("/v1/books", headers=ADMIN,
                                 json={"book_id": "bad", "title": "Bad"})).status_code == 422
            assert (await c.post("/v1/books", headers=MOD,
                                 json={"book_id": "other", "title": "Other",
                                       "owner_member_id": "alice"})).status_code == 422

    asyncio.run(scenario())


def test_book_transfer_cas_audit_and_old_sqlite_migration(tmp_path):
    import sqlite3
    cfg = Settings(db_path=tmp_path / "owner.sqlite3",
                   team_tokens={"alice": "a-secret", "mod": "m-secret",
                                "mod2": "c-secret"}, admin_member_id="alice")
    old = QueueStore(cfg.db_path)
    from chaptera_server.library import BookLibrary
    lib = BookLibrary(old)
    lib.create_book("legacy", "Old", owner_member_id="mod")
    with old._connect() as db:
        db.execute("UPDATE books SET owner_member_id=NULL WHERE id='legacy'")
    app = create_app(cfg, QueueStore(cfg.db_path))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            assert (await c.get("/v1/books/legacy", headers=ADMIN)).json()["owner_member_id"] is None
            result = await c.put("/v1/books/legacy/owner", headers=ADMIN, json={
                "owner_member_id": "mod2", "expected_owner_revision": 0,
            })
            assert result.status_code == 200, result.text
            assert result.json()["owner_revision"] == 1
            assert (await c.put("/v1/books/legacy/owner", headers=ADMIN, json={
                "owner_member_id": "mod", "expected_owner_revision": 0,
            })).status_code == 409
            assert (await c.put("/v1/books/legacy/owner", headers=ADMIN, json={
                "owner_member_id": "alice", "expected_owner_revision": 1,
            })).status_code == 422
            assert (await c.get("/v1/books/legacy", headers=MOD)).status_code == 200

    asyncio.run(scenario())
    with old._connect() as db:
        event = db.execute(
            "SELECT previous_owner_member_id,new_owner_member_id,actor_id "
            "FROM book_owner_events WHERE book_id='legacy' AND revision=1"
        ).fetchone()
        assert tuple(event) == (None, "mod2", "alice")
