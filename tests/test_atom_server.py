"""Golden server/DB compatibility tests for opt-in atom JSON jobs."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import JobSpec, QueueStore
from chaptera_server.worker import TranslationWorker
from chaptera_translation_core import ChapterInput, TranslationCore


def atom_reply(request):
    doc = json.loads(request.source_html_with_markers)
    index = {key: i for i, key in enumerate(doc['columns'])}
    rows = [dict(zip(doc['columns'], row)) for row in doc['rows']]
    segments = []
    for row in rows:
        domain = row['render_domain_id']
        if segments and segments[-1][0] == domain:
            segments[-1][1]['covers'].append(row['atom_id'])
            segments[-1][1]['translation'] += row['source_text'].replace('世界', 'Мир')
        else:
            segments.append((domain, {'covers': [row['atom_id']],
                                      'translation': row['source_text'].replace('世界', 'Мир')}))
    return json.dumps({'segments': [value for _, value in segments]}, ensure_ascii=False)


class NativeAtomProvider:
    calls = 0
    async def generate(self, request):
        self.calls += 1
        assert request.transport == 'atoms_v1'
        return atom_reply(request)


def test_end_to_end_team_api_persists_atom_provenance_and_survives_restart(tmp_path):
    db = tmp_path / 'jobs.db'
    repo = QueueStore(db)
    app = create_app(Settings(db_path=db, team_tokens={'alice': 'alice-secret', 'bob': 'bob-secret'}), repo)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            first = await client.post('/v1/jobs', headers={'X-API-Key': 'alice-secret'}, json={
                'chapter_id': '31', 'source_html': '<body><p>世界。</p><p>世界！</p></body>',
                'transport': 'atoms_v1',
            })
            assert first.status_code == 201, first.text
            job_id = first.json()['id']
            assert await TranslationWorker(QueueStore(db), NativeAtomProvider()).run_once()
            result = await client.get(f'/v1/jobs/{job_id}', headers={'X-API-Key': 'bob-secret'})
            assert result.status_code == 200
            assert result.json()['status'] == 'completed'
            assert result.json()['translated_html'] == '<body><p>Мир。</p><p>Мир！</p></body>'
            audit = result.json()['atom_provenance']
            assert len(audit) == 2
            assert all('target_hash' in row and 'source_text' not in row for row in audit)
            return job_id, audit
    jid, audit = asyncio.run(scenario())
    assert QueueStore(db).get_job(jid)['atom_provenance'] == audit


def test_bad_atom_result_is_never_committed_to_sqlite(tmp_path):
    store = QueueStore(tmp_path / 'db.sqlite3')
    job = store.create_job('alice', JobSpec('1', '<p>世界。</p>', transport='atoms_v1'))
    class BadProvider:
        async def generate(self, request):
            return '{"segments": [{"covers":["999999"],"translation":"Мир"}]}'
    assert asyncio.run(TranslationWorker(store, BadProvider()).run_once())
    saved = store.get_job(job['id'])
    assert saved['status'] == 'failed'
    assert saved['translated_html'] is None
    assert saved['atom_provenance'] == []


def test_legacy_sqlite_schema_migrates_and_keeps_html_v0_idempotency(tmp_path):
    db = tmp_path / 'old.sqlite3'
    # Build a v0.3 schema with a real queued entry without a transport field.
    temp = QueueStore(db)
    spec = JobSpec('old', '<p>世界。</p>')
    job = temp.create_job('alice', spec, idempotency_key='same')
    with temp._connect() as con:
        old_data = spec.normalized()
        old_data.pop('transport')
        import hashlib
        request_json = json.dumps(old_data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        con.execute('UPDATE jobs SET input_json=?,request_hash=? WHERE id=?',
                    (request_json, hashlib.sha256(request_json.encode('utf-8')).hexdigest(), job['id']))
        # SQLite needs table reconstruction to represent a pre-migration database.
        con.execute('ALTER TABLE jobs DROP COLUMN provenance_json')
    upgraded = QueueStore(db)
    assert upgraded.get_job(job['id'])['atom_provenance'] == []
    assert upgraded.create_job('alice', spec, idempotency_key='same')['id'] == job['id']
    # Simulate processing the previously queued, legacy HTML job unchanged.
    class OldProvider:
        async def generate(self, request):
            assert request.transport == 'html_v0'
            return request.source_html_with_markers.replace('世界', 'Мир')
    assert asyncio.run(TranslationWorker(upgraded, OldProvider()).run_once())
    result = upgraded.get_job(job['id'])
    assert result['status'] == 'completed'
    assert 'Мир' in result['translated_html']
    assert result['atom_provenance'] == []


def test_truncated_jsonl_recovers_only_missing_atom():
    calls = []
    class PartialProvider:
        async def generate(self, request):
            calls.append(request)
            packet = json.loads(atom_reply(request))['segments']
            if len(calls) == 1:
                return json.dumps(packet[0], ensure_ascii=False) + '\n{"covers": ["broken"'
            return json.dumps({'segments': packet}, ensure_ascii=False)
    result = asyncio.run(TranslationCore(PartialProvider()).translate(
        ChapterInput('2', '<p>世界。</p><p>世界！</p>', transport='atoms_v1')))
    assert len(calls) == 2
    assert result.unit_count == 2
    assert result.translated_html == '<p>Мир。</p><p>Мир！</p>'


def test_managed_long_chapter_translates_without_fabricated_partial_context(tmp_path):
    from chaptera_server.library import BookLibrary
    store = QueueStore(tmp_path / 'long.sqlite3')
    lib = BookLibrary(store)
    lib.create_book('long', 'Long chapter test')
    source = '<p>' + ('世界。' * 17000) + '</p>'
    lib.put_chapter('long', 'c1', chapter_index=1, chapter_path='1.xhtml',
                    source_html=source, expected_revision=0)
    spec = lib.new_job('long', 'c1', source_language='auto', target_language='Russian',
                       style_instructions='', max_attempts=3, transport='atoms_v1')
    job = store.create_job('alice', spec)
    provider = NativeAtomProvider()
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    result = store.get_job(job['id'])
    assert result['status'] == 'completed'
    assert 'Мир。' in result['translated_html']
    assert len(result['atom_provenance']) == result['unit_count']
    assert any(item['stage'] == 'context_skipped_long_chapter' for item in store.events(job['id']))
    assert lib.context_for_chapter('long', 'c1')['status'] == 'missing_or_stale'
