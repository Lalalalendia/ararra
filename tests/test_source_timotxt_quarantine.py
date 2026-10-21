"""Durable Timotxt RAW quarantine: no damaged chapter may become 'ok'.

All examples are synthetic. No access to any remote novel, SQLite production
data, Gemini, login browser, or real source server.
"""
from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_catalog_med import IngestError
from chaptera_server.source_collect import catalog_from_html
from chaptera_server.source_chapter_parts import fetch_complete_chapter
from chaptera_server.source_job_store import (
    SourceJobConflict, SourceJobStore,
)
from chaptera_server.source_job_worker import SourceCollectionWorker
from chaptera_server.storage import QueueStore

ROOT = "https://www.timotxt.com/2010493463/"
FIRST = ROOT + "10.html"
SECOND = ROOT + "11.html"
FIRST_PART2 = ROOT + "10_2.html"
TEXT_DAMAGED = "天亮後空間寶녪與놅仍發出微光，眾人繼續趕路。" * 22
TEXT_DAMAGED_PART2 = "河流盡頭傳來朦朧的聲音，녪門緩緩打開。" * 20
TEXT_CLEAN = "晴朗的早晨村莊逐漸熱鬧起來，大家準備新的旅途。" * 22
DIRECTORY = """<html><h1>測試小說 章節列表</h1>
<p>類型：穿越 章節： 2 更新：今天</p><h3>測試小說全部章节</h3>
<ul><li><a href="/2010493463/10.html">第一章 新的一天</a></li>
<li><a href="/2010493463/11.html">第二章 旅途</a></li></ul></html>"""
EVIDENCE = [
    {"project_id":1,"original":"空間寶녪","rus":"Камень Пространства"},
    {"project_id":2,"original":"空間寶石","rus":"Камень Пространства"},
]


def html(body: str, continuation: str | None = None) -> str:
    page = f'<div class="content"><p>{body}</p></div>'
    if continuation is not None:
        page += f'<div class="reader-pager"><a href="{continuation}">下一頁</a></div>'
    return "<html>" + page + "</html>"


def catalog():
    return catalog_from_html("timotxt",ROOT,DIRECTORY)


class FakeSite:
    def __init__(self, *, damaged=True, multipart=False):
        self.damaged = damaged
        self.multipart = multipart
        self.calls = []

    async def fetch(self, source: str, url: str) -> str:
        assert source == "timotxt"
        self.calls.append(url)
        if url == FIRST:
            raw = TEXT_DAMAGED if self.damaged else TEXT_CLEAN
            return html(raw,"/2010493463/10_2.html" if self.multipart else None)
        if url == FIRST_PART2:
            return html(TEXT_DAMAGED_PART2)
        if url == SECOND:
            return html(TEXT_CLEAN + "另一段不重複的故事情節。")
        raise AssertionError("Unexpected source request: " + url)


def setup(tmp_path, *, selected=None):
    repo = QueueStore(tmp_path / "queue.sqlite3")
    jobs = SourceJobStore(repo)
    job = jobs.create("chief","preview",catalog(),selected or [1])
    return repo,jobs,job["job_id"]


def test_suspect_raw_is_durable_but_never_completed_or_stageable(tmp_path):
    repo, jobs, job_id = setup(tmp_path,selected=[1,2])
    site = FakeSite()
    worker = SourceCollectionWorker(jobs,site)
    assert asyncio.run(worker.run_once(only_job_id=job_id))
    state = jobs.get("chief",job_id)
    assert state["completed"] == 0
    assert state["failed"] == 1
    assert state["review_snapshots"] == 1
    assert state["status"] == "queued"
    with pytest.raises(SourceJobConflict,match="not yet verified"):
        jobs.materialize("chief",job_id,[1])
    record = jobs.reviews.list("chief",job_id)
    assert len(record) == 1
    assert "raw_text" not in record[0]
    assert record[0]["raw_sha256"] == hashlib.sha256(TEXT_DAMAGED.encode()).hexdigest()
    assert record[0]["status"] == "needs_review"

    # Same database reopened by independent worker/API process.
    recovered = SourceJobStore(QueueStore(repo.db_path))
    detail = recovered.reviews.detail("chief",job_id,record[0]["snapshot_id"])
    assert detail["raw_text"] == TEXT_DAMAGED
    assert detail["candidates"] == []
    assert not detail["exportable"]

    assert asyncio.run(SourceCollectionWorker(recovered,site).run_once(only_job_id=job_id))
    latest = recovered.get("chief",job_id)
    assert latest["status"] == "partial"
    assert latest["completed"] == 1
    assert latest["failed"] == 1
    assert latest["review_snapshots"] == 1
    assert [x.ordinal for x in recovered.materialize("chief",job_id,[2]).chapters] == [2]
    with pytest.raises(SourceJobConflict):
        recovered.materialize("chief",job_id,[1,2])
    assert site.calls == [FIRST,SECOND]


def test_one_shot_staging_still_refuses_damaged_text(tmp_path):
    async def fetch(url):
        assert url == FIRST
        return html(TEXT_DAMAGED)
    with pytest.raises(IngestError,match="obfuscated"):
        asyncio.run(fetch_complete_chapter("timotxt",ROOT,FIRST,fetch))
    # No quarantine mode for another domain, or for ordinary stage/import.
    with pytest.raises(ValueError,match="Timotxt-only"):
        asyncio.run(fetch_complete_chapter(
            "webnovel","https://www.webnovel.com/book/123456789",
            "https://www.webnovel.com/book/123456789/chapter/987654321",fetch,
            allow_unverified_timotxt=True,
        ))


def test_multi_page_raw_is_quarantined_only_after_every_part(tmp_path):
    _,jobs,job_id = setup(tmp_path)
    site=FakeSite(multipart=True)
    assert asyncio.run(SourceCollectionWorker(jobs,site).run_once(only_job_id=job_id))
    item=jobs.reviews.list("chief",job_id)[0]
    saved=jobs.reviews.detail("chief",job_id,item["snapshot_id"])
    assert TEXT_DAMAGED in saved["raw_text"]
    assert TEXT_DAMAGED_PART2 in saved["raw_text"]
    assert site.calls == [FIRST,FIRST_PART2]


def test_crash_or_stale_lease_cannot_create_quarantine(tmp_path):
    _,jobs,job_id = setup(tmp_path)
    claimed=jobs.claim(only_job_id=job_id)
    assert claimed is not None
    jobs.cancel("chief",job_id)
    with pytest.raises(SourceJobConflict,match="Stale source job lease"):
        jobs.finish(job_id,claimed["lease_token"],error="needs_review",review_raw=TEXT_DAMAGED)
    assert jobs.reviews.list("chief",job_id) == []
    assert jobs.get("chief",job_id)["completed"] == 0


def test_raw_review_survives_retry_but_does_not_turn_into_clean_item(tmp_path):
    _,jobs,job_id = setup(tmp_path)
    fake=FakeSite()
    asyncio.run(SourceCollectionWorker(jobs,fake).run_once(only_job_id=job_id))
    first=jobs.reviews.list("chief",job_id)[0]
    assert jobs.get("chief",job_id)["status"] == "partial"
    jobs.retry_failures("chief",job_id)
    fake.damaged=False
    asyncio.run(SourceCollectionWorker(jobs,fake).run_once(only_job_id=job_id))
    assert jobs.get("chief",job_id)["completed"] == 1
    assert jobs.reviews.list("chief",job_id)[0]["snapshot_id"] == first["snapshot_id"]
    assert TEXT_CLEAN in jobs.materialize("chief",job_id,[1]).chapters[0].body
    # A later verified fetch does not rewrite the previous quarantined source.
    assert TEXT_DAMAGED == jobs.reviews.detail(
        "chief",job_id,first["snapshot_id"],
    )["raw_text"]


def test_candidate_is_separate_cas_bound_and_never_exportable(tmp_path):
    _,jobs,job_id = setup(tmp_path)
    asyncio.run(SourceCollectionWorker(jobs,FakeSite()).run_once(only_job_id=job_id))
    before=jobs.reviews.list("chief",job_id)[0]
    snapshot_id=before["snapshot_id"]
    result=jobs.reviews.derive(
        "chief",job_id,snapshot_id,
        expected_raw_sha256=before["raw_sha256"],glossary_rows=EVIDENCE,
    )
    assert result["raw_sha256"] == before["raw_sha256"]
    assert result["normalized_sha256"] != result["raw_sha256"]
    assert result["status"] == "needs_review"
    assert result["candidate_only"] and not result["exportable"]
    after=SourceJobStore(jobs.store).reviews.detail("chief",job_id,snapshot_id)
    assert after["raw_text"] == TEXT_DAMAGED
    assert "空間寶石" in after["candidates"][0]["normalized_text"]
    assert "놅" in after["candidates"][0]["normalized_text"]
    assert after["candidates"][0]["mapping_used"] == {"녪":"石"}
    assert after["candidates"][0]["candidate_only"] is True
    # An idempotent derivation does not multiply candidate rows.
    jobs.reviews.derive(
        "chief",job_id,snapshot_id,
        expected_raw_sha256=before["raw_sha256"],glossary_rows=EVIDENCE,
    )
    assert len(jobs.reviews.detail("chief",job_id,snapshot_id)["candidates"]) == 1
    with pytest.raises(SourceJobConflict,match="Original changed"):
        jobs.reviews.derive(
            "chief",job_id,snapshot_id,
            expected_raw_sha256="0"*64,glossary_rows=EVIDENCE,
        )
    assert jobs.get("chief",job_id)["completed"] == 0


def test_quarantine_byte_budget_fails_without_false_raw_saved(tmp_path,monkeypatch):
    import chaptera_server.source_job_review as mod
    monkeypatch.setattr(mod,"MAX_REVIEW_GLOBAL_BYTES",8)
    _,jobs,job_id=setup(tmp_path)
    asyncio.run(SourceCollectionWorker(jobs,FakeSite()).run_once(only_job_id=job_id))
    state=jobs.get("chief",job_id)
    assert state["failed"]==1 and state["completed"]==0
    assert state["review_snapshots"]==0
    assert "RAW was not saved" in state["failures"][0]["error"]
    assert jobs.reviews.list("chief",job_id)==[]


def test_tampered_original_and_derived_checksums_are_rejected(tmp_path):
    repo,jobs,job_id = setup(tmp_path)
    asyncio.run(SourceCollectionWorker(jobs,FakeSite()).run_once(only_job_id=job_id))
    receipt=jobs.reviews.list("chief",job_id)[0]
    jobs.reviews.derive(
        "chief",job_id,receipt["snapshot_id"],
        expected_raw_sha256=receipt["raw_sha256"],glossary_rows=EVIDENCE,
    )
    with repo._connect() as con:
        con.execute("UPDATE source_collect_review_derived SET normalized_text='X' "
                    "WHERE snapshot_id=?",(receipt["snapshot_id"],))
    with pytest.raises(SourceJobConflict,match="Derived source candidate integrity"):
        jobs.reviews.detail("chief",job_id,receipt["snapshot_id"])
    with repo._connect() as con:
        con.execute("UPDATE source_collect_review_raw SET raw_text='X' "
                    "WHERE snapshot_id=?",(receipt["snapshot_id"],))
    with pytest.raises(SourceJobConflict,match="Stored original review integrity"):
        jobs.reviews.detail("chief",job_id,receipt["snapshot_id"])


def test_deleting_terminal_job_cascades_private_review_and_derived(tmp_path):
    repo,jobs,job_id = setup(tmp_path)
    asyncio.run(SourceCollectionWorker(jobs,FakeSite()).run_once(only_job_id=job_id))
    receipt=jobs.reviews.list("chief",job_id)[0]
    jobs.reviews.derive(
        "chief",job_id,receipt["snapshot_id"],
        expected_raw_sha256=receipt["raw_sha256"],glossary_rows=EVIDENCE,
    )
    removed=jobs.delete_finished("chief",job_id)
    assert removed["deleted_review_snapshots"] == 1
    assert removed["freed_review_bytes"] > 0
    with repo._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM source_collect_review_raw").fetchone()[0]==0
        assert con.execute("SELECT COUNT(*) FROM source_collect_review_derived").fetchone()[0]==0


def test_http_actor_isolation_and_review_cas(tmp_path):
    repo,jobs,job_id=setup(tmp_path)
    asyncio.run(SourceCollectionWorker(jobs,FakeSite()).run_once(only_job_id=job_id))
    app=create_app(Settings(db_path=repo.db_path,team_tokens={
        "chief":"chief-key", "mod":"mod-key",
    }),repo)
    route=f"/v1/source-books/jobs/{job_id}/reviews"

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url="http://test",
        ) as client:
            assert (await client.get(route)).status_code==401
            other=await client.get(route,headers={"X-API-Key":"mod-key"})
            assert other.status_code==404
            head={"X-API-Key":"chief-key"}
            index=await client.get(route,headers=head)
            assert index.status_code==200 and "no-store" in index.headers["cache-control"]
            item=index.json()["snapshots"][0]
            assert "raw_text" not in index.text
            detail=await client.get(route+"/"+item["snapshot_id"],headers=head)
            assert detail.status_code==200
            assert detail.json()["raw_text"]==TEXT_DAMAGED
            assert "no-store" in detail.headers["cache-control"]
            other_detail=await client.get(
                route+"/"+item["snapshot_id"],headers={"X-API-Key":"mod-key"},
            )
            assert other_detail.status_code==404
            posted=await client.post(
                route+"/"+item["snapshot_id"]+"/candidates",headers=head,
                json={"expected_raw_sha256":item["raw_sha256"],
                      "glossary_rows":EVIDENCE},
            )
            assert posted.status_code==201,posted.text
            assert posted.json()["candidate_only"]
            assert "normalized_text" not in posted.text
            stale=await client.post(
                route+"/"+item["snapshot_id"]+"/candidates",headers=head,
                json={"expected_raw_sha256":"0"*64,"glossary_rows":EVIDENCE},
            )
            assert stale.status_code==409
            stranger=await client.post(
                route+"/"+item["snapshot_id"]+"/candidates",
                headers={"X-API-Key":"mod-key"},
                json={"expected_raw_sha256":item["raw_sha256"],
                      "glossary_rows":EVIDENCE},
            )
            assert stranger.status_code==404
            assert jobs.get("chief",job_id)["completed"]==0
    asyncio.run(check())
