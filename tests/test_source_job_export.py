"""RE-02 source book TXT export: exact range coverage and signed-by-digest file."""
from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_collect import catalog_from_html
from chaptera_server.source_job_export import VerifiedSourceTXT, SourceExportNotReady
from chaptera_server.source_job_store import SourceJobStore
from chaptera_server.storage import QueueStore

BOOK = "https://www.royalroad.com/fiction/142007/aura"
TOC = '<div id="chapters">' + "".join(
    f'<a href="/fiction/142007/aura/chapter/{70000+n}/{n}-part">{n} - Part {n}</a>'
    for n in range(1, 4)
) + "</div>"
AUTH = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


def setup(tmp_path, *, outcomes=("ok", "ok", "ok")):
    repo = QueueStore(tmp_path / "source-export.sqlite3")
    jobs = SourceJobStore(repo)
    catalog = catalog_from_html("royalroad", BOOK, TOC)
    job = jobs.create("alice", "catalog-export-1", catalog, [1, 2, 3])
    for n, outcome in enumerate(outcomes, 1):
        if outcome == "pending":
            break
        claim = jobs.claim()
        assert claim is not None
        assert claim["chapter"]["ordinal"] == n
        if outcome == "failed":
            jobs.finish(job["job_id"], claim["lease_token"], error="blocked: access required")
        else:
            body = (f"Original source paragraph for chapter {n}. " * 10) + (
                "\n\nChapter-like line is still body, not a new chapter."
            )
            jobs.finish(job["job_id"], claim["lease_token"], body=body)
    return repo, jobs, job["job_id"]


def test_complete_report_is_content_free_and_export_matches_exact_bytes(tmp_path):
    repo, jobs, jid = setup(tmp_path)
    exporter = VerifiedSourceTXT(repo)
    report = exporter.inspect("alice", jid)
    assert report["ready"] is True
    assert report["scope"] == "selected_catalog_range_only"
    assert report["selected_count"] == report["verified_count"] == 3
    assert report["issue_count"] == 0
    assert report["content_bytes"] > 1000
    assert len(report["content_sha256"]) == 64
    assert "Original source paragraph" not in str(report)

    response, result = exporter.render(
        "alice", jid, report["manifest_sha256"], report["content_sha256"],
    )
    try:
        data = result.file.read()
        assert len(data) == result.byte_length == report["content_bytes"]
        assert hashlib.sha256(data).hexdigest() == report["content_sha256"]
        text = data.decode("utf-8")
        assert text.count("Глава 1.") == 1
        assert text.index("Глава 1.") < text.index("Глава 2.") < text.index("Глава 3.")
        assert "Chapter-like line is still body" in text
        assert text.endswith("\n")
        assert response == report
    finally:
        result.file.close()
    with pytest.raises(SourceExportNotReady):
        exporter.render("alice", jid, report["manifest_sha256"], "0" * 64)


def test_failed_and_pending_items_never_become_a_complete_export(tmp_path):
    for outcomes,expected in [
        (("ok", "failed", "ok"), ("failed", 1, 0)),
        (("ok", "pending", "pending"), ("missing", 0, 2)),
    ]:
        path = tmp_path / str(outcomes[1])
        path.mkdir()
        repo, _, jid = setup(path, outcomes=outcomes)
        exporter = VerifiedSourceTXT(repo)
        report = exporter.inspect("alice", jid)
        assert report["ready"] is False
        assert report["content_sha256"] is None
        assert report["failed_count"] == expected[1]
        assert report["missing_count"] == expected[2]
        assert any(issue["code"] == expected[0] for issue in report["issues"])
        assert "Original source paragraph" not in str(report)
        with pytest.raises(SourceExportNotReady):
            exporter.render("alice", jid, report["manifest_sha256"], "0" * 64)


def test_stale_receipt_and_tampered_text_are_rejected(tmp_path):
    repo, _, jid = setup(tmp_path)
    exporter = VerifiedSourceTXT(repo)
    clean = exporter.inspect("alice", jid)
    with repo._connect() as con:
        con.execute(
            "UPDATE source_collect_items SET body=? WHERE job_id=? AND ordinal=2",
            ("Source contents maliciously replaced", jid),
        )
    broken = exporter.inspect("alice", jid)
    assert broken["ready"] is False
    assert broken["damaged_count"] == 1
    assert any(issue["code"] == "corrupted" for issue in broken["issues"])
    with pytest.raises(SourceExportNotReady):
        exporter.render("alice", jid, clean["manifest_sha256"], clean["content_sha256"])

    # Even after both content and stored SHA are rewritten, the preview
    # digest is different and a prior exact-byte export receipt must fail.
    new_body = "A different but internally consistent replacement. " * 12
    with repo._connect() as con:
        con.execute(
            "UPDATE source_collect_items SET body=?,sha256=?,bytes=? "
            "WHERE job_id=? AND ordinal=2",
            (new_body, hashlib.sha256(new_body.encode()).hexdigest(),
             len(new_body.encode()), jid),
        )
    updated = exporter.inspect("alice", jid)
    assert updated["ready"] is True
    assert updated["content_sha256"] != clean["content_sha256"]
    with pytest.raises(SourceExportNotReady):
        exporter.render("alice", jid, clean["manifest_sha256"], clean["content_sha256"])


def test_out_of_manifest_checkpoints_are_reported(tmp_path):
    repo, _, jid = setup(tmp_path)
    with repo._connect() as con:
        con.execute(
            "INSERT INTO source_collect_items("
            "job_id,item_index,ordinal,source_id,url,status,body,sha256,error,bytes,saved_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (jid, 99, 99, "bad", "https://www.royalroad.com/fiction/142007/aura/",
             "failed", None, None, "manipulated checkpoint", 0, 1.0),
        )
    report = VerifiedSourceTXT(repo).inspect("alice", jid)
    assert report["ready"] is False
    assert report["damaged_count"] == 1
    assert report["issue_count"] >= 1
    assert any(issue["code"] == "unexpected_checkpoint" for issue in report["issues"])


def test_oversized_txt_is_refused_without_losing_source_checkpoints(tmp_path, monkeypatch):
    from chaptera_server import source_job_export as module
    repo, jobs, jid = setup(tmp_path)
    monkeypatch.setattr(module, "MAX_SOURCE_TXT_BYTES", 400)
    report = VerifiedSourceTXT(repo).inspect("alice", jid)
    assert report["ready"] is False
    assert report["content_sha256"] is None
    assert any(issue["code"] == "too_large" for issue in report["issues"])
    assert jobs.get("alice", jid)["completed"] == 3


def test_original_txt_http_auth_integrity_and_status(tmp_path):
    repo, jobs, jid = setup(tmp_path)
    app = create_app(Settings(
        db_path=repo.db_path,
        team_tokens={"alice": "alice-key", "bob": "bob-key"},
    ), repo)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            path=f"/v1/source-books/jobs/{jid}"
            assert (await client.get(path + "/assembly")).status_code == 401
            assert (await client.get(path + "/assembly", headers=BOB)).status_code == 404
            preview=await client.get(path + "/assembly", headers=AUTH)
            assert preview.status_code == 200, preview.text
            report=preview.json()
            assert report["ready"]
            params={
                "expected_manifest_sha256": report["manifest_sha256"],
                "expected_content_sha256": report["content_sha256"],
            }
            assert (await client.get(path + "/export.txt", params=params)).status_code == 401
            assert (await client.get(path + "/export.txt", params=params,
                                     headers=BOB)).status_code == 404
            result=await client.get(path + "/export.txt", params=params, headers=AUTH)
            assert result.status_code == 200, result.text[:300]
            assert result.headers["cache-control"] == "no-store, private"
            assert result.headers["x-content-type-options"] == "nosniff"
            assert result.headers["x-readera-source-scope"] == "selected_catalog_range_only"
            assert result.headers["x-readera-manifest-sha256"] == report["manifest_sha256"]
            assert result.headers["x-readera-content-sha256"] == report["content_sha256"]
            assert len(result.content) == int(result.headers["content-length"])
            assert hashlib.sha256(result.content).hexdigest() == report["content_sha256"]
            assert result.headers["content-disposition"].endswith(".txt\"")
            stale=await client.get(path + "/export.txt", headers=AUTH, params={
                **params, "expected_content_sha256": "0" * 64,
            })
            assert stale.status_code == 409
            assert "Original source paragraph" not in stale.text
    asyncio.run(scenario())
