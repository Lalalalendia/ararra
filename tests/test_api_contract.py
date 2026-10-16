"""Pin pre-v0.6 public API behavior while import endpoints are added."""
from __future__ import annotations

import asyncio

import httpx

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore


def test_legacy_jobs_contract_still_accepts_minimal_payload(tmp_path):
    store = QueueStore(tmp_path / 'jobs.db')
    app = create_app(Settings(db_path=store.db_path, team_tokens={'team': 'secret'}), store)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            headers = {'X-API-Key': 'secret', 'Idempotency-Key': 'one'}
            content = {'chapter_id': '0001', 'source_html': '<p>第一章。</p>'}
            first = await c.post('/v1/jobs', headers=headers, json=content)
            assert first.status_code == 201, first.text
            saved = first.json()
            assert saved['status'] == 'queued'
            assert saved['id']
            same = await c.post('/v1/jobs', headers=headers, json=content)
            assert same.status_code == 201 and same.json()['id'] == saved['id']
            change = await c.post('/v1/jobs', headers=headers, json={**content, 'chapter_id': 'two'})
            assert change.status_code == 409
            result = await c.get('/v1/jobs/' + saved['id'], headers=headers)
            assert result.status_code == 200
            assert result.json()['status'] == 'queued'
            assert result.json().get('transport', 'html_v0') == 'html_v0'
            assert (await c.get('/v1/jobs/' + saved['id'] + '/events', headers=headers)).status_code == 200
            assert (await c.post('/v1/jobs/' + saved['id'] + '/cancel', headers=headers)).json()['status'] == 'cancelled'
    asyncio.run(scenario())


def test_book_revision_auth_and_conflict_contract_survives_import_addition(tmp_path):
    store = QueueStore(tmp_path / 'db.sqlite3')
    app = create_app(Settings(db_path=store.db_path, team_tokens={'alice': 'alpha', 'bob': 'bravo'}), store)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            h_a, h_b = {'X-API-Key': 'alpha'}, {'X-API-Key': 'bravo'}
            assert (await c.get('/v1/books/b')).status_code == 401
            assert (await c.get('/v1/books/b', headers=h_a)).status_code == 404
            created = await c.post('/v1/books', headers=h_a, json={'book_id': 'b', 'title': 'Старинный роман'})
            assert created.status_code == 201
            data = {'chapter_index': 9, 'chapter_path': 'chapter/9.xhtml',
                    'source_html': '<p>原文</p>', 'expected_revision': 0}
            saved = await c.put('/v1/books/b/chapters/c9', headers=h_a, json=data)
            assert saved.status_code == 200
            assert saved.json()['source_revision'] == 1
            existing = await c.get('/v1/books/b/chapters/c9', headers=h_b)
            assert existing.status_code == 200
            assert existing.json()['source_html'] == '<p>原文</p>'
            conflict = await c.put('/v1/books/b/chapters/c9', headers=h_b,
                                   json={**data, 'source_html': '<p>Изменённый исходник</p>'})
            assert conflict.status_code == 409
            posted = await c.post('/v1/books/b/chapters/c9/translate', headers=h_b, json={})
            assert posted.status_code == 201
            # The existing API must still submit a managed job without a context fingerprint.
            assert posted.json()['id']
    asyncio.run(scenario())
