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


def test_api_preserves_versioned_glossary_and_context_until_worker(tmp_path):
    from chaptera_translation_core import glossary_fingerprint, make_snapshot, sha256_text
    from chaptera_translation_core.core.worker_helpers.translation_completeness import strip_source_boilerplate
    from chaptera_translation_core.prompt_policy import scoped_glossary

    source = '<body><p>李明 пришёл домой.</p></body>'
    chapter_path = 'OEBPS/Text/517.xhtml'
    glossary = {'李明': {'rus': 'Ли Мин', 'authority': 'required', 'note': 'персонаж'}}
    versions = {'李明': [{'scope': [chapter_path], 'override': {'rus': 'Ли Минь'}}]}
    effective = scoped_glossary(glossary, versions, chapter_path)
    snapshot = make_snapshot(
        chapter_path=chapter_path,
        source_hash=sha256_text(strip_source_boilerplate(source)),
        glossary_hash=glossary_fingerprint(effective),
        context={'summary': 'Возвращение героя домой.'},
    )
    store = QueueStore(tmp_path / 'job-db.sqlite3')
    app = create_app(Settings(db_path=tmp_path / 'job-db.sqlite3', team_tokens={
        'alice': 'alice-secret', 'bob': 'bob-secret',
    }), store)
    requests = []

    class SimulatedProvider:
        async def generate(self, request):
            requests.append(request)
            return request.source_html_with_markers.replace('李明', 'Ли Минь')

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            headers = {'X-API-Key': 'alice-secret', 'Idempotency-Key': 'book-517'}
            payload = {
                'chapter_id': '517', 'source_html': source,
                'chapter_path': chapter_path, 'glossary': glossary,
                'glossary_versions': versions,
                'chapter_context_snapshot': snapshot,
                'style_instructions': 'Сохраняй сдержанный стиль.',
            }
            posted = await client.post('/v1/jobs', headers=headers, json=payload)
            assert posted.status_code == 201, posted.text
            jid = posted.json()['id']
            again = await client.post('/v1/jobs', headers=headers, json=payload)
            assert again.json()['id'] == jid
            conflict = await client.post('/v1/jobs', headers=headers, json={
                **payload, 'glossary_versions': {'李明': [{'scope': [chapter_path], 'override': {'rus': 'Чужое имя'}}]},
            })
            assert conflict.status_code == 409
            assert await TranslationWorker(QueueStore(store.db_path), SimulatedProvider()).run_once()
            result = await client.get(f'/v1/jobs/{jid}', headers={'X-API-Key': 'bob-secret'})
            assert result.status_code == 200
            assert result.json()['status'] == 'completed'
            assert result.json()['translated_html'] == '<body><p>Ли Минь пришёл домой.</p></body>'
            events = await client.get(f'/v1/jobs/{jid}/events', headers={'X-API-Key': 'bob-secret'})
            assert any(e['stage'] == 'completed' for e in events.json()['events'])

    asyncio.run(scenario())
    assert len(requests) == 1
    assert 'Ли Минь' in requests[0].prompt
    assert 'SOURCE-DERIVED CHAPTER CONTEXT' in requests[0].prompt
    assert 'Сохраняй сдержанный стиль.' in requests[0].prompt
