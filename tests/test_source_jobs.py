"""Durable source crawler tests: crash/lease, retry, auth, chunk staging."""
from __future__ import annotations

import asyncio
import sqlite3
import time

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_collect import catalog_from_html
from chaptera_server.source_job_store import SourceJobStore, SourceJobConflict, SourceJobMissing
from chaptera_server.source_job_worker import SourceCollectionWorker
from chaptera_server.storage import QueueStore

BOOK="https://www.royalroad.com/fiction/142007/aura"
TOC='<div id="chapters">'+''.join(
    f'<a href="/fiction/142007/aura/chapter/{60000+n}/{n}-part"> {n} - Part {n} </a>'
    for n in range(1,4)
)+'</div>'
TEXT={n:('<div class="chapter-content"><p>'+f"Chapter {n} text is public. "*20+'</p></div>')
      for n in range(1,4)}


class FakeSite:
    def __init__(self, *, blocked=None):
        self.blocked=set(blocked or [])
        self.fetches=[]

    async def fetch(self, source, url):
        assert source=="royalroad"
        self.fetches.append(url)
        if url==BOOK:
            return TOC
        for n in (1,2,3):
            if f"/{60000+n}/" in url:
                if n in self.blocked:
                    return '<div class="chapter-content">Log in to read</div>'
                return TEXT[n]
        raise AssertionError("Unexpected source URL: "+url)


def make_store(tmp_path,name="crawler.sqlite3"):
    queue=QueueStore(tmp_path/name)
    jobs=SourceJobStore(queue)
    return queue,jobs


def catalog():
    return catalog_from_html("royalroad",BOOK,TOC)


def test_job_checkpoint_survives_restart_without_redownloading_completed(tmp_path):
    store,jobs=make_store(tmp_path)
    created=jobs.create("alice","cat1",catalog(),[1,2,3],idempotency_key="book-a")
    assert created["total"]==3 and created["status"]=="queued"
    assert jobs.create("alice","cat1",catalog(),[1,2,3],idempotency_key="book-a")["job_id"]==created["job_id"]
    with pytest.raises(SourceJobConflict,match="Idempotency"):
        jobs.create("alice","cat1",catalog(),[1,2],idempotency_key="book-a")
    first=jobs.claim()
    assert first["chapter"]["ordinal"]==1
    jobs.finish(first["job_id"],first["lease_token"],body="First completed source paragraph. "*10)
    assert jobs.get("alice",created["job_id"])["completed"]==1

    restarted=SourceJobStore(QueueStore(store.db_path))
    source=FakeSite()
    worker=SourceCollectionWorker(restarted,source)
    async def resume():
        assert await worker.run_once()
        assert await worker.run_once()
        assert not await worker.run_once()
    asyncio.run(resume())
    done=restarted.get("alice",created["job_id"])
    assert (done["status"],done["completed"],done["failed"],done["pending"])==("complete",3,0,0)
    assert len(source.fetches)==2
    assert not any("/60001/" in url for url in source.fetches)
    fragment=restarted.materialize("alice",created["job_id"],[1,2])
    assert fragment.complete
    assert fragment.as_import_html().count(b"<h1>")==2
    assert b"First completed source paragraph." in fragment.as_import_html()


def test_lease_fencing_aborts_stale_worker_and_cancellation(tmp_path):
    store,jobs=make_store(tmp_path)
    job=jobs.create("alice","cat1",catalog(),[1,2])
    first=jobs.claim(lease_seconds=30)
    assert first["chapter"]["ordinal"]==1
    with store._connect() as con:
        con.execute("UPDATE source_collect_jobs SET lease_expires_at=? WHERE id=?",
                    (time.time()-1,job["job_id"]))
    second=jobs.claim(lease_seconds=30)
    assert second["chapter"]["ordinal"]==1
    assert first["lease_token"]!=second["lease_token"]
    with pytest.raises(SourceJobConflict,match="Stale"):
        jobs.finish(job["job_id"],first["lease_token"],body="X"*150)
    state=jobs.finish(job["job_id"],second["lease_token"],body="Y"*150)
    assert state["completed"]==1
    third=jobs.claim()
    assert third["chapter"]["ordinal"]==2
    jobs.cancel("alice",job["job_id"])
    with pytest.raises(SourceJobConflict,match="Stale"):
        jobs.finish(job["job_id"],third["lease_token"],body="X"*150)
    assert jobs.get("alice",job["job_id"])["status"]=="cancelled"
    with pytest.raises(SourceJobMissing):
        jobs.get("bob",job["job_id"])


def test_failed_chapter_retry_keeps_verified_work(tmp_path):
    _,jobs=make_store(tmp_path)
    job=jobs.create("alice","cat1",catalog(),[1,2,3])
    source=FakeSite(blocked={2})
    worker=SourceCollectionWorker(jobs,source)
    async def first_pass():
        for _ in range(3):
            assert await worker.run_once()
    asyncio.run(first_pass())
    partial=jobs.get("alice",job["job_id"])
    assert partial["status"]=="partial"
    assert (partial["completed"],partial["failed"])==(2,1)
    with pytest.raises(SourceJobConflict,match="not yet verified"):
        jobs.materialize("alice",job["job_id"],[1,2])
    source.blocked.clear()
    queued=jobs.retry_failures("alice",job["job_id"])
    assert (queued["completed"],queued["failed"],queued["pending"])==(2,0,1)
    asyncio.run(worker.run_once())
    done=jobs.get("alice",job["job_id"])
    assert done["status"]=="complete"
    assert len([u for u in source.fetches if "/60001/" in u])==1
    assert len([u for u in source.fetches if "/60002/" in u])==2
    assert len([u for u in source.fetches if "/60003/" in u])==1


def test_worker_refuses_duplicate_body_and_tampered_sqlite_content(tmp_path):
    store,jobs=make_store(tmp_path)
    job=jobs.create("alice","cat1",catalog(),[1,2])
    first=jobs.claim()
    jobs.finish(first["job_id"],first["lease_token"],body="Unique repeated source paragraph. "*10)
    second=jobs.claim()
    final=jobs.finish(second["job_id"],second["lease_token"],body="Unique repeated source paragraph. "*10)
    assert final["status"]=="partial"
    assert final["failed"]==1
    assert "Duplicate body" in final["failures"][0]["error"]
    fragment=jobs.materialize("alice",job["job_id"],[1])
    assert fragment.complete
    with store._connect() as con:
        con.execute("UPDATE source_collect_items SET body='Tampered' WHERE job_id=? AND ordinal=1",
                    (job["job_id"],))
    with pytest.raises(SourceJobConflict,match="checksum"):
        jobs.materialize("alice",job["job_id"],[1])


def test_api_background_job_stage_handback_and_actor_isolation(tmp_path):
    queue=QueueStore(tmp_path/"api.sqlite3")
    app=create_app(Settings(db_path=queue.db_path,team_tokens={"alice":"alice-key","bob":"bob-key"}),queue)
    source=FakeSite()
    app.state.public_source_fetcher=source
    auth={"X-API-Key":"alice-key"}
    other={"X-API-Key":"bob-key"}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            denied=await client.post("/v1/source-books/jobs",json={})
            assert denied.status_code==401
            discovered=await client.post("/v1/source-books/catalogs",headers=auth,
                json={"source":"royalroad","book_url":BOOK})
            assert discovered.status_code==201,discovered.text
            cat=discovered.json()
            spec={"catalog_id":cat["catalog_id"],"expected_catalog_sha256":cat["catalog_sha256"],
                  "start_ordinal":1,"end_ordinal":3}
            created=await client.post("/v1/source-books/jobs",json=spec,
                 headers={**auth,"Idempotency-Key":"first-run"})
            assert created.status_code==201,created.text
            jid=created.json()["job_id"]
            assert created.json()["total"]==3
            again=await client.post("/v1/source-books/jobs",json=spec,
                 headers={**auth,"Idempotency-Key":"first-run"})
            assert again.json()["job_id"]==jid
            assert (await client.get(f"/v1/source-books/jobs/{jid}",headers=other)).status_code==404
            assert (await client.post(f"/v1/source-books/jobs/{jid}/stage",headers=other,
                json={"start_ordinal":1,"end_ordinal":2})).status_code==404
            before=await client.post(f"/v1/source-books/jobs/{jid}/stage",headers=auth,
                json={"start_ordinal":1,"end_ordinal":2})
            assert before.status_code==409
            worker=SourceCollectionWorker(app.state.source_job_store,source)
            assert await worker.run_once()
            checkpoint=await client.get(f"/v1/source-books/jobs/{jid}",headers=auth)
            assert checkpoint.json()["completed"]==1
            # Simulate independent worker restart while the API app remains alive.
            restarted=SourceCollectionWorker(SourceJobStore(QueueStore(queue.db_path)),source)
            assert await restarted.run_once()
            assert await restarted.run_once()
            after=await client.get(f"/v1/source-books/jobs/{jid}",headers=auth)
            assert after.json()["status"]=="complete"
            staged=await client.post(f"/v1/source-books/jobs/{jid}/stage",headers=auth,
                json={"start_ordinal":1,"end_ordinal":2})
            assert staged.status_code==201,staged.text
            sid=staged.json()["stage_id"]
            repeated=await client.post(f"/v1/source-books/jobs/{jid}/stage",headers=auth,
                json={"start_ordinal":1,"end_ordinal":2})
            assert repeated.status_code==201,repeated.text
            assert repeated.json()["stage_id"]==sid
            view=await client.get(f"/v1/imports/stages/{sid}",headers=auth)
            assert view.status_code==200
            assert view.json()["preview"]["chapter_count"]==2
            receipt=await client.get(f"/v1/source-books/stages/{sid}/provenance",headers=auth)
            assert receipt.status_code==200
            assert [x["ordinal"] for x in receipt.json()["chapters"]]==[1,2]
            request={
              "book_id":"resumed","book_mode":"create","title":"Собранная книга",
              "prefix_policy":"reject","chapters":[
                 {"sequence":n,"action":"create","chapter_id":f"ch{n}",
                  "chapter_index":n,"chapter_path":f"chapters/{n}.xhtml",
                  "expected_revision":0}
                 for n in (1,2)
              ],
            }
            confirmed=await client.post(f"/v1/imports/stages/{sid}/confirm",headers=auth,json=request)
            assert confirmed.status_code==200,confirmed.text
            assert confirmed.json()["imported_count"]==2
            assert (await client.get("/v1/books/resumed/chapters/ch1",headers=auth)).status_code==200
            assert (await client.post(f"/v1/source-books/jobs/{jid}/cancel",headers=auth)).status_code==409
    asyncio.run(scenario())
