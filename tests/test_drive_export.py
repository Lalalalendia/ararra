"""RE-09A: two existing Drive folders, safe retries, OAuth and stale input."""
from __future__ import annotations

import json
import re
import sys
from urllib.parse import parse_qs

import httpx
import pytest

from chaptera_server.drive_export import (
    DriveExportError, GoogleDriveClient, export_completed_job, read_completed_job,
)
from chaptera_server.drive_acceptance import run_drive_acceptance
from chaptera_server.library import BookLibrary
from chaptera_server.storage import QueueStore

SOURCES = "1daqCzEVgPdX639YV5otTLxgVTPbfeBEp"
TRANSLATIONS = "1X2ZP8Fzbnq5MEAWJyGSErVGGFkfGBrR9"


class FakeDrive:
    def __init__(self):
        self.files = {
            SOURCES:{"id":SOURCES,"name":"Источники",
                "mimeType":"application/vnd.google-apps.folder","parents":["root"],
                "capabilities":{"canAddChildren":True},"appProperties":{}},
            TRANSLATIONS:{"id":TRANSLATIONS,"name":"Переводы",
                "mimeType":"application/vnd.google-apps.folder","parents":["root"],
                "capabilities":{"canAddChildren":True},"appProperties":{}},
        }
        self.bodies = {}
        self.next_id = 0
        self.creates = []
        self.bad_token = False
        self.incomplete_search = False
        self.fail_translation_once = False

    def make(self, name, parent, *, mime="text/plain", props=None, body=b""):
        self.next_id += 1
        fid = f"fakeFileId1234567890{self.next_id:06d}"
        data = {"id":fid,"name":name,"mimeType":mime,"parents":[parent],
                "appProperties":dict(props or {})}
        self.files[fid] = data
        self.bodies[fid] = body
        self.creates.append(fid)
        return data

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/token":
            if self.bad_token:
                return httpx.Response(401,json={"error":"invalid_grant"})
            return httpx.Response(200,json={"access_token":"fake-secret",
                                            "token_type":"Bearer","expires_in":3600})
        assert request.headers.get("authorization") == "Bearer fake-secret"
        if path == "/drive/v3/files" and request.method == "GET":
            q = request.url.params.get("q","")
            m=re.fullmatch(r"'([A-Za-z0-9_-]+)' in parents and trashed = false",q)
            assert m, q
            rows=[dict(x) for x in self.files.values()
                  if m.group(1) in x.get("parents",[])]
            return httpx.Response(200,json={
                "files":rows,"incompleteSearch":self.incomplete_search,
            })
        if path.startswith("/drive/v3/files/") and request.method == "GET":
            fid=path.rsplit("/",1)[-1]
            file=self.files.get(fid)
            if file and request.url.params.get("alt")=="media":
                return httpx.Response(200,content=self.bodies[fid],
                                      headers={"Content-Type":"text/plain"})
            return httpx.Response(200,json=file) if file else httpx.Response(404)
        if path == "/drive/v3/files" and request.method == "POST":
            obj=json.loads(request.content)
            created=self.make(obj["name"],obj["parents"][0],
                mime=obj["mimeType"],props=obj.get("appProperties",{}))
            return httpx.Response(200,json=created)
        if path.startswith("/upload/drive/v3/files/") and request.method=="PATCH":
            fid=path.rsplit("/",1)[-1]
            if fid not in self.files:
                return httpx.Response(404)
            assert request.url.params.get("uploadType")=="media"
            assert self.files[fid]["mimeType"]=="text/plain"
            self.bodies[fid]=request.content
            return httpx.Response(200,json={
                "id":fid,"mimeType":self.files[fid]["mimeType"],
            })
        if path == "/upload/drive/v3/files" and request.method == "POST":
            assert request.url.params["uploadType"] == "multipart"
            payload=request.content
            info=re.search(rb"Content-Type: application/json; charset=UTF-8\r\n\r\n(.*?)\r\n--readera",
                           payload,re.S)
            assert info,"multipart metadata missing"
            obj=json.loads(info.group(1))
            if self.fail_translation_once and obj["appProperties"]["readeraPart"]=="translation":
                self.fail_translation_once=False
                return httpx.Response(503,json={"error":"temporary"})
            pre=b"Content-Type: text/plain; charset=UTF-8\r\n\r\n"
            raw=payload.split(pre,1)[1].rsplit(b"\r\n--readera",1)[0]
            created=self.make(obj["name"],obj["parents"][0],
                mime=obj["mimeType"],props=obj["appProperties"],body=raw)
            return httpx.Response(200,json=created)
        raise AssertionError(f"Unexpected HTTP {request.method} {path}")

    def client(self):
        return GoogleDriveClient(
            client_id="fake-google-client-id",client_secret="fake-secret",
            refresh_token="fake-refresh",
            http=httpx.Client(transport=httpx.MockTransport(self),
                              trust_env=False, follow_redirects=False),
        )


def make_job(tmp_path, *, completed=True):
    db=QueueStore(tmp_path/"drive.sqlite3")
    lib=BookLibrary(db)
    lib.create_book("book-1","Проверка <романа> / том 1",
                    owner_member_id="moderator",actor_id="administrator")
    chapter=lib.put_chapter("book-1","chapter-1",chapter_index=1,
        chapter_path="chapters/1.xhtml",
        source_html="<p>Source original text.</p>",expected_revision=0)
    spec=lib.new_job("book-1","chapter-1",
        source_language="en",target_language="Russian",
        style_instructions="",max_attempts=1)
    job=db.create_job("moderator",spec)
    if completed:
        claim=db.claim_next()
        assert claim is not None and claim[0]["id"]==job["id"]
        assert db.complete(job["id"],claim[1],
            "<p>Машинный перевод — до принятия человеком.</p>",
            chapter["source_hash"],1)
    return db,lib,job["id"]


def run_export(db,job,service):
    return export_completed_job(
        db,service.client(),job,
        source_folder_id=SOURCES,translation_folder_id=TRANSLATIONS,
    )


def test_two_folder_upload_and_repeat_are_idempotent(tmp_path):
    db,_,jid=make_job(tmp_path)
    service=FakeDrive()
    receipt=run_export(db,jid,service)
    assert receipt["status"]=="exported_or_already_present"
    assert receipt["source_file_id"] != receipt["translation_file_id"]
    assert len(service.creates)==4  # two book folders + two plain TXT
    source=service.bodies[receipt["source_file_id"]]
    ru=service.bodies[receipt["translation_file_id"]]
    assert source==b"<p>Source original text.</p>"
    assert "Машинный перевод" in ru.decode("utf-8")
    assert all(service.files[fid]["name"]=="Глава 001.txt" for fid in (
        receipt["source_file_id"],receipt["translation_file_id"]))
    assert service.files[receipt["source_file_id"]]["parents"]!=(
        service.files[receipt["translation_file_id"]]["parents"])
    folders=[service.files[k] for k in service.creates
             if service.files[k]["mimeType"]=="application/vnd.google-apps.folder"]
    assert len(folders)==2
    assert all("Проверка романа том 1" in x["name"] for x in folders)
    assert run_export(db,jid,service)==receipt
    assert len(service.creates)==4


def test_both_chatgpt_edits_remain_after_repeat(tmp_path):
    db,_,jid=make_job(tmp_path)
    service=FakeDrive()
    receipt=run_export(db,jid,service)
    src_id,ru_id=receipt["source_file_id"],receipt["translation_file_id"]
    service.bodies[src_id]=b"<p>ChatGPT fixed original.</p>"
    service.bodies[ru_id]="<p>Исправленный перевод ChatGPT.</p>".encode()
    # Both the ordinary book folders and TXT files remain user-editable.
    # Renaming them must not make the exporter create shadow copies.
    service.files[src_id]["name"]="Исправленный оригинал.txt"
    service.files[ru_id]["name"]="Ручная редакция.txt"
    for fid in service.creates:
        if service.files[fid]["mimeType"]=="application/vnd.google-apps.folder":
            service.files[fid]["name"]="Моя папка — "+service.files[fid]["name"]
    again=run_export(db,jid,service)
    assert again==receipt
    assert service.bodies[src_id]==b"<p>ChatGPT fixed original.</p>"
    assert "Исправленный перевод" in service.bodies[ru_id].decode()
    assert len(service.creates)==4


def test_partial_upload_is_resumable_without_third_file(tmp_path):
    db,_,jid=make_job(tmp_path)
    service=FakeDrive()
    service.fail_translation_once=True
    with pytest.raises(DriveExportError,match="503"):
        run_export(db,jid,service)
    assert len(service.creates)==3  # both book dirs and original TXT
    result=run_export(db,jid,service)
    assert len(service.creates)==4
    assert service.files[result["source_file_id"]]["appProperties"]["readeraPart"]=="source"
    assert service.files[result["translation_file_id"]]["appProperties"]["readeraPart"]=="translation"


def test_foreign_file_and_incomplete_listing_fail_closed(tmp_path):
    db,_,jid=make_job(tmp_path)
    service=FakeDrive()
    first=run_export(db,jid,service)
    source_id=first["source_file_id"]
    folder=service.files[source_id]["parents"][0]
    service.make("Глава 001.txt",folder,body=b"foreign contents")
    with pytest.raises(DriveExportError,match="Duplicate chapter filenames"):
        run_export(db,jid,service)
    assert service.bodies[source_id]==b"<p>Source original text.</p>"
    service.incomplete_search=True
    with pytest.raises(DriveExportError,match="incomplete"):
        run_export(db,jid,service)


def test_no_export_when_job_incomplete_or_source_or_glossary_changed(tmp_path):
    db,lib,jid=make_job(tmp_path/"pending",completed=False)
    with pytest.raises(DriveExportError,match="not completed"):
        read_completed_job(db,jid)
    completed_db,library,completed_id=make_job(tmp_path/"completed")
    library.put_chapter("book-1","chapter-1",chapter_index=1,
        chapter_path="chapters/1.xhtml",source_html="<p>Updated original</p>",
        expected_revision=1)
    service=FakeDrive()
    with pytest.raises(DriveExportError,match="changed"):
        run_export(completed_db,completed_id,service)
    assert not service.creates
    stable_db,stable_lib,stable_id=make_job(tmp_path/"glossary")
    stable_lib.put_glossary("book-1",glossary={"a":"б"},versions={},
        authority="READY",expected_revision=0,actor_id="moderator")
    with pytest.raises(DriveExportError,match="changed"):
        run_export(stable_db,stable_id,FakeDrive())


def test_oauth_or_parent_failure_and_dry_run_have_no_writes(tmp_path,monkeypatch,capsys):
    db,_,jid=make_job(tmp_path)
    bad=FakeDrive()
    bad.bad_token=True
    with pytest.raises(DriveExportError,match="OAuth"):
        run_export(db,jid,bad)
    assert not bad.creates
    deny=FakeDrive()
    deny.files[TRANSLATIONS]["capabilities"]["canAddChildren"]=False
    with pytest.raises(DriveExportError,match="not writable"):
        run_export(db,jid,deny)
    assert not deny.creates
    # Dry-run consults SQLite only and requires no Google OAuth credentials.
    from chaptera_server import cli
    monkeypatch.setenv("CHAPTERA_DB_PATH",str(db.db_path))
    monkeypatch.setattr(sys,"argv",["chaptera-server","export-drive","--job-id",jid,"--dry-run"])
    cli.main()
    meta=json.loads(capsys.readouterr().out)
    assert meta["dry_run"] and meta["network_used"] is False
    assert len(meta["source_sha256"])==len(meta["translation_sha256"])==64


def test_drive_acceptance_preflight_is_read_only():
    service=FakeDrive()
    drive=service.client()
    try:
        status=run_drive_acceptance(
            drive,source_folder_id=SOURCES,
            translation_folder_id=TRANSLATIONS,live=False,
        )
    finally:
        drive.http.close()
    assert status["stage"]=="read_only_preflight"
    assert status["drive_mutations"]==0
    assert service.creates==[]
    assert set(service.files)=={SOURCES,TRANSLATIONS}


def test_drive_acceptance_full_synthetic_edition_and_retry_isolated():
    service=FakeDrive()
    drive=service.client()
    try:
        status=run_drive_acceptance(
            drive,source_folder_id=SOURCES,
            translation_folder_id=TRANSLATIONS,live=True,
        )
    finally:
        drive.http.close()
    assert status["stage"]=="live_two_folder_export_verified"
    assert status["both_edits_preserved"] and status["file_ids_unchanged"]
    assert status["no_gemini_requests"] and status["production_db_unchanged"]
    assert len(service.creates)==4
    for part in ("source","translation"):
        root = SOURCES if part=="source" else TRANSLATIONS
        folder_id=status[f"{part}_folder_id"]
        file_id=status[f"{part}_file_id"]
        folder=service.files[folder_id]
        file=service.files[file_id]
        assert folder["parents"]==[root]
        assert folder["name"].startswith("__RE09A_SMOKE_")
        assert file["parents"]==[folder_id]
        assert file["name"]=="Глава 001.txt"
        assert "readeraJobId" in file["appProperties"]
        assert b"RE09A smoke edit-in-place" in service.bodies[file_id]
    assert status["source_file_id"]!=status["translation_file_id"]


def test_drive_acceptance_refuses_invalid_destinations_before_mutation():
    service=FakeDrive()
    service.files[TRANSLATIONS]["capabilities"]["canAddChildren"]=False
    drive=service.client()
    try:
        with pytest.raises(DriveExportError,match="not writable"):
            run_drive_acceptance(
                drive,source_folder_id=SOURCES,
                translation_folder_id=TRANSLATIONS,live=True,
            )
    finally:
        drive.http.close()
    assert service.creates==[]
