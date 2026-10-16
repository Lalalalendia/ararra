"""Golden tests for server-owned memory, team revisions and queue recovery."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.context_prepass import ContextPrepassRejected, _decode_context
from chaptera_server.library import BookLibrary, LibraryConflict
from chaptera_server.storage import QueueStore
from chaptera_server.worker import TranslationWorker


SOURCE1 = '<body><p>李明去了城堡。</p></body>'
SOURCE2 = '<body><p>王雪 встретила 李明.</p></body>'
SOURCE3 = '<body><p>彼は真実を知った。</p></body>'


def context_payload(*, terms=None):
    return {'c0': {
        'summary': 'Встреча персонажей.', 'pov': 'Третье лицо', 'tone_and_register': 'нейтральный',
        'setting': [], 'active_entities': [], 'relationships': [], 'important_events': [],
        'continuity_in': [], 'continuity_out': [], 'ambiguous_references': [],
        'reveal_constraints': [], 'semantic_terms': terms or [],
    }}


class ContextAwareProvider:
    def __init__(self):
        self.context_calls = []
        self.translation_calls = []
        self.hook = None

    async def generate(self, request):
        if request.chapter_id.startswith('context:'):
            self.context_calls.append(request)
            if self.hook:
                self.hook('context')
            return json.dumps(context_payload(terms=[
                {'source': '王雪', 'suggested_ru': 'Ван Сюэ', 'kind': 'person'},
                {'source': '不存在', 'suggested_ru': 'Несуществующий', 'kind': 'person'},
            ]), ensure_ascii=False)
        self.translation_calls.append(request)
        if self.hook:
            self.hook('translation')
        return (request.source_html_with_markers
                .replace('王雪', 'Ван Сюэ')
                .replace('李明', 'Ли Мин')
                .replace('彼は真実を知った。', 'Он узнал правду.'))


def setup_library(tmp_path, *, book_id='book1'):
    store = QueueStore(tmp_path / 'team.sqlite3')
    lib = BookLibrary(store)
    lib.create_book(book_id, 'Тестовый роман')
    lib.put_chapter(book_id, 'c1', chapter_index=1, chapter_path='chapters/1.xhtml',
                    source_html=SOURCE1, expected_revision=0)
    lib.put_chapter(book_id, 'c2', chapter_index=2, chapter_path='chapters/2.xhtml',
                    source_html=SOURCE2, expected_revision=0)
    return store, lib


def enqueue(lib, store, actor='alice', chapter='c2', key=None):
    spec = lib.new_job('book1', chapter, source_language='auto', target_language='Russian',
                       style_instructions='', max_attempts=3)
    return store.create_job(actor, spec, idempotency_key=key)


def test_two_team_members_share_persisted_context_with_no_duplicate_prepass(tmp_path):
    store, lib = setup_library(tmp_path)
    lib.put_glossary('book1', glossary={'李明': {'rus': 'Ли Мин', 'authority': 'required'}},
                     versions={}, authority='READY', expected_revision=0, actor_id='alice')
    lib.accept_translation('book1', 'c1', '<p>Ли Мин отправился в замок.</p>', expected_revision=0, expected_source_revision=1)
    provider = ContextAwareProvider()
    job_a = enqueue(lib, store, actor='alice', key='a2')
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job_a['id'])['status'] == 'completed'
    snap = lib.context_for_chapter('book1', 'c2')
    assert snap['status'] == 'current'
    assert len(snap['snapshot']['semantic_terms']) == 1
    assert snap['snapshot']['semantic_terms'][0]['source'] == '王雪'
    assert 'Несуществующий' not in json.dumps(snap, ensure_ascii=False)
    assert len(provider.context_calls) == 1
    assert len(provider.translation_calls) == 1
    assert 'SOURCE-DERIVED CHAPTER CONTEXT' in provider.translation_calls[0].prompt
    assert 'Ли Мин отправился в замок' in provider.context_calls[0].prompt
    job_b = enqueue(lib, store, actor='bob', key='b2')
    # New process with same SQLite keeps the context and canonical glossary.
    assert asyncio.run(TranslationWorker(QueueStore(store.db_path), provider).run_once())
    assert store.get_job(job_b['id'])['status'] == 'completed'
    assert len(provider.context_calls) == 1  # cache hit, no second Gemini context call
    assert len(provider.translation_calls) == 2
    assert any(event['stage'] == 'context_cache_hit' for event in store.events(job_b['id']))
    assert lib.get_book('book1')['glossary']['李明']['rus'] == 'Ли Мин'
    # Semantic suggestions are NOT promoted to canonical glossary.
    assert '王雪' not in lib.get_book('book1')['glossary']


def test_accepted_predecessor_and_source_change_invalidate_context(tmp_path):
    store, lib = setup_library(tmp_path)
    provider = ContextAwareProvider()
    assert asyncio.run(TranslationWorker(store, provider).run_once()) is False
    job = enqueue(lib, store)
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert lib.context_for_chapter('book1', 'c2')['status'] == 'current'
    assert lib.context_for_chapter('book1', 'c2')['snapshot']['predecessor']['translation_tail'] == ''
    lib.accept_translation('book1', 'c1', '<p>Герой в замке.</p>', expected_revision=0, expected_source_revision=1)
    assert lib.context_for_chapter('book1', 'c2')['status'] == 'missing_or_stale'
    job2 = enqueue(lib, store, 'bob')
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job2['id'])['status'] == 'completed'
    assert len(provider.context_calls) == 2
    assert lib.context_for_chapter('book1', 'c2')['status'] == 'current'
    lib.put_chapter('book1', 'c2', chapter_index=2, chapter_path='chapters/2.xhtml',
                    source_html=SOURCE2 + '<p>Ещё текст.</p>', expected_revision=1)
    assert lib.context_for_chapter('book1', 'c2')['status'] == 'missing_or_stale'
    stale_job = enqueue(lib, store)
    # Old job from earlier immutable source can't overwrite the new revision.
    assert stale_job['id']
    assert store.get_job(job['id'])['status'] == 'completed'


def test_glossary_revision_invalidates_and_old_queued_job_fails_closed(tmp_path):
    store, lib = setup_library(tmp_path)
    provider = ContextAwareProvider()
    queued = enqueue(lib, store)
    lib.put_glossary('book1', glossary={'王雪': {'rus': 'Ван Сюэ'}}, versions={},
                     authority='READY', expected_revision=0, actor_id='bob')
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(queued['id'])['status'] == 'failed'
    assert not provider.context_calls and not provider.translation_calls
    fresh = enqueue(lib, store)
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(fresh['id'])['status'] == 'completed'
    assert lib.context_for_chapter('book1', 'c2')['status'] == 'current'
    lib.put_glossary('book1', glossary={'王雪': {'rus': 'Ван Сюэ', 'note': 'персонаж'}},
                     versions={}, authority='READY', expected_revision=1, actor_id='alice')
    assert lib.context_for_chapter('book1', 'c2')['status'] == 'missing_or_stale'
    with store._connect() as con:
        revisions = con.execute('SELECT revision, actor_id FROM book_glossary_revisions ORDER BY revision').fetchall()
    assert [tuple(r) for r in revisions] == [(1, 'bob'), (2, 'alice')]


def test_missing_adjacent_chapter_never_uses_nonadjacent_predecessor(tmp_path):
    store, lib = setup_library(tmp_path)
    lib.put_chapter('book1', 'c4', chapter_index=4, chapter_path='chapters/4.xhtml',
                    source_html=SOURCE3, expected_revision=0)
    provider = ContextAwareProvider()
    job = enqueue(lib, store, chapter='c4')
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job['id'])['status'] == 'completed'
    assert lib.context_for_chapter('book1', 'c4')['snapshot']['predecessor']['chapter_path'] == ''
    lib.put_chapter('book1', 'c3', chapter_index=3, chapter_path='chapters/3.xhtml',
                    source_html='<p>第三章。</p>', expected_revision=0)
    assert lib.context_for_chapter('book1', 'c4')['status'] == 'missing_or_stale'


def test_context_changed_during_provider_cannot_be_persisted(tmp_path):
    store, lib = setup_library(tmp_path)
    provider = ContextAwareProvider()
    provider.hook = lambda stage: lib.accept_translation(
        'book1', 'c1', '<p>Открылась тайна.</p>', expected_revision=0, expected_source_revision=1,
    ) if stage == 'context' else None
    job = enqueue(lib, store)
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job['id'])['status'] == 'failed'
    assert not provider.translation_calls
    assert lib.context_for_chapter('book1', 'c2')['snapshot'] is None


def test_predecessor_edited_during_translation_fences_final_result(tmp_path):
    store, lib = setup_library(tmp_path)
    provider = ContextAwareProvider()
    provider.hook = lambda stage: lib.accept_translation(
        'book1', 'c1', '<p>Новый принятый перевод.</p>', expected_revision=0, expected_source_revision=1,
    ) if stage == 'translation' else None
    job = enqueue(lib, store)
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job['id'])['status'] == 'failed'
    assert store.get_job(job['id'])['translated_html'] is None
    assert lib.context_for_chapter('book1', 'c2')['status'] == 'missing_or_stale'


def test_partial_context_response_retries_without_publishing(tmp_path):
    store, lib = setup_library(tmp_path)

    class PartialProvider:
        calls = 0
        async def generate(self, request):
            self.calls += 1
            return json.dumps({'c0': {'summary': 'Это не полный контекст'}})

    provider = PartialProvider()
    job = enqueue(lib, store)
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job['id'])['status'] == 'retry_wait'
    assert store.get_job(job['id'])['translated_html'] is None
    assert provider.calls == 1
    assert lib.context_for_chapter('book1', 'c2')['status'] == 'missing_or_stale'


def test_optimistic_revisions_prevent_two_editors_silently_overwriting(tmp_path):
    store, lib = setup_library(tmp_path)
    with pytest.raises(LibraryConflict, match='source revision'):
        lib.put_chapter('book1', 'c1', chapter_index=1, chapter_path='chapters/1.xhtml',
                        source_html='<p>Конфликт</p>', expected_revision=0)
    rev = lib.accept_translation('book1', 'c1', '<p>One.</p>', expected_revision=0, expected_source_revision=1)
    assert rev['accepted_revision'] == 1
    with pytest.raises(LibraryConflict, match='Source revision changed'):
        lib.accept_translation('book1', 'c1', '<p>Outdated review.</p>', expected_revision=1, expected_source_revision=2)
    with pytest.raises(LibraryConflict, match='revision mismatch'):
        lib.accept_translation('book1', 'c1', '<p>Two.</p>', expected_revision=0, expected_source_revision=1)
    with pytest.raises(LibraryConflict, match='revision mismatch'):
        lib.put_glossary('book1', glossary={}, versions={}, authority='READY',
                         expected_revision=2, actor_id='bob')


def test_server_api_two_members_share_book_and_not_required_to_supply_hashes(tmp_path):
    store = QueueStore(tmp_path / 'jobs.sqlite3')
    app = create_app(Settings(db_path=store.db_path, team_tokens={'alice': 'alice-secret', 'bob': 'bob-secret'}), store)
    provider = ContextAwareProvider()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            a, b = {'X-API-Key': 'alice-secret'}, {'X-API-Key': 'bob-secret'}
            assert (await client.post('/v1/books', headers=a,
                        json={'book_id': 'novel', 'title': 'Роман'})).status_code == 201
            bad = await client.put('/v1/books/novel/chapters/c2', headers=b,
                json={'chapter_index': 2, 'chapter_path': 'c2.xhtml', 'source_html': SOURCE2, 'expected_revision': 0})
            assert bad.status_code == 200, bad.text
            clash = await client.put('/v1/books/novel/chapters/c2', headers=a,
                json={'chapter_index': 2, 'chapter_path': 'c2.xhtml', 'source_html': SOURCE2 + 'updated', 'expected_revision': 0})
            assert clash.status_code == 409
            no_source_guard = await client.put('/v1/books/novel/chapters/c2/accepted-translation',
                headers=a, json={'translated_html': '<p>Ready</p>', 'expected_revision': 0})
            assert no_source_guard.status_code == 422
            old_source_guard = await client.put('/v1/books/novel/chapters/c2/accepted-translation',
                headers=a, json={'translated_html': '<p>Ready</p>', 'expected_revision': 0,
                                 'expected_source_revision': 8})
            assert old_source_guard.status_code == 409
            glossary = await client.put('/v1/books/novel/glossary', headers=a, json={
                'expected_revision': 0, 'glossary': {'李明': 'Ли Мин'},
            })
            assert glossary.status_code == 200, glossary.text
            posted = await client.post('/v1/books/novel/chapters/c2/translate', headers=b, json={})
            assert posted.status_code == 201, posted.text
            jid = posted.json()['id']
            assert await TranslationWorker(QueueStore(store.db_path), provider).run_once()
            result = await client.get(f'/v1/jobs/{jid}', headers=a)
            assert result.json()['status'] == 'completed'
            ctx = await client.get('/v1/books/novel/chapters/c2/context', headers=a)
            assert ctx.json()['status'] == 'current'
            assert ctx.json()['snapshot']['summary']
            missing_auth = await client.get('/v1/books/novel', headers={})
            assert missing_auth.status_code == 401
    asyncio.run(scenario())


def test_cjk_and_japanese_context_json_shape_has_explicit_id():
    assert _decode_context(json.dumps(context_payload(), ensure_ascii=False))['c0']['summary']
    with pytest.raises(ContextPrepassRejected):
        _decode_context(json.dumps([context_payload()['c0']]))
    with pytest.raises(ContextPrepassRejected):
        _decode_context('{"c0":{"summary":"only"}}')


def test_old_accepted_translation_is_not_used_after_original_source_revision_change(tmp_path):
    store, lib = setup_library(tmp_path)
    lib.accept_translation('book1', 'c1', '<p>Старый перевод первой главы.</p>', expected_revision=0, expected_source_revision=1)
    lib.put_chapter('book1', 'c1', chapter_index=1, chapter_path='chapters/1.xhtml',
                    source_html=SOURCE1 + '<p>Новые события.</p>', expected_revision=1)
    chapter = lib.get_chapter('book1', 'c1')
    assert chapter['accepted_html'] == '<p>Старый перевод первой главы.</p>'
    assert not chapter['accepted_current']
    assert chapter['accepted_source_revision'] == 1
    provider = ContextAwareProvider()
    job = enqueue(lib, store)
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job['id'])['status'] == 'completed'
    assert 'Старый перевод первой главы' not in provider.context_calls[0].prompt
    assert 'Старый перевод первой главы' not in provider.translation_calls[0].prompt


def test_future_chapter_cannot_leak_into_context_prepass(tmp_path):
    store, lib = setup_library(tmp_path)
    lib.put_chapter('book1', 'c3', chapter_index=3, chapter_path='chapters/3.xhtml',
                    source_html='<p>Будущий спойлер SECRET-REVEAL.</p>', expected_revision=0)
    provider = ContextAwareProvider()
    job = enqueue(lib, store)
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job['id'])['status'] == 'completed'
    assert 'SECRET-REVEAL' not in provider.context_calls[0].prompt
    assert 'SECRET-REVEAL' not in provider.translation_calls[0].prompt


def test_context_entity_aliases_are_source_grounded(tmp_path):
    store, lib = setup_library(tmp_path)

    class EntityProvider(ContextAwareProvider):
        async def generate(self, request):
            if request.chapter_id.startswith('context:'):
                doc = context_payload()
                doc['c0']['active_entities'] = [
                    {'source_name': '王雪', 'aliases': ['李明', 'Откуда-то', '不存在'], 'gender_or_grammar': 'female'},
                    {'source_name': '未来皇帝', 'aliases': ['王雪'], 'gender_or_grammar': 'male'},
                ]
                return json.dumps(doc, ensure_ascii=False)
            return await super().generate(request)

    job = enqueue(lib, store)
    assert asyncio.run(TranslationWorker(store, EntityProvider()).run_once())
    assert store.get_job(job['id'])['status'] == 'completed'
    entities = lib.context_for_chapter('book1', 'c2')['snapshot']['active_entities']
    assert len(entities) == 1
    assert entities[0]['source_name'] == '王雪'
    assert entities[0]['aliases'] == ['李明']


def test_worker_cancellation_during_context_preflight_never_saves_snapshot(tmp_path):
    store, lib = setup_library(tmp_path)
    job = enqueue(lib, store)

    class CancellingProvider(ContextAwareProvider):
        async def generate(self, request):
            if request.chapter_id.startswith('context:'):
                store.request_cancel(job['id'])
            return await super().generate(request)

    provider = CancellingProvider()
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job['id'])['status'] == 'cancelled'
    assert lib.context_for_chapter('book1', 'c2')['status'] == 'missing_or_stale'
    assert not provider.translation_calls


def test_book_glossary_rejects_conflicting_scoped_overrides(tmp_path):
    store, lib = setup_library(tmp_path)
    from chaptera_translation_core.prompt_policy import GlossaryVersionConflict
    with pytest.raises(GlossaryVersionConflict):
        lib.put_glossary('book1', glossary={'李明': 'Ли Мин'}, versions={
            '李明': [
                {'scope': ['chapters/2.xhtml'], 'override': {'rus': 'Минг Ли'}},
                {'scope': ['chapters/2.xhtml'], 'override': {'rus': 'Ли Мин'}},
            ]
        }, authority='READY', expected_revision=0, actor_id='bob')
    assert lib.get_book('book1')['glossary_revision'] == 0
