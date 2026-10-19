"""RE-02 durable team archive: exact identities, dedup and safe deletion.

All source texts are synthetic; no network or paid translation is performed.
"""
from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_collect import catalog_from_html
from chaptera_server.source_job_store import SourceJobStore, SourceJobConflict, SourceJobMissing
from chaptera_server.source_original_archive import (
    SourceOriginalArchive, SourceArchiveMissing, SourceArchiveConflict,
)
from chaptera_server.source_team_reader import SourceTeamReader
from chaptera_server.storage import QueueStore

BOOK = "https://www.royalroad.com/fiction/142007/aura"
TOC = '<div id="chapters">' + "".join(
    f'<a href="/fiction/142007/aura/chapter/{60000+n}/{n}-part">{n} - Part {n}</a>'
    for n in (1,2,3)
) + "</div>"
TEXT = "Synthetic public-domain-like example text. " * 12


def catalog():
    return catalog_from_html("royalroad",BOOK,TOC)


def make_job(jobs,actor,chapter=1,body=TEXT):
    job = jobs.create(actor,"archive-"+actor,catalog(),[chapter])
    claimed=jobs.claim()
    assert claimed and claimed["job_id"] == job["job_id"]
    assert claimed["chapter"]["ordinal"] == chapter
    jobs.finish(job["job_id"],claimed["lease_token"],body=body)
    return job["job_id"]


def only_revision(archive):
    listing=archive.list_books()
    assert len(listing["books"])==1
    book=listing["books"][0]
    chapters=archive.chapters(book["archive_book_id"])["chapters"]
    assert len(chapters)==1
    return chapters[0]


def test_job_deletion_preserves_original_and_other_member_can_read(tmp_path):
    repo=QueueStore(tmp_path/"jobs.sqlite3")
    jobs=SourceJobStore(repo)
    jid=make_job(jobs,"alice")
    archive=SourceOriginalArchive(repo)
    assert archive.list_books()["books"] == []
    out=jobs.delete_finished("alice",jid)
    assert out["deleted"] is True
    assert out["archived_verified_chapters"]==1
    assert out["archive_new_unique_bodies"]==1
    assert out["archive_added_unique_bytes"]==len(TEXT.encode("utf-8"))
    with pytest.raises(SourceJobMissing):
        SourceTeamReader(repo).chapter(jid,1)
    rev=only_revision(archive)
    assert rev["ordinal"]==1
    assert rev["collector_member_id"]=="alice"
    read=SourceOriginalArchive(QueueStore(repo.db_path)).chapter(rev["revision_id"])
    assert read["body"]==TEXT
    assert read["sha256"]==hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
    assert jobs.usage("alice")["actor_used_bytes"]==0
    # "Deleted job" and "archived shared original" are deliberately separate.
    assert jobs.list_recent("alice")==[]


def test_two_moderators_same_revision_deduplicate_body_across_deletions(tmp_path):
    repo=QueueStore(tmp_path/"dedup.db")
    jobs=SourceJobStore(repo)
    first=make_job(jobs,"alice")
    result=jobs.delete_finished("alice",first)
    assert result["archive_new_unique_bodies"]==1
    second=make_job(jobs,"bob")
    result2=jobs.delete_finished("bob",second)
    assert result2["archived_verified_chapters"]==1
    assert result2["archive_new_unique_bodies"]==0
    assert result2["archive_added_unique_bytes"]==0
    archive=SourceOriginalArchive(repo)
    rev=only_revision(archive)
    assert archive.chapter(rev["revision_id"])["body"]==TEXT
    with repo._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM source_original_bodies").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM source_original_revisions").fetchone()[0] == 1


def test_different_revisions_of_same_source_identity_never_overwrite(tmp_path):
    repo=QueueStore(tmp_path/"forks.db")
    jobs=SourceJobStore(repo)
    first=make_job(jobs,"alice",body="First synthetic version of this chapter. "*16)
    jobs.delete_finished("alice",first)
    second=make_job(jobs,"bob",body="Second synthetic version with a different meaning. "*14)
    jobs.delete_finished("bob",second)
    archive=SourceOriginalArchive(repo)
    book=archive.list_books()["books"][0]
    revisions=archive.chapters(book["archive_book_id"])["chapters"]
    assert len(revisions)==2
    assert len({r["revision_id"] for r in revisions})==2
    assert len({archive.chapter(r["revision_id"])["body"] for r in revisions})==2
    with repo._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM source_original_bodies").fetchone()[0]==2


def test_corruption_fails_closed_preserving_job_and_rolling_back_archive(tmp_path):
    repo=QueueStore(tmp_path/"corrupt.db")
    jobs=SourceJobStore(repo)
    jid=make_job(jobs,"alice")
    with repo._connect() as con:
        con.execute("UPDATE source_collect_items SET body='tampered' WHERE job_id=?",
                    (jid,))
    with pytest.raises(SourceJobConflict,match="integrity"):
        jobs.delete_finished("alice",jid)
    assert jobs.get("alice",jid)["status"]=="complete"
    with repo._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM source_original_bodies").fetchone()[0]==0
    with pytest.raises(SourceArchiveMissing):
        SourceOriginalArchive(repo).chapter("0"*64)


def test_archive_quota_denies_deletion_instead_of_losing_shared_text(tmp_path):
    repo=QueueStore(tmp_path/"quota.db")
    jobs=SourceJobStore(repo)
    archive=jobs.archive
    archive.max_bytes=1024*1024
    first=make_job(jobs,"alice",body="A"*600_000)
    jobs.delete_finished("alice",first)
    second=make_job(jobs,"bob",body="B"*600_000)
    with pytest.raises(SourceJobConflict,match="quota exceeded"):
        jobs.delete_finished("bob",second)
    assert jobs.get("bob",second)["status"]=="complete"
    assert SourceOriginalArchive(repo).list_books()["books"][0]["chapter_count"]==1
    with repo._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM source_original_bodies").fetchone()[0]==1


def test_legacy_prearchive_source_jobs_are_adopted_only_on_explicit_delete(tmp_path):
    repo=QueueStore(tmp_path/"legacy.db")
    jobs=SourceJobStore(repo)
    jid=make_job(jobs,"alice")
    with repo._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM source_original_revisions").fetchone()[0]==0
    restarted=SourceJobStore(QueueStore(repo.db_path))
    assert restarted.get("alice",jid)["status"]=="complete"
    restarted.delete_finished("alice",jid)
    assert only_revision(restarted.archive)["collector_member_id"]=="alice"


def test_auth_team_reads_and_only_admin_can_purge_archive_revision(tmp_path):
    path=tmp_path/"api.db"
    app=create_app(Settings(
        db_path=path,
        team_tokens={"alice":"alice-key","bob":"bob-key","operator":"admin-key"},
        admin_member_id="operator",
    ))
    jobs=app.state.source_job_store
    jid=make_job(jobs,"alice")
    jobs.delete_finished("alice",jid)
    revision=only_revision(jobs.archive)
    base="/v1/source-books/archive"
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url="http://test",
        ) as client:
            for target in (base,base+"/"+revision["archive_book_id"],
                           base+"/revisions/"+revision["revision_id"]):
                assert (await client.get(target)).status_code==401
            root=await client.get(base,headers={"X-API-Key":"bob-key"})
            assert root.status_code==200,root.text
            assert root.headers["cache-control"]=="no-store, private"
            assert root.json()["books"][0]["chapter_count"]==1
            index=await client.get(base+"/"+revision["archive_book_id"],
                                   headers={"X-API-Key":"bob-key"})
            assert index.status_code==200
            assert index.json()["chapters"][0]["revision_id"]==revision["revision_id"]
            chapter=await client.get(
                base+"/revisions/"+revision["revision_id"],
                headers={"X-API-Key":"bob-key"},
            )
            assert chapter.status_code==200
            assert chapter.headers["x-content-type-options"]=="nosniff"
            assert chapter.json()["body"]==TEXT
            denied=await client.delete(
                base+"/revisions/"+revision["revision_id"],
                headers={"X-API-Key":"bob-key"},
            )
            assert denied.status_code==403
            removed=await client.delete(
                base+"/revisions/"+revision["revision_id"],
                headers={"X-API-Key":"admin-key"},
            )
            assert removed.status_code==200,removed.text
            assert removed.json()["purged"] is True
            assert removed.json()["removed_orphan_body"] is True
            assert (await client.get(
                base+"/revisions/"+revision["revision_id"],
                headers={"X-API-Key":"bob-key"},
            )).status_code==404
    asyncio.run(scenario())
    with app.state.store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM source_original_bodies").fetchone()[0]==0
        audit=con.execute(
            "SELECT revision_id,body_sha256,admin_member_id FROM source_original_purge_audit"
        ).fetchone()
        assert audit["revision_id"]==revision["revision_id"]
        assert audit["admin_member_id"]=="operator"
        assert audit["body_sha256"]==revision["body_sha256"]


def test_admin_purge_keeps_body_if_other_revision_uses_same_sha(tmp_path):
    repo=QueueStore(tmp_path/"shared-body.db")
    jobs=SourceJobStore(repo)
    first=make_job(jobs,"alice",chapter=1)
    jobs.delete_finished("alice",first)
    second=make_job(jobs,"bob",chapter=2)
    jobs.delete_finished("bob",second)
    archive=SourceOriginalArchive(repo)
    book=archive.list_books()["books"][0]
    revisions=archive.chapters(book["archive_book_id"])["chapters"]
    assert len(revisions)==2
    a,b=revisions
    assert a["body_sha256"]==b["body_sha256"]
    purged=archive.purge_revision(a["revision_id"],admin_member_id="operator")
    assert purged["removed_orphan_body"] is False
    assert archive.chapter(b["revision_id"])["body"]==TEXT
    assert archive.purge_revision(b["revision_id"],admin_member_id="operator")["removed_orphan_body"] is True


def test_tampered_archive_body_rejected_on_read_without_leaking_contents(tmp_path):
    repo=QueueStore(tmp_path/"tamper-archive.db")
    jobs=SourceJobStore(repo)
    jid=make_job(jobs,"alice")
    jobs.delete_finished("alice",jid)
    archive=SourceOriginalArchive(repo)
    rev=only_revision(archive)
    with repo._connect() as con:
        con.execute("UPDATE source_original_bodies SET body='forged'"
                    " WHERE sha256=?", (rev["body_sha256"],))
    with pytest.raises(SourceArchiveConflict,match="checksum"):
        archive.chapter(rev["revision_id"])
