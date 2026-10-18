"""Team-wide verified original source reads; all job mutations remain actor-owned.

This suite uses synthetic chapters and no external network traffic.
"""
from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_collect import catalog_from_html
from chaptera_server.source_job_store import SourceJobStore, SourceJobMissing
from chaptera_server.source_team_reader import SourceTeamReader
from chaptera_server.storage import QueueStore

BOOK = "https://www.royalroad.com/fiction/142007/aura"
TOC = '<div id="chapters">' + "".join(
    f'<a href="/fiction/142007/aura/chapter/{90000+n}/{n}-part">{n} - Part {n}</a>'
    for n in (1, 2, 3)
) + "</div>"
ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}
ADMIN = {"X-API-Key": "admin-key"}


def fixture(tmp_path):
    db = QueueStore(tmp_path / "shared.sqlite3")
    app = create_app(Settings(db_path=db.db_path,
        team_tokens={"alice":"alice-key","bob":"bob-key","admin":"admin-key"},
        admin_member_id="admin"), db)
    jobs = app.state.source_job_store
    cat = catalog_from_html("royalroad",BOOK,TOC)
    jid = jobs.create("alice","cat1",cat,[1,2,3])["job_id"]
    return app,db,jobs,jid


def checkpoint(jobs, jid, n, text=None):
    claimed = jobs.claim()
    assert claimed and claimed["job_id"] == jid and claimed["chapter"]["ordinal"] == n
    body = text or (f"Original synthetic chapter {n}. "*20)
    jobs.finish(jid,claimed["lease_token"],body=body)
    return body


def test_team_can_read_single_verified_chapter_but_not_manage_other_jobs(tmp_path):
    app,repo,jobs,jid = fixture(tmp_path)
    original = checkpoint(jobs,jid,1,
        '<script>alert("inert original text")</script>\n' + "A sample story text. "*15)
    assert jobs.get("alice",jid)["completed"] == 1
    assert jobs.get("alice",jid)["status"] == "queued"

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url="http://test",
        ) as client:
            prefix = "/v1/source-books/jobs"
            shared = "/v1/source-books/shared"
            for path in (
                shared,shared+"/"+jid,
                shared+"/"+jid+"/chapters/1",
                shared+"/"+jid+"/assembly",
                shared+"/"+jid+"/export.txt",
            ):
                assert (await client.get(path)).status_code == 401

            for identity in (BOB,ADMIN):
                index = await client.get(shared,headers=identity)
                assert index.status_code == 200,index.text
                assert index.json()["read_only"] is True
                entries = index.json()["sources"]
                assert len(entries) == 1
                entry = entries[0]
                assert entry["job_id"] == jid
                assert entry["owner_member_id"] == "alice"
                assert entry["verified_count"] == 1
                assert entry["selected_count"] == 3
                assert entry["can_manage"] is False
                assert "Original synthetic" not in str(entries)
                assert "alice-key" not in str(entries)
                available = await client.get(shared+"/"+jid,headers=identity)
                assert available.status_code == 200,available.text
                listing = available.json()
                assert listing["verified_count"] == 1
                assert [x["ordinal"] for x in listing["chapters"]] == [1]
                assert "body" not in str(listing)
                text = await client.get(shared+f"/{jid}/chapters/1",headers=identity)
                assert text.status_code == 200,text.text
                assert text.headers["cache-control"] == "no-store, private"
                assert text.headers["x-content-type-options"] == "nosniff"
                assert text.json()["body"] == original
                assert text.json()["sha256"] == hashlib.sha256(original.encode()).hexdigest()
                assert text.json()["scope"] == "single_verified_original"
                assert (await client.get(shared+f"/{jid}/chapters/2",
                                         headers=identity)).status_code == 404

            # The existing owner-only APIs stay owner-only in spite of team read.
            assert (await client.get(prefix,headers=BOB)).json()["jobs"] == []
            assert (await client.get(prefix+"/"+jid,headers=BOB)).status_code == 404
            assert (await client.get(prefix+"/"+jid+"/assembly",headers=BOB)).status_code == 404
            assert (await client.delete(prefix+"/"+jid,headers=BOB)).status_code == 404
            assert (await client.post(prefix+"/"+jid+"/cancel",headers=BOB)).status_code == 404
            assert (await client.post(prefix+"/"+jid+"/retry",headers=BOB)).status_code == 404
            forbidden = await client.post(prefix+"/"+jid+"/stage",
                headers=BOB,json={"start_ordinal":1,"end_ordinal":1})
            assert forbidden.status_code == 404
            ready = await client.get(shared+"/"+jid+"/assembly",headers=BOB)
            assert ready.status_code == 200
            assert ready.json()["ready"] is False
            assert "Original synthetic" not in ready.text
            no_full = await client.get(
                shared+"/"+jid+"/export.txt",
                headers=BOB,params={
                    "expected_manifest_sha256":ready.json()["manifest_sha256"],
                    "expected_content_sha256":"0"*64,
                },
            )
            assert no_full.status_code == 409
    asyncio.run(scenario())

    assert jobs.usage("bob")["actor_used_bytes"] == 0
    assert jobs.usage("alice")["actor_used_bytes"] == len(original.encode())


def test_team_can_download_only_complete_verified_txt_and_detect_change(tmp_path):
    app,repo,jobs,jid = fixture(tmp_path)
    for n in (1,2,3):
        checkpoint(jobs,jid,n)
    assert jobs.get("alice",jid)["status"] == "complete"

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url="http://test",
        ) as client:
            root=f"/v1/source-books/shared/{jid}"
            preview=await client.get(root+"/assembly",headers=BOB)
            assert preview.status_code == 200
            receipt=preview.json()
            assert receipt["ready"] is True
            assert receipt["selected_count"] == receipt["verified_count"] == 3
            query={"expected_manifest_sha256":receipt["manifest_sha256"],
                   "expected_content_sha256":receipt["content_sha256"]}
            result=await client.get(root+"/export.txt",headers=BOB,params=query)
            assert result.status_code == 200,result.text
            assert result.headers["cache-control"] == "no-store, private"
            assert result.headers["x-readera-source-scope"] == "selected_catalog_range_only"
            assert hashlib.sha256(result.content).hexdigest() == receipt["content_sha256"]
            assert result.text.count("Глава ") == 3
            bad=await client.get(root+"/export.txt",headers=BOB,params={
                **query,"expected_content_sha256":"f"*64,
            })
            assert bad.status_code == 409
    asyncio.run(scenario())

    with repo._connect() as con:
        con.execute(
            "UPDATE source_collect_items SET body=? "
            "WHERE job_id=? AND ordinal=2",("TAMPERED TEXT",jid),
        )
    async def corrupted():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url="http://test",
        ) as client:
            path=f"/v1/source-books/shared/{jid}/chapters/2"
            assert (await client.get(path,headers=BOB)).status_code == 409
            assert (await client.get(path,headers=BOB)).json()["detail"].find(
                "checksum") >= 0
            preview=await client.get(
                f"/v1/source-books/shared/{jid}/assembly",headers=BOB,
            )
            assert preview.status_code == 200 and preview.json()["ready"] is False
    asyncio.run(corrupted())


def test_only_checkpointed_chapters_are_shared_and_deletion_revokes_reader(tmp_path):
    app,repo,jobs,jid=fixture(tmp_path)
    new=jobs.create("bob","cat2",catalog_from_html("royalroad",BOOK,TOC),[1])
    reader=SourceTeamReader(repo)
    assert reader.list_recent("alice") == []
    with pytest.raises(SourceJobMissing):
        reader.chapters(jid)
    assert reader.list_recent("bob") == []
    checkpoint(jobs,jid,1)
    source=reader.list_recent("bob",limit=10)
    assert len(source)==1 and source[0]["can_manage"] is False
    assert reader.list_recent("alice")[0]["can_manage"] is True
    assert reader.chapters(jid,offset=1)["chapters"] == []
    assert reader.chapter(jid,1)["owner_member_id"] == "alice"
    with pytest.raises(SourceJobMissing):
        reader.chapter(jid,2)

    jobs.cancel("alice",jid)
    assert jobs.get("alice",jid)["status"] == "cancelled"
    assert reader.chapter(jid,1)["ordinal"] == 1
    jobs.delete_finished("alice",jid)
    with pytest.raises(SourceJobMissing):
        reader.chapter(jid,1)
    assert reader.list_recent("bob") == []
    assert jobs.get("bob",new["job_id"])["status"] == "queued"


def test_team_reader_checks_manifest_and_stored_identity_before_revealing_text(tmp_path):
    app,repo,jobs,jid=fixture(tmp_path)
    checkpoint(jobs,jid,1)
    with repo._connect() as con:
        con.execute(
            "UPDATE source_collect_items SET source_id='forged' WHERE job_id=? AND ordinal=1",
            (jid,),
        )
    with pytest.raises(Exception,match="identity"):
        SourceTeamReader(repo).chapter(jid,1)
    with repo._connect() as con:
        con.execute(
            "UPDATE source_collect_items SET source_id=? WHERE job_id=? AND ordinal=1",
            ("90001",jid),
        )
        con.execute(
            "UPDATE source_collect_jobs SET manifest_json='[]' WHERE id=?", (jid,),
        )
    with pytest.raises(Exception,match="digest"):
        SourceTeamReader(repo).chapter(jid,1)


def test_disabled_or_bad_team_keys_cannot_read_originals(tmp_path):
    app,repo,jobs,jid = fixture(tmp_path)
    checkpoint(jobs,jid,1)
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url="http://test",
        ) as client:
            for key in ("wrong",""):
                assert (await client.get(
                    f"/v1/source-books/shared/{jid}/chapters/1",
                    headers={"X-API-Key":key})).status_code == 401
    asyncio.run(scenario())
