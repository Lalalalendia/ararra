from __future__ import annotations

import asyncio

import httpx

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore


def test_private_api_requires_team_token_and_idempotency(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    app = create_app(Settings(db_path=tmp_path / "jobs.db", team_tokens={"alice": "test-secret"}), store)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/v1/jobs")).status_code == 401
            headers = {"X-API-Key": "test-secret", "Idempotency-Key": "upload-chapter-1"}
            body = {"chapter_id": "1", "source_html": "<p>世界正在燃烧。</p>"}
            added = await client.post("/v1/jobs", json=body, headers=headers)
            assert added.status_code == 201, added.text
            jid = added.json()["id"]
            second = await client.post("/v1/jobs", json=body, headers=headers)
            assert second.json()["id"] == jid
            other = await client.post("/v1/jobs", json={**body, "source_html": "changed"}, headers=headers)
            assert other.status_code == 409
            assert len((await client.get("/v1/jobs", headers=headers)).json()["jobs"]) == 1
            assert (await client.get(f"/v1/jobs/{jid}", headers=headers)).json()["status"] == "queued"
            assert (await client.post(f"/v1/jobs/{jid}/cancel", headers=headers)).json()["status"] == "cancelled"

    asyncio.run(scenario())


def test_api_fails_closed_when_tokens_are_unconfigured(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "jobs.db", team_tokens={}))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            assert (await client.get("/v1/jobs", headers={"X-API-Key": "anything"})).status_code == 401

    asyncio.run(scenario())


def test_api_rejects_conflicting_glossary_versions_before_queueing(tmp_path):
    store = QueueStore(tmp_path / 'jobs.db')
    app = create_app(Settings(db_path=tmp_path / 'jobs.db', team_tokens={'alice': 'test-secret'}), store)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            result = await client.post('/v1/jobs', headers={'X-API-Key': 'test-secret'}, json={
                'chapter_id': '517', 'source_html': '<p>李明 пришёл.</p>',
                'chapter_path': 'ch517.xhtml',
                'glossary': {'李明': 'Ли Мин'},
                'glossary_versions': {'李明': [
                    {'scope': ['ch517.xhtml'], 'override': {'rus': 'Ли Мин'}},
                    {'scope': ['ch517.xhtml'], 'override': {'rus': 'Ли Минь'}},
                ]},
            })
            assert result.status_code == 422
            assert not store.list_jobs()
    asyncio.run(scenario())
