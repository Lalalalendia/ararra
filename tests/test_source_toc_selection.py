"""RE-02 two-stage TOC plans: CAS, sparse ordering, checkpoints and staging.

Entire suite is offline. No Gemini, Drive or live-site requests.
"""
from __future__ import annotations

import asyncio
import hashlib
import time

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_collect import catalog_from_html
from chaptera_server.source_job_store import SourceJobStore, SourceJobConflict
from chaptera_server.source_job_worker import SourceCollectionWorker
from chaptera_server.source_selection_store import (
    SourceSelectionStore, SourceSelectionMissing,
    SourceSelectionExpired, SourceSelectionConflict,
)
from chaptera_server.source_site_storage import SourceCatalogStore
from chaptera_server.source_job_export import VerifiedSourceTXT
from chaptera_server.storage import QueueStore

BOOK="https://www.royalroad.com/fiction/142007/aura"
TOC='<div id="chapters">'+''.join(
    f'<a href="/fiction/142007/aura/chapter/{60000+n}/{n}-part">{n} - Part {n}</a>'
    for n in (1,2,3)
)+'</div>'
BODY={n:'<div class="chapter-content"><p>'+
      (f"Unique original chapter {n} body. "*16)+'</p></div>'
      for n in (1,2,3)}
ALICE={"X-API-Key":"alice-key"}
BOB={"X-API-Key":"bob-key"}


class FakeSite:
    def __init__(self):
        self.calls=[]

    async def fetch(self, source, url):
        assert source == "royalroad"
        self.calls.append(url)
        if url == BOOK:
            return TOC
        for n in (1,2,3):
            if f"/{60000+n}/" in url:
                return BODY[n]
        raise AssertionError("Unknown URL in selected collection")


def create_fixture(tmp_path):
    store=QueueStore(tmp_path/"ordered.sqlite3")
    catalogs=SourceCatalogStore(store)
    selections=SourceSelectionStore(store)
    jobs=SourceJobStore(store)
    cat=catalog_from_html("royalroad",BOOK,TOC)
    saved_catalog=catalogs.save("alice",cat)
    return store,catalogs,selections,jobs,cat,saved_catalog


def test_selection_saved_cas_actor_restart_and_expiry(tmp_path):
    store,catalogs,selections,_,cat,discovered=create_fixture(tmp_path)
    cat_id=discovered["catalog_id"]
    first=selections.save("alice",cat_id,cat,[3,1],
        expected_catalog_sha256=cat.catalog_sha256,expected_selection_sha256=None)
    assert first["ordered_ordinals"]==[3,1]
    assert first["selected_count"]==2 and first["catalog_count"]==3
    assert len(first["selection_sha256"])==64
    assert selections.recent("bob")["selection"] is None
    with pytest.raises(SourceSelectionMissing):
        selections.get("bob",cat_id)
    with pytest.raises(SourceSelectionMissing):
        selections.load("bob",first["selection_id"])
    restarted=SourceSelectionStore(QueueStore(store.db_path))
    assert restarted.get("alice",cat_id)["ordered_ordinals"]==[3,1]
    assert restarted.recent("alice")["selection"]["selection_id"]==first["selection_id"]
    with pytest.raises(SourceSelectionConflict,match="another tab"):
        restarted.save("alice",cat_id,cat,[1,3],
            expected_catalog_sha256=cat.catalog_sha256,
            expected_selection_sha256=None)
    updated=restarted.save("alice",cat_id,cat,[1,3],
        expected_catalog_sha256=cat.catalog_sha256,
        expected_selection_sha256=first["selection_sha256"])
    assert updated["selection_id"]==first["selection_id"]
    assert updated["selection_sha256"]!=first["selection_sha256"]
    with pytest.raises(SourceSelectionConflict):
        restarted.save("alice",cat_id,cat,[3,2],
            expected_catalog_sha256=cat.catalog_sha256,
            expected_selection_sha256=first["selection_sha256"])
    with store._connect() as con:
        con.execute("UPDATE source_book_catalogs SET expires_at=? WHERE id=?",
                    (time.time()-1,cat_id))
    with pytest.raises(SourceSelectionExpired):
        restarted.load("alice",updated["selection_id"])
    assert restarted.recent("alice")["selection"] is None


def test_selection_rejects_unseen_duplicate_empty_bool_and_tampered_digest(tmp_path):
    store,_,selections,_,cat,discovered=create_fixture(tmp_path)
    base={"expected_catalog_sha256":cat.catalog_sha256,
          "expected_selection_sha256":None}
    for invalid in ([],[1,1],[1,100],[True],[-1],["1"]):
        with pytest.raises(SourceSelectionConflict):
            selections.save("alice",discovered["catalog_id"],cat,invalid,**base)
    with pytest.raises(SourceSelectionConflict,match="Stale"):
        selections.save("alice",discovered["catalog_id"],cat,[2],
            expected_catalog_sha256="0"*64,expected_selection_sha256=None)
    result=selections.save("alice",discovered["catalog_id"],cat,[3,1],**base)
    with store._connect() as con:
        con.execute("UPDATE source_book_selections SET ordered_ordinals_json='[1,3]' "
                    "WHERE id=?", (result["selection_id"],))
    with pytest.raises(SourceSelectionConflict,match="invalid"):
        selections.load("alice",result["selection_id"])


def test_job_persists_selected_order_restart_and_selected_only_export(tmp_path):
    store,_,selections,jobs,cat,discovered=create_fixture(tmp_path)
    saved=selections.save("alice",discovered["catalog_id"],cat,[3,1],
        expected_catalog_sha256=cat.catalog_sha256,
        expected_selection_sha256=None)
    created=jobs.create("alice",discovered["catalog_id"],cat,[3,1],
        idempotency_key="ordered-3-1",preserve_order=True,
        selection_sha256=saved["selection_sha256"],
        selection_id=saved["selection_id"])
    assert created["selection_mode"]=="ordered"
    assert created["selected_ordinals"]==[3,1]
    assert created["total"]==2
    assert (created["selected_start"],created["selected_end"])==(1,3)
    assert jobs.create("alice",discovered["catalog_id"],cat,[3,1],
        idempotency_key="ordered-3-1",preserve_order=True,
        selection_sha256=saved["selection_sha256"],
        selection_id=saved["selection_id"])["job_id"]==created["job_id"]
    with pytest.raises(SourceJobConflict,match="Saved selection changed"):
        jobs.create("alice",discovered["catalog_id"],cat,[1,3],
            idempotency_key="ordered-3-1",preserve_order=True,
            selection_sha256=saved["selection_sha256"],
            selection_id=saved["selection_id"])
    modified=selections.save("alice",discovered["catalog_id"],cat,[1,3],
        expected_catalog_sha256=cat.catalog_sha256,
        expected_selection_sha256=saved["selection_sha256"])
    with pytest.raises(SourceJobConflict,match="Idempotency"):
        jobs.create("alice",discovered["catalog_id"],cat,[1,3],
            idempotency_key="ordered-3-1",preserve_order=True,
            selection_sha256=modified["selection_sha256"],
            selection_id=modified["selection_id"])
    one=jobs.claim()
    assert one["chapter"]["ordinal"]==3 and one["item_index"]==0
    jobs.finish(one["job_id"],one["lease_token"],body=("Original third source. "*20))
    restarted=SourceJobStore(QueueStore(store.db_path))
    assert restarted.get("alice",created["job_id"])["selected_ordinals"]==[3,1]
    two=restarted.claim()
    assert two["chapter"]["ordinal"]==1 and two["item_index"]==1
    restarted.finish(two["job_id"],two["lease_token"],body=("Original first source. "*20))
    assert restarted.claim() is None
    state=restarted.get("alice",created["job_id"])
    assert state["status"]=="complete"
    original=restarted.materialize("alice",created["job_id"],[3,1])
    assert [c.ordinal for c in original.chapters]==[3,1]
    text=original.as_import_html().decode()
    assert text.index("Глава 3.")<text.index("Глава 1.")
    report=VerifiedSourceTXT(store).inspect("alice",created["job_id"])
    assert report["ready"] is True
    assert report["scope"]=="selected_catalog_selection_only"
    _,rendered=VerifiedSourceTXT(store).render("alice",created["job_id"],
        report["manifest_sha256"],report["content_sha256"])
    try:
        content=rendered.file.read()
    finally:
        rendered.file.close()
    assert content.index(b"Original third source")<content.index(b"Original first source")
    assert hashlib.sha256(content).hexdigest()==report["content_sha256"]
    assert b"Chapter 2" not in content


def test_ordered_selection_api_stage_and_book_index_are_in_user_order(tmp_path):
    store=QueueStore(tmp_path/"api.sqlite3")
    app=create_app(Settings(db_path=store.db_path,team_tokens={
        "alice":"alice-key","bob":"bob-key",
    }),store)
    fake=FakeSite()
    app.state.public_source_fetcher=fake

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            cat_resp=await client.post("/v1/source-books/catalogs",
                headers=ALICE,json={"source":"royalroad","book_url":BOOK})
            assert cat_resp.status_code==201,cat_resp.text
            cat=cat_resp.json()
            target=f"/v1/source-books/catalogs/{cat['catalog_id']}/selection"
            payload={"ordered_ordinals":[3,1],"expected_catalog_sha256":cat["catalog_sha256"]}
            assert (await client.put(target,json=payload)).status_code==401
            assert (await client.put(target,headers=BOB,json=payload)).status_code==404
            plan_resp=await client.put(target,headers=ALICE,json=payload)
            assert plan_resp.status_code==200,plan_resp.text
            plan=plan_resp.json()
            assert plan["ordered_ordinals"]==[3,1]
            assert (await client.get(target,headers=BOB)).status_code==404
            assert (await client.get(target,headers=ALICE)).json()["selection_sha256"]==plan["selection_sha256"]
            assert (await client.get("/v1/source-books/selections/recent",headers=ALICE)
                    ).json()["selection"]["selection_id"]==plan["selection_id"]
            assert (await client.get("/v1/source-books/selections/recent",headers=BOB)
                    ).json()["selection"] is None
            jobs_url="/v1/source-books/jobs"
            spec={"catalog_id":cat["catalog_id"],
                  "expected_catalog_sha256":cat["catalog_sha256"],
                  "selection_id":plan["selection_id"],
                  "expected_selection_sha256":plan["selection_sha256"]}
            mixed=await client.post(jobs_url,headers=ALICE,json={**spec,
                "start_ordinal":1,"end_ordinal":3})
            assert mixed.status_code==409
            stale=await client.post(jobs_url,headers=ALICE,json={**spec,
                "expected_selection_sha256":"0"*64})
            assert stale.status_code==409
            foreign=await client.post(jobs_url,headers=BOB,json=spec)
            assert foreign.status_code in (404,409)
            created=await client.post(jobs_url,headers={**ALICE,
                "Idempotency-Key":"plan-3-1"},json=spec)
            assert created.status_code==201,created.text
            job=created.json()
            assert job["selected_ordinals"]==[3,1]
            assert job["selection_mode"]=="ordered"
            jid=job["job_id"]
            assert (await client.post(f"{jobs_url}/{jid}/stage",headers=ALICE,json={
                "start_ordinal":1,"end_ordinal":3})).status_code==422
            worker=SourceCollectionWorker(app.state.source_job_store,fake)
            assert await worker.run_once()
            halfway=await client.get(f"{jobs_url}/{jid}",headers=ALICE)
            assert halfway.json()["completed"]==1
            assert await worker.run_once()
            assert not await worker.run_once()
            assert any("/60003/" in url for url in fake.calls)
            assert any("/60001/" in url for url in fake.calls)
            assert not any("/60002/" in url for url in fake.calls)
            created_stage=await client.post(f"{jobs_url}/{jid}/stage",headers=ALICE,
                json={"start_position":1,"end_position":2})
            assert created_stage.status_code==201,created_stage.text
            sid=created_stage.json()["stage_id"]
            repeated=await client.post(f"{jobs_url}/{jid}/stage",headers=ALICE,
                json={"start_position":1,"end_position":2})
            assert repeated.status_code==201 and repeated.json()["stage_id"]==sid
            view=await client.get(f"/v1/imports/stages/{sid}",headers=ALICE)
            assert view.status_code==200,view.text
            preview=view.json()["preview"]["chapters"]
            assert [c["detected_number"] for c in preview]==[3,1]
            assert [c["destination_position"] for c in preview]==[1,2]
            provenance=await client.get(f"/v1/source-books/stages/{sid}/provenance",headers=ALICE)
            assert [c["ordinal"] for c in provenance.json()["chapters"]]==[3,1]
            assert [c["destination_position"] for c in provenance.json()["chapters"]]==[1,2]
            before=await client.get("/v1/books",headers=ALICE)
            assert "ordered-book" not in before.text
            confirm=await client.post(f"/v1/imports/stages/{sid}/confirm",
                headers=ALICE,json={
                    "book_id":"ordered-book","book_mode":"create",
                    "title":"Selected only","prefix_policy":"reject",
                    "chapters":[
                        {"sequence":1,"action":"create","chapter_id":"ch3",
                         "chapter_index":1,"chapter_path":"chapters/1.xhtml",
                         "expected_revision":0},
                        {"sequence":2,"action":"create","chapter_id":"ch1",
                         "chapter_index":2,"chapter_path":"chapters/2.xhtml",
                         "expected_revision":0},
                    ],
                })
            assert confirm.status_code==200,confirm.text
            chapter=await client.get("/v1/books/ordered-book/chapters/ch3",headers=ALICE)
            assert chapter.json()["chapter_index"]==1
            assembly=await client.get(f"{jobs_url}/{jid}/assembly",headers=ALICE)
            assert assembly.status_code==200 and assembly.json()["ready"]
            assert assembly.json()["scope"]=="selected_catalog_selection_only"
            result=await client.get(f"{jobs_url}/{jid}/export.txt",headers=ALICE,
                params={"expected_manifest_sha256":assembly.json()["manifest_sha256"],
                        "expected_content_sha256":assembly.json()["content_sha256"]})
            assert result.status_code==200,result.text
            assert result.headers["x-readera-source-scope"]=="selected_catalog_selection_only"
            assert result.content.index(b"chapter 3")<result.content.index(b"chapter 1")
    asyncio.run(scenario())


def test_ordered_job_rejects_changed_persisted_selection_digest(tmp_path):
    store,_,selections,jobs,cat,discovered=create_fixture(tmp_path)
    saved=selections.save("alice",discovered["catalog_id"],cat,[3,1],
        expected_catalog_sha256=cat.catalog_sha256,expected_selection_sha256=None)
    created=jobs.create("alice",discovered["catalog_id"],cat,[3,1],
        preserve_order=True,selection_sha256=saved["selection_sha256"],
        selection_id=saved["selection_id"])
    with store._connect() as con:
        con.execute("UPDATE source_collect_jobs SET selection_sha256=? WHERE id=?",
                    ("0"*64,created["job_id"]))
    with pytest.raises(SourceJobConflict,match="Ordered selection digest"):
        jobs.get("alice",created["job_id"])
