"""Fail-closed source-window context regression and revision-fencing tests."""
from __future__ import annotations

import asyncio
import json
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import chaptera_server.storage as storage
import chaptera_server.quota as quota
from chaptera_server.quota import DailyQuotaLedger, QuotaControlledProvider, pacific_window

from chaptera_server.context_prepass import (
    CONTEXT_WINDOW_VISIBLE_CHARS, MAX_CONTEXT_WINDOWS,
    ContextPrepassRejected, ContextPrepassUnserviceable, source_windows,
)
from chaptera_server.library import BookLibrary
from chaptera_server.storage import QueueStore
from chaptera_server.worker import TranslationWorker
from chaptera_translation_core.chapter_context import context_is_current


def _payload(*, terms=None, name='', aliases=None):
    return {'c0': {
        'summary': 'Повествование продолжается', 'pov': 'третье лицо',
        'tone_and_register': 'нейтральный', 'setting': [],
        'active_entities': ([{'source_name': name, 'aliases': aliases or []}] if name else []),
        'relationships': [], 'important_events': [], 'continuity_in': [],
        'continuity_out': [], 'ambiguous_references': [],
        'reveal_constraints': [], 'semantic_terms': terms or [],
    }}


class WindowProvider:
    def __init__(self, *, fail_window=None, hook=None):
        self.context_prompts = []
        self.translation_calls = 0
        self.fail_window = fail_window
        self.hook = hook

    async def generate(self, request):
        if request.chapter_id.startswith('context:'):
            self.context_prompts.append(request.prompt)
            count = len(self.context_prompts)
            if self.hook:
                self.hook(count)
            if self.fail_window == count:
                return '{"c0":{"summary":"incomplete"}}'
            docs = json.loads(request.prompt.split('ORDERED SOURCE DOCUMENTS JSON:\n', 1)[1])
            assert len(docs) == 1
            name = '王雪' if '王雪' in docs[0]['source'] else ''
            candidate = 'Ван Сюэ' if count % 2 else 'Ван Снежная'
            return json.dumps(_payload(
                name=name,
                aliases=['不存在', '王雪'] if name else [],
                terms=[{'source': '王雪', 'suggested_ru': candidate, 'kind': 'person'}] if name else [],
            ), ensure_ascii=False)
        self.translation_calls += 1
        return request.source_html_with_markers.replace('王雪', 'Ван Сюэ').replace('世界', 'Мир').replace('未来秘密', 'Секрет будущего')


def _create(tmp_path, source):
    store = QueueStore(tmp_path / 'windows.sqlite3')
    lib = BookLibrary(store)
    lib.create_book('book', 'Book', owner_member_id='test-moderator')
    lib.put_chapter('book', 'one', chapter_index=1, chapter_path='one.xhtml',
                    source_html=source, expected_revision=0)
    spec = lib.new_job('book', 'one', source_language='Chinese', target_language='Russian',
                       style_instructions='', max_attempts=3, transport='html_v0')
    return store, lib, store.create_job('alice', spec)


def test_window_planner_exact_span_coverage_for_mixed_chinese_japanese():
    text = ('王雪说：世界。彼は微笑んだ！' * 1800) + '終章'
    spans = source_windows(text)
    assert len(spans) > 1
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert ''.join(text for _, _, text in spans) == text
    assert all(stop - start <= CONTEXT_WINDOW_VISIBLE_CHARS for start, stop, _ in spans)
    assert all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1))


def test_fail_closed_if_source_exceeds_max_windows_before_model_request(tmp_path):
    source = '<p>' + '世界' * (MAX_CONTEXT_WINDOWS * CONTEXT_WINDOW_VISIBLE_CHARS // 2 + 5) + '</p>'
    store, lib, job = _create(tmp_path, source)
    provider = WindowProvider()
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job['id'])['status'] == 'failed'
    assert store.get_job(job['id'])['translated_html'] is None
    assert lib.context_for_chapter('book', 'one')['snapshot'] is None
    assert not provider.context_prompts and not provider.translation_calls
    assert any(e['stage'] == 'context_unserviceable' for e in store.events(job['id']))


def test_partial_window_error_never_saves_snapshot_or_calls_translation(tmp_path):
    source = '<p>' + ('世界。王雪！' * 3500) + '</p>'
    store, lib, job = _create(tmp_path, source)
    provider = WindowProvider(fail_window=2)
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job['id'])['status'] == 'retry_wait'
    assert store.get_job(job['id'])['translated_html'] is None
    assert lib.context_for_chapter('book', 'one')['snapshot'] is None
    assert len(provider.context_prompts) == 2
    assert provider.translation_calls == 0


def test_window_scope_excludes_future_glossary_and_aliases(tmp_path):
    source = '<p>' + ('王雪世界。' * 2500) + ('未来秘密。' * 2200) + '</p>'
    store, lib, job = _create(tmp_path, source)
    lib.put_glossary('book', glossary={'未来秘密': {'rus': 'Секрет будущего'}},
                     versions={}, authority='READY', expected_revision=0, actor_id='alice')
    # Original job is intentionally stale after the glossary edit.
    spec = lib.new_job('book', 'one', source_language='Chinese', target_language='Russian',
                       style_instructions='', max_attempts=3, transport='html_v0')
    job2 = store.create_job('bob', spec)
    provider = WindowProvider()
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job['id'])['status'] == 'failed'
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job2['id'])['status'] == 'completed'
    assert len(provider.context_prompts) >= 2
    assert 'Секрет будущего' not in provider.context_prompts[0]
    snap = lib.context_for_chapter('book', 'one')['snapshot']
    assert snap is not None
    assert all('不存在' not in str(entity.get('aliases'))
               for entity in snap['active_entities'])
    # Contradictory auto-suggested names across windows are excluded.
    assert snap['semantic_terms'] == []


def test_window_coverage_audit_is_fingerprinted_and_stale_guarded(tmp_path):
    source = '<p>' + '世界。' * 5700 + '</p>'
    store, lib, job = _create(tmp_path, source)
    provider = WindowProvider()
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert lib.context_for_chapter('book', 'one')['status'] == 'current'
    snap = lib.context_for_chapter('book', 'one')['snapshot']
    tampered = json.loads(json.dumps(snap))
    tampered['window_coverage']['windows'][0]['end'] -= 1
    assert not context_is_current(tampered, source_hash=snap['source_hash'],
                                  glossary_hash=snap['glossary_fingerprint'],
                                  predecessor=snap['predecessor'])
    assert context_is_current(snap, source_hash=snap['source_hash'],
                              glossary_hash=snap['glossary_fingerprint'],
                              predecessor=snap['predecessor'])
    assert provider.translation_calls == 1


def test_edit_during_second_window_prevents_cache_and_final_translation(tmp_path):
    source = '<p>' + '世界。' * 13000 + '</p>'
    store, lib, job = _create(tmp_path, source)
    def mutate(window_number):
        if window_number == 2:
            lib.put_chapter('book', 'one', chapter_index=1, chapter_path='one.xhtml',
                            source_html=source + '<p>加笔。</p>', expected_revision=1)
    provider = WindowProvider(hook=mutate)
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job['id'])['status'] == 'failed'
    assert not provider.translation_calls
    assert lib.context_for_chapter('book', 'one')['snapshot'] is None


def test_window_coverage_rebuilds_when_glossary_changes(tmp_path):
    source = '<p>' + '世界。' * 4200 + '</p>'
    store, lib, job = _create(tmp_path, source)
    provider = WindowProvider()
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    first = lib.context_for_chapter('book', 'one')['snapshot']
    lib.put_glossary('book', glossary={'世界': 'Мир'}, versions={},
                     authority='READY', expected_revision=0, actor_id='bob')
    assert lib.context_for_chapter('book', 'one')['status'] == 'missing_or_stale'
    assert first['window_coverage']['visible_chars'] == len('世界。' * 4200)


def test_window_cache_survives_process_restart_and_reuses_all_windows(tmp_path):
    source = '<p>' + '世界。' * 7100 + '</p>'
    store, lib, job = _create(tmp_path, source)
    provider = WindowProvider()
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    first_requests = len(provider.context_prompts)
    assert first_requests >= 2
    spec = lib.new_job('book', 'one', source_language='Chinese', target_language='Russian',
                       style_instructions='', max_attempts=3, transport='html_v0')
    fresh = store.create_job('bob', spec)
    assert asyncio.run(TranslationWorker(QueueStore(store.db_path), provider).run_once())
    assert store.get_job(fresh['id'])['status'] == 'completed'
    assert len(provider.context_prompts) == first_requests
    assert any(e['stage'] == 'context_cache_hit' for e in store.events(fresh['id']))


def test_previous_prompt_version_is_invalidated_without_deleting_snapshot(tmp_path):
    source = '<p>世界。</p>'
    store, lib, job = _create(tmp_path, source)
    assert asyncio.run(TranslationWorker(store, WindowProvider()).run_once())
    snap = lib.context_for_chapter('book', 'one')['snapshot']
    assert snap['prompt_version'] == 6
    old = dict(snap)
    old['prompt_version'] = 5
    assert not context_is_current(old, source_hash=snap['source_hash'],
                                  glossary_hash=snap['glossary_fingerprint'],
                                  predecessor=snap['predecessor'])


def test_window_chunking_without_any_punctuation_preserves_every_character():
    text = '未来王雪' * 11_000
    chunks = source_windows(text)
    assert len(chunks) > 1
    assert ''.join(c for _, _, c in chunks) == text
    assert all(len(c) <= CONTEXT_WINDOW_VISIBLE_CHARS for _, _, c in chunks)


def test_cancellation_stops_new_context_window_requests(tmp_path):
    source = '<p>' + '世界。' * 13_000 + '</p>'
    store, lib, job = _create(tmp_path, source)
    def cancel_on_first_window(window_number):
        if window_number == 1:
            store.request_cancel(job['id'])
    provider = WindowProvider(hook=cancel_on_first_window)
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    assert store.get_job(job['id'])['status'] == 'cancelled'
    assert len(provider.context_prompts) == 1
    assert not provider.translation_calls
    assert lib.context_for_chapter('book', 'one')['snapshot'] is None


def test_one_rpd_per_day_preserves_every_valid_context_window(tmp_path, monkeypatch):
    """The owner can translate a long chapter over many quota reset days."""
    clock = [datetime(2026, 10, 17, 12, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr(storage, 'time', SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(quota, 'time', SimpleNamespace(time=lambda: clock[0]))
    source = '<p>' + ('世界。' * 7100) + '</p>'
    store, library, job = _create(tmp_path, source)
    remote = WindowProvider()
    days_waiting = 0

    for day in range(8):
        restarted = QueueStore(store.db_path)
        provider = QuotaControlledProvider(remote, DailyQuotaLedger(
            restarted, 'owner-google-project', 'same-gemini-model', 1,
        ))
        assert asyncio.run(TranslationWorker(restarted, provider).run_once())
        state = restarted.get_job(job['id'])
        if state['status'] == 'completed':
            assert state['translated_html'] == source.replace('世界', 'Мир')
            break
        assert state['status'] == 'retry_wait'
        assert state['attempts'] == 0
        with restarted._connect() as con:
            row = con.execute(
                'SELECT quota_partial_waits FROM jobs WHERE id=?', (job['id'],)
            ).fetchone()
            assert row['quota_partial_waits'] == 0
        days_waiting += 1
        clock[0] = pacific_window(clock[0])[1] + 1
    else:
        raise AssertionError('Multi-day chapter never completed')

    assert days_waiting >= 2
    assert len(remote.context_prompts) == 2
    assert remote.translation_calls == 1
    events = store.events(job['id'])
    assert any(e['stage'] == 'context_window_checkpoint_saved' for e in events)
    assert any(e['stage'] == 'context_window_checkpoint_hit' for e in events)
    with store._connect() as con:
        # Duplicate private model text is dropped on terminal completion.
        assert con.execute(
            'SELECT COUNT(*) FROM atom_job_checkpoints WHERE job_id=?', (job['id'],)
        ).fetchone()[0] == 0


def test_valid_context_window_not_regenerated_after_bad_second_window(
    tmp_path, monkeypatch,
):
    clock = [1000.0]
    monkeypatch.setattr(storage, 'time', SimpleNamespace(time=lambda: clock[0]))
    source = '<p>' + ('世界。王雪！' * 3500) + '</p>'
    store, lib, job = _create(tmp_path, source)
    first = WindowProvider(fail_window=2)
    assert asyncio.run(TranslationWorker(store, first).run_once())
    assert store.get_job(job['id'])['status'] == 'retry_wait'
    assert lib.context_for_chapter('book', 'one')['snapshot'] is None
    with store._connect() as con:
        assert con.execute(
            'SELECT COUNT(*) FROM atom_job_checkpoints WHERE job_id=?', (job['id'],)
        ).fetchone()[0] == 1

    clock[0] += 4
    second = WindowProvider()
    assert asyncio.run(TranslationWorker(QueueStore(store.db_path), second).run_once())
    assert store.get_job(job['id'])['status'] == 'completed'
    assert len(first.context_prompts) == 2
    assert len(second.context_prompts) == 1
    assert second.translation_calls == 1
    assert lib.context_for_chapter('book', 'one')['status'] == 'current'
    assert any(e['stage'] == 'context_window_checkpoint_hit' for e in store.events(job['id']))


def test_tampered_but_rehashed_context_cache_cannot_bypass_validation(
    tmp_path, monkeypatch,
):
    clock = [1000.0]
    monkeypatch.setattr(storage, 'time', SimpleNamespace(time=lambda: clock[0]))
    source = '<p>' + ('世界。王雪！' * 3500) + '</p>'
    store, lib, job = _create(tmp_path, source)
    assert asyncio.run(TranslationWorker(store, WindowProvider(fail_window=2)).run_once())
    # Deliberately recompute checksum: integrity and semantic validation
    # are independent protections against forged context.
    bad = '{"c0":{"summary":"forged"}}'
    with store._connect() as con:
        con.execute(
            '''UPDATE atom_job_checkpoints SET response_text=?, response_sha256=?
               WHERE job_id=?''',
            (bad, hashlib.sha256(bad.encode()).hexdigest(), job['id']),
        )
    clock[0] += 4
    resumed = WindowProvider()
    assert asyncio.run(TranslationWorker(QueueStore(store.db_path), resumed).run_once())
    assert store.get_job(job['id'])['status'] == 'retry_wait'
    assert resumed.context_prompts == []
    assert resumed.translation_calls == 0
    assert lib.context_for_chapter('book', 'one')['snapshot'] is None
