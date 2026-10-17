"""RE-06A offline cost-safety: only validated, fenced successes replay."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import chaptera_server.quota as quota
import chaptera_server.storage as storage
from chaptera_server.library import BookLibrary
from chaptera_server.quota import DailyQuotaLedger, QuotaControlledProvider, pacific_window
from chaptera_server.storage import JobSpec, QueueStore
from chaptera_server.subrequest_checkpoints import (
    CheckpointIntegrityError, SubrequestCheckpoints,
)
from chaptera_server.worker import TranslationWorker
from chaptera_translation_core.ports import ProviderRequest, TranslationCancelled


def at_utc(year, month, day):
    return datetime(year, month, day, 12, tzinfo=timezone.utc).timestamp()


def clock_for_quota(monkeypatch):
    clock = [at_utc(2026, 10, 17)]
    monkeypatch.setattr(quota, 'time', SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(storage, 'time', SimpleNamespace(time=lambda: clock[0]))
    return clock


class MultiWindowProvider:
    model = 'offline-window-v1'

    def __init__(self):
        self.context_calls = 0
        self.translation_calls = 0

    async def generate(self, request):
        if request.chapter_id.startswith('context:'):
            self.context_calls += 1
            documents = json.loads(request.prompt.split('ORDERED SOURCE DOCUMENTS JSON:\n', 1)[1])
            assert len(documents) == 1
            return json.dumps({'c0': {
                'summary': 'Источник описывает развитие событий.',
                'pov': '', 'tone_and_register': '', 'setting': [],
                'active_entities': [], 'relationships': [], 'important_events': [],
                'continuity_in': [], 'continuity_out': [],
                'ambiguous_references': [], 'reveal_constraints': [],
                'semantic_terms': [],
            }}, ensure_ascii=False)
        self.translation_calls += 1
        return request.source_html_with_markers.replace('世界', 'Мир')


def make_book_job(store, source, *, max_attempts=1):
    library = BookLibrary(store)
    library.create_book('book', 'Checkpointed book', owner_member_id='moderator')
    library.put_chapter('book', 'one', chapter_index=1, chapter_path='one.xhtml',
                        source_html=source, expected_revision=0)
    spec = library.new_job('book', 'one', source_language='Chinese',
                           target_language='Russian', style_instructions='',
                           max_attempts=max_attempts)
    return library, store.create_job('moderator', spec)


def count_rows(store, job_id):
    with store._connect() as con:
        return con.execute(
            'SELECT COUNT(*) FROM job_subrequest_checkpoints WHERE job_id=?',
            (job_id,),
        ).fetchone()[0]


def test_context_windows_resume_across_two_pacific_midnights_without_repaying(
    tmp_path, monkeypatch,
):
    clock = clock_for_quota(monkeypatch)
    store = QueueStore(tmp_path / 'restart.sqlite3')
    source = '<p>' + '世界。' * 4500 + '</p>'  # Two bounded context windows.
    library, job = make_book_job(store, source)
    remote = MultiWindowProvider()

    def worker():
        current = QueueStore(store.db_path)
        limiter = DailyQuotaLedger(current, 'project-x', remote.model, 1)
        return TranslationWorker(current, QuotaControlledProvider(remote, limiter))

    assert asyncio.run(worker().run_once())  # window 1, then quota 0
    assert store.get_job(job['id'])['status'] == 'retry_wait'
    assert remote.context_calls == 1 and remote.translation_calls == 0
    assert count_rows(store, job['id']) == 1
    with store._connect() as con:
        assert con.execute('SELECT quota_partial_waits FROM jobs WHERE id=?',
                           (job['id'],)).fetchone()[0] == 0

    clock[0] = pacific_window(clock[0])[1] + 1
    assert asyncio.run(worker().run_once())  # cached first; pay window 2
    assert store.get_job(job['id'])['status'] == 'retry_wait'
    assert remote.context_calls == 2 and remote.translation_calls == 0
    assert count_rows(store, job['id']) == 2
    assert library.context_for_chapter('book', 'one')['status'] == 'current'

    clock[0] = pacific_window(clock[0])[1] + 1
    assert asyncio.run(worker().run_once())  # complete HTML in third day
    final = store.get_job(job['id'])
    assert final['status'] == 'completed'
    assert remote.context_calls == 2 and remote.translation_calls == 1
    assert count_rows(store, job['id']) == 3
    assert 'Мир' in final['translated_html']
    assert any(e['stage'] == 'subrequest_reused' for e in store.events(job['id']))


def atom_response(request):
    packet = json.loads(request.source_html_with_markers)
    fields = packet['columns']
    rows = [dict(zip(fields, row)) for row in packet['rows']]
    segments = []
    for row in rows:
        domain = row['render_domain_id']
        translated = row['source_text'].replace('世界', 'Мир')
        if segments and segments[-1][0] == domain:
            segments[-1][1]['covers'].append(row['atom_id'])
            segments[-1][1]['translation'] += translated
        else:
            segments.append((domain, {
                'covers': [row['atom_id']], 'translation': translated,
            }))
    return json.dumps({'segments': [value for _, value in segments]}, ensure_ascii=False)


class AtomProvider:
    model = 'offline-atom-v1'

    def __init__(self):
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        return atom_response(request)


def test_atom_batches_resume_and_keep_full_source_owned_provenance(tmp_path, monkeypatch):
    clock = clock_for_quota(monkeypatch)
    store = QueueStore(tmp_path / 'atom-restart.sqlite3')
    source = '<body>' + '<p>世界。</p>' * 30 + '</body>'
    job = store.create_job('moderator', JobSpec(
        '30', source, transport='atoms_v1', max_attempts=1))
    remote = AtomProvider()

    def worker():
        current = QueueStore(store.db_path)
        limiter = DailyQuotaLedger(current, 'project-y', remote.model, 1)
        return TranslationWorker(current, QuotaControlledProvider(remote, limiter))

    assert asyncio.run(worker().run_once())
    assert store.get_job(job['id'])['status'] == 'retry_wait'
    assert remote.calls == 1
    assert count_rows(store, job['id']) == 1
    clock[0] = pacific_window(clock[0])[1] + 1
    assert asyncio.run(worker().run_once())
    result = store.get_job(job['id'])
    assert result['status'] == 'completed'
    assert remote.calls == 2
    assert count_rows(store, job['id']) == 2
    assert len(result['atom_provenance']) == 30
    assert all('target_hash' in p for p in result['atom_provenance'])
    assert 'Мир' in result['translated_html']


def test_stale_source_job_cannot_reuse_saved_preflight_for_new_revision(
    tmp_path, monkeypatch,
):
    clock = clock_for_quota(monkeypatch)
    store = QueueStore(tmp_path / 'stale.sqlite3')
    library, old = make_book_job(store, '<p>世界。</p>')
    remote = MultiWindowProvider()

    def worker():
        current = QueueStore(store.db_path)
        return TranslationWorker(current, QuotaControlledProvider(
            remote, DailyQuotaLedger(current, 'project-z', remote.model, 1),
        ))

    assert asyncio.run(worker().run_once())
    assert store.get_job(old['id'])['status'] == 'retry_wait'
    assert count_rows(store, old['id']) == 1
    library.put_chapter('book', 'one', chapter_index=1, chapter_path='one.xhtml',
                        source_html='<p>全新的世界。</p>', expected_revision=1)
    replacement = store.create_job('moderator', library.new_job(
        'book', 'one', source_language='Chinese', target_language='Russian',
        style_instructions='', max_attempts=1,
    ))
    clock[0] = pacific_window(clock[0])[1] + 1
    assert asyncio.run(worker().run_once())
    assert store.get_job(old['id'])['status'] == 'failed'
    assert remote.context_calls == 1
    assert asyncio.run(worker().run_once())
    assert count_rows(store, replacement['id']) == 1
    assert remote.context_calls == 2
    assert count_rows(store, old['id']) == 1


def test_checkpoint_rejects_tampering_scope_change_and_lost_lease(tmp_path):
    store = QueueStore(tmp_path / 'checkpoint.sqlite3')
    item = store.create_job('moderator', JobSpec(
        'chapter', '<p>世界。</p>', max_attempts=2))
    job, lease = store.claim_next(lease_seconds=2)
    scope = 'model:gemini-test-v1'
    session = SubrequestCheckpoints(
        store, job['id'], lease, job['request_hash'], scope)
    request = ProviderRequest('chapter', 'full prompt', 'payload', 1, 'atoms_v1')
    session.save('atom_packet:1', request, '{"segments":[]}')
    session.save('atom_packet:1', request, '{"segments":[]}')
    assert session.saved_calls == 1
    assert session.load('atom_packet:1', request) == '{"segments":[]}'
    assert SubrequestCheckpoints(
        QueueStore(store.db_path), job['id'], lease, job['request_hash'],
        'model:gemini-test-v2',
    ).load('atom_packet:1', request) is None
    assert session.load('atom_packet:1', ProviderRequest(
        'chapter', 'changed prompt', 'payload', 1, 'atoms_v1')) is None
    with store._connect() as con:
        con.execute("UPDATE job_subrequest_checkpoints SET response_text='tampered' "
                    "WHERE job_id=?", (job['id'],))
    with pytest.raises(CheckpointIntegrityError):
        session.load('atom_packet:1', request)
    with store._connect() as con:
        con.execute("UPDATE jobs SET lease_owner='other' WHERE id=?", (job['id'],))
    with pytest.raises(TranslationCancelled):
        session.load('atom_packet:1', request)


def test_invalid_atom_response_is_never_saved(tmp_path):
    store = QueueStore(tmp_path / 'invalid.sqlite3')
    job = store.create_job('moderator', JobSpec('one', '<p>世界。</p>', transport='atoms_v1'))

    class InvalidAtom:
        async def generate(self, _request):
            return '{"segments":[{"covers":["unknown-id"],"translation":"bad"}]}'

    assert asyncio.run(TranslationWorker(store, InvalidAtom()).run_once())
    assert store.get_job(job['id'])['status'] == 'failed'
    assert count_rows(store, job['id']) == 0
