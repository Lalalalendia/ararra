"""ReadEra RE-06B: local RPD visibility is private and never billable."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

import chaptera_server.quota as quota
from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.quota import DailyQuotaLedger, pacific_window
from chaptera_server.storage import QueueStore

MOD = {"X-API-Key": "test-moderator-key"}


def settings(tmp_path, *, limit=0):
    return Settings(
        db_path=tmp_path / "quota-status.sqlite3",
        team_tokens={"administrator": "test-admin-key",
                     "moderator": "test-moderator-key"},
        admin_member_id="administrator",
        gemini_model="test-model",
        gemini_quota_project="private-project-123" if limit else "",
        gemini_requests_per_day=limit,
    )


def test_unconfigured_status_does_not_claim_unlimited_google_usage(tmp_path):
    cfg = settings(tmp_path)
    app = create_app(cfg)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://readera.test"
        ) as client:
            assert (await client.get("/v1/quota/gemini")).status_code == 401
            reply = await client.get("/v1/quota/gemini", headers=MOD)
            assert reply.status_code == 200
            assert reply.json() == {"enabled": False, "model": "test-model"}
    asyncio.run(scenario())


def test_quota_status_reads_ledger_without_reserving_calls_or_leaking_project(
    tmp_path, monkeypatch,
):
    moment = datetime(2026, 10, 17, 12, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(quota, "time", SimpleNamespace(time=lambda: moment))
    cfg = settings(tmp_path, limit=21)
    store = QueueStore(cfg.db_path)
    app = create_app(cfg, store)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://readera.test"
        ) as client:
            before = await client.get("/v1/quota/gemini", headers=MOD)
            assert before.status_code == 200, before.text
            status = before.json()
            assert status == {
                "enabled": True,
                "model": "test-model",
                "pacific_day": pacific_window(moment)[0],
                "used": 0, "limit": 21, "remaining": 21,
                "reset_at": pacific_window(moment)[1],
                "externally_blocked": False,
            }
            assert "private-project-123" not in before.text
            with store._connect() as con:
                assert con.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='gemini_daily_requests'"
                ).fetchone() is None

            ledger = DailyQuotaLedger(
                store, "private-project-123", "test-model", 21
            )
            ledger.reserve()
            after = await client.get("/v1/quota/gemini", headers=MOD)
            assert after.json()["used"] == 1
            assert after.json()["remaining"] == 20
            ledger.block_day()
            blocked = await client.get("/v1/quota/gemini", headers=MOD)
            assert blocked.json()["externally_blocked"] is True
            assert blocked.json()["used"] == 1
            assert blocked.json()["remaining"] == 0
            assert "private-project-123" not in blocked.text
            assert (await client.get("/v1/quota/gemini")).status_code == 401
    asyncio.run(scenario())
