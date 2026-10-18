"""Durable actor/global crawler quotas, restart and explicit cleanup gates."""
from __future__ import annotations

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_collect import catalog_from_html
from chaptera_server.source_job_store import SourceJobStore, SourceJobConflict
from chaptera_server.storage import QueueStore

import asyncio
import httpx
import pytest

BOOK = "https://www.royalroad.com/fiction/142007/aura"
TOC = '<div id="chapters">' + ''.join(
    f'<a href="/fiction/142007/aura/chapter/{90000+n}/{n}-part">{n} - Part {n}</a>'
    for n in (1, 2)
) + '</div>'
ONE_MIB = 1024 * 1024


def setup(tmp_path):
    repo = QueueStore(tmp_path / 'quotas.sqlite3')
    jobs = SourceJobStore(
        repo, actor_quota_bytes=ONE_MIB, global_quota_bytes=2 * ONE_MIB
    )
    return repo, jobs, catalog_from_html('royalroad', BOOK, TOC)


def put(jobs, actor, catalog, text, *, key):
    job = jobs.create(actor, 'cat-1', catalog, [1], idempotency_key=key)
    claim = jobs.claim()
    assert claim and claim['job_id'] == job['job_id']
    return jobs.finish(job['job_id'], claim['lease_token'], body=text)


def test_actor_quota_enforced_across_completed_jobs_and_retry_after_cleanup(tmp_path):
    repo, jobs, cat = setup(tmp_path)
    first = jobs.create('alice', 'cat-1', cat, [1], idempotency_key='first')
    claim = jobs.claim()
    jobs.finish(first['job_id'], claim['lease_token'], body='A' * (768 * 1024))
    second = jobs.create('alice', 'cat-1', cat, [1], idempotency_key='second')
    claim = jobs.claim()
    failed = jobs.finish(second['job_id'], claim['lease_token'], body='B' * (400 * 1024))
    assert failed['status'] == 'partial'
    assert failed['completed'] == 0
    assert failed['failed'] == 1
    assert 'Member' in failed['failures'][0]['error']
    assert jobs.usage('alice')['actor_used_bytes'] == 768 * 1024
    assert jobs.usage('alice')['effective_remaining_bytes'] == 256 * 1024
    assert jobs.get('alice', first['job_id'])['status'] == 'complete'
    assert jobs.delete_finished('alice',first['job_id'])['freed_source_bytes'] == 768 * 1024
    assert jobs.usage('alice')['actor_used_bytes'] == 0
    retry = jobs.retry_failures('alice',second['job_id'])
    assert retry['status'] == 'queued'
    claim = jobs.claim()
    done = jobs.finish(second['job_id'],claim['lease_token'],body='B' * (400 * 1024))
    assert done['status'] == 'complete'
    restarted = SourceJobStore(
        QueueStore(repo.db_path),
        actor_quota_bytes=ONE_MIB,global_quota_bytes=2*ONE_MIB,
    )
    assert restarted.usage('alice')['actor_used_bytes'] == 400*1024
    assert restarted.get('alice',second['job_id'])['completed'] == 1


def test_shared_quota_stops_other_members_without_disclosing_usage(tmp_path):
    repo, jobs, catalog = setup(tmp_path)
    put(jobs,'alice',catalog,'A'*(800*1024),key='alice-book')
    put(jobs,'bob',catalog,'B'*(800*1024),key='bob-book')
    third = put(jobs,'charlie',catalog,'C'*(600*1024),key='charlie-book')
    assert third['status'] == 'partial' and third['failed'] == 1
    assert 'Shared' in third['failures'][0]['error']
    own=jobs.usage('charlie')
    assert own['actor_used_bytes'] == 0
    assert own['effective_remaining_bytes'] == 2*ONE_MIB-1600*1024
    assert 'global_used_bytes' not in own
    assert own['scope']=='verified_source_bodies_only'
    assert jobs.usage('alice')['actor_used_bytes'] == 800*1024
    assert jobs.usage('bob')['actor_used_bytes'] == 800*1024
    # A restored worker sees the same cross-member accounting.
    reopened=SourceJobStore(QueueStore(repo.db_path),
        actor_quota_bytes=ONE_MIB,global_quota_bytes=2*ONE_MIB)
    assert reopened.usage('charlie') == own


def test_full_quota_refuses_new_jobs_but_preserves_existing_sources(tmp_path):
    repo, jobs, catalog = setup(tmp_path)
    completed = jobs.create('alice','cat-1',catalog,[1],idempotency_key='first')
    first=jobs.claim()
    jobs.finish(first['job_id'],first['lease_token'],body='A'*ONE_MIB)
    assert jobs.usage('alice')['can_collect'] is False
    with pytest.raises(SourceJobConflict,match='quota exhausted'):
        jobs.create('alice','cat-2',catalog,[1],idempotency_key='blocked')
    assert jobs.get('alice',completed['job_id'])['completed']==1
    assert jobs.delete_finished('alice',completed['job_id'])['deleted']
    assert jobs.usage('alice')['can_collect'] is True


def test_api_uses_same_configured_budgets_and_exposes_only_member_quota(tmp_path):
    repo,_jobs,catalog=setup(tmp_path)
    app=create_app(Settings(db_path=repo.db_path,
        team_tokens={'alice':'alice-key','bob':'bob-key'},
        source_actor_quota_mib=1,source_global_quota_mib=2), repo)
    assert app.state.source_job_store.actor_quota_bytes==ONE_MIB
    job=app.state.source_job_store.create('alice','cat-1',catalog,[1])
    claim=app.state.source_job_store.claim()
    app.state.source_job_store.finish(
        job['job_id'],claim['lease_token'],body='A'*(600*1024),
    )

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url='http://test',
        ) as client:
            assert (await client.get('/v1/source-books/jobs')).status_code==401
            a=await client.get('/v1/source-books/jobs',headers={'X-API-Key':'alice-key'})
            b=await client.get('/v1/source-books/jobs',headers={'X-API-Key':'bob-key'})
            assert a.status_code==b.status_code==200
            assert len(a.json()['jobs'])==1 and not b.json()['jobs']
            assert a.json()['usage']['actor_used_bytes']==600*1024
            assert b.json()['usage']['actor_used_bytes']==0
            for response in (a,b):
                assert 'global_used_bytes' not in response.text
                assert response.json()['usage']['actor_limit_bytes']==ONE_MIB
    asyncio.run(check())


def test_configuration_validates_budgets_and_environment(monkeypatch,tmp_path):
    for actor,global_limit in [(0,1),(2,1),(-1,10),(1,0),(1,1_048_577)]:
        with pytest.raises(ValueError,match='Source'):
            Settings(db_path=tmp_path/'q.db',
                source_actor_quota_mib=actor,source_global_quota_mib=global_limit)
    monkeypatch.setenv('CHAPTERA_SOURCE_ACTOR_QUOTA_MIB','16')
    monkeypatch.setenv('CHAPTERA_SOURCE_GLOBAL_QUOTA_MIB','64')
    s=Settings.from_env()
    assert s.source_actor_quota_mib==16 and s.source_global_quota_mib==64
    with pytest.raises(ValueError):
        SourceJobStore(QueueStore(tmp_path/'q2.db'),
            actor_quota_bytes=3*ONE_MIB,global_quota_bytes=2*ONE_MIB)
