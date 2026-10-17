"""RE-02 recent source jobs: actor-scoped recovery, safe explicit deletion."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_collect import catalog_from_html
from chaptera_server.source_job_store import SourceJobStore, SourceJobConflict, SourceJobMissing
from chaptera_server.storage import QueueStore

BOOK = "https://www.royalroad.com/fiction/142007/aura"
CATALOG = '<div id="chapters">' + "".join(
    f'<a href="/fiction/142007/aura/chapter/{80000+n}/{n}-part">{n} - Part {n}</a>'
    for n in (1,2)
) + "</div>"
ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


def fixture(tmp_path):
    db=QueueStore(tmp_path/"history.sqlite3")
    jobs=SourceJobStore(db)
    catalog=catalog_from_html("royalroad",BOOK,CATALOG)
    return db,jobs,catalog


def finish_all(jobs,job_id):
    for n in (1,2):
        claim=jobs.claim()
        assert claim and claim["job_id"]==job_id
        jobs.finish(job_id,claim["lease_token"],body=f"Verified source chapter {n}. "*15)


def test_recent_history_is_actor_scoped_and_sorted_by_update(tmp_path):
    db,jobs,cat=fixture(tmp_path)
    a=jobs.create("alice","cat-a",cat,[1,2],idempotency_key="A")
    b=jobs.create("bob","cat-b",cat,[1,2],idempotency_key="B")
    assert [j["job_id"] for j in jobs.list_recent("alice")]==[a["job_id"]]
    assert [j["job_id"] for j in jobs.list_recent("bob")]==[b["job_id"]]
    second=jobs.create("alice","cat-c",cat,[1],idempotency_key="C")
    with db._connect() as con:
        con.execute("UPDATE source_collect_jobs SET updated_at=123 WHERE id=?",(a["job_id"],))
        con.execute("UPDATE source_collect_jobs SET updated_at=456 WHERE id=?",(second["job_id"],))
    recents=jobs.list_recent("alice")
    assert [x["job_id"] for x in recents]==[second["job_id"],a["job_id"]]
    assert all("body" not in str(job) for job in recents)
    assert all(job["book_url"]==BOOK for job in recents)
    assert len(jobs.list_recent("alice",limit=1))==1
    with pytest.raises(ValueError):
        jobs.list_recent("alice",limit=51)


def test_delete_refuses_active_job_and_stale_worker_and_preserves_others(tmp_path):
    db,jobs,cat=fixture(tmp_path)
    a=jobs.create("alice","cat-a",cat,[1,2])
    b=jobs.create("bob","cat-b",cat,[1,2])
    with pytest.raises(SourceJobConflict,match="Cancel"):
        jobs.delete_finished("alice",a["job_id"])
    claim=jobs.claim()
    with pytest.raises(SourceJobConflict,match="Cancel"):
        jobs.delete_finished("alice",a["job_id"])
    with pytest.raises(SourceJobMissing):
        jobs.delete_finished("bob",a["job_id"])
    jobs.cancel("alice",a["job_id"])
    deleted=jobs.delete_finished("alice",a["job_id"])
    assert deleted["deleted"] and deleted["library_unchanged"]
    assert deleted["deleted_checkpoints"]==0
    with pytest.raises(SourceJobMissing):
        jobs.get("alice",a["job_id"])
    with pytest.raises(SourceJobConflict,match="Stale"):
        jobs.finish(a["job_id"],claim["lease_token"],body="Invalid stale result. "*15)
    assert jobs.get("bob",b["job_id"])["status"]=="queued"


def test_completed_job_delete_cascades_items_and_never_erases_imported_book(tmp_path):
    db,jobs,cat=fixture(tmp_path)
    a=jobs.create("alice","cat-a",cat,[1,2])
    finish_all(jobs,a["job_id"])
    app=create_app(Settings(db_path=db.db_path,team_tokens={
        "alice":"alice-key","bob":"bob-key",
    }),db)
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url="http://test",
        ) as client:
            jpath="/v1/source-books/jobs"
            assert (await client.get(jpath)).status_code==401
            other=(await client.get(jpath,headers=BOB))
            assert other.status_code==200 and other.json()["jobs"]==[]
            mine=(await client.get(jpath+"?limit=20",headers=ALICE))
            assert mine.status_code==200
            assert [x["job_id"] for x in mine.json()["jobs"]]==[a["job_id"]]
            assert "Verified source chapter" not in mine.text
            assert (await client.delete(jpath+"/"+a["job_id"],headers=BOB)).status_code==404
            assert (await client.delete(jpath+"/"+a["job_id"])).status_code==401
            staged=await client.post(jpath+"/"+a["job_id"]+"/stage",headers=ALICE,json={
                "start_ordinal":1,"end_ordinal":2,
            })
            assert staged.status_code==201,staged.text
            sid=staged.json()["stage_id"]
            confirmed=await client.post(f"/v1/imports/stages/{sid}/confirm",
                headers=ALICE,json={
                    "book_id":"history-survives","book_mode":"create",
                    "title":"Source book","prefix_policy":"reject",
                    "chapters":[
                        {"sequence":n,"action":"create","chapter_id":f"c{n}",
                        "chapter_index":n,"chapter_path":f"chapters/{n}.xhtml",
                        "expected_revision":0}
                        for n in (1,2)
                    ],
                })
            assert confirmed.status_code==200,confirmed.text
            deleted=await client.delete(jpath+"/"+a["job_id"],headers=ALICE)
            assert deleted.status_code==200,deleted.text
            receipt=deleted.json()
            assert receipt["deleted_checkpoints"]==2
            assert receipt["freed_source_bytes"]>100
            assert receipt["library_unchanged"] is True
            assert (await client.get(jpath+"/"+a["job_id"],headers=ALICE)).status_code==404
            assert (await client.get(jpath,headers=ALICE)).json()["jobs"]==[]
            assert (await client.get(f"/v1/books/history-survives/chapters/c1",headers=ALICE)).status_code==200
            provenance=await client.get(f"/v1/source-books/stages/{sid}/provenance",headers=ALICE)
            assert provenance.status_code==200
    asyncio.run(scenario())
    with db._connect() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM source_collect_items WHERE job_id=?",(a["job_id"],)
        ).fetchone()[0]==0
