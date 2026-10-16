from __future__ import annotations

import asyncio

import httpx

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore
from chaptera_server.worker import TranslationWorker


class FakeTranslationProvider:
    async def generate(self, request):
        return request.source_html_with_markers.replace("世界正在燃烧。", "Мир горит.")


def test_team_submit_then_worker_then_team_reads_translation(tmp_path):
    store = QueueStore(tmp_path / "job-db.sqlite3")
    app = create_app(Settings(db_path=tmp_path / "job-db.sqlite3", team_tokens={
        "alice": "alice-secret", "bob": "bob-secret",
    }), store)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            submit = await client.post("/v1/jobs", headers={"X-API-Key": "alice-secret"}, json={
                "chapter_id": "5", "source_html": "<body><p>世界正在燃烧。</p></body>",
            })
            assert submit.status_code == 201
            jid = submit.json()["id"]
            assert await TranslationWorker(store, FakeTranslationProvider()).run_once()
            query = await client.get(f"/v1/jobs/{jid}", headers={"X-API-Key": "bob-secret"})
            assert query.status_code == 200
            assert query.json()["translated_html"] == "<body><p>Мир горит.</p></body>"
            assert query.json()["actor_id"] == "alice"
            events = await client.get(f"/v1/jobs/{jid}/events", headers={"X-API-Key": "bob-secret"})
            assert events.status_code == 200
            assert "validating" in {event["stage"] for event in events.json()["events"]}

    asyncio.run(scenario())
