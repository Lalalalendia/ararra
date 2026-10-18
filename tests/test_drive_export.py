"""RE-09A: two existing Drive folders, safe retries, OAuth and stale input."""
from __future__ import annotations

import json
import re
import sqlite3
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
    assert len(service.creates)==6  # two moderator dirs + two book dirs + two TXT
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
    assert len(folders)==4
    moderator_dirs = [x for x in folders
                      if x["appProperties"].get("readeraModeratorKey") and
                         not x["appProperties"].get("readeraBookKey")]
    book_dirs = [x for x in folders if x["appProperties"].get("readeraBookKey")]
    assert len(moderator_dirs)==len(book_dirs)==2
    assert {x["name"] for x in moderator_dirs}=={"moderator"}
    assert all("Проверка романа том 1" in x["name"] for x in book_dirs)
    assert {tuple(x["parents"]) for x in moderator_dirs}=={(SOURCES,), (TRANSLATIONS,)}
    assert {x["parents"][0] for x in book_dirs}=={x["id"] for x in moderator_dirs}
    assert run_export(db,jid,service)==receipt
    assert len(service.creates)==6


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
    assert len(service.creates)==6


def test_partial_upload_is_resumable_without_third_file(tmp_path):
    db,_,jid=make_job(tmp_path)
    service=FakeDrive()
    service.fail_translation_once=True
    with pytest.raises(DriveExportError,match="503"):
        run_export(db,jid,service)
    assert len(service.creates)==5  # moderator + book on both sides, original TXT
    result=run_export(db,jid,service)
    assert len(service.creates)==6
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
    assert len(service.creates)==6
    for part in ("source","translation"):
        root = SOURCES if part=="source" else TRANSLATIONS
        folder_id=status[f"{part}_folder_id"]
        file_id=status[f"{part}_file_id"]
        folder=service.files[folder_id]
        file=service.files[file_id]
        moderator_id=folder["parents"][0]
        moderator=service.files[moderator_id]
        assert moderator["parents"]==[root]
        assert moderator["name"]=="drive-smoke"
        assert moderator["appProperties"].get("readeraModeratorKey")
        assert folder["appProperties"]["readeraModeratorKey"] == moderator["appProperties"]["readeraModeratorKey"]
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


@pytest.mark.parametrize("fail_translation", [False, True])
def test_drive_acceptance_closes_sqlite_connections_before_temp_cleanup(
    monkeypatch, fail_translation,
):
    """Retain SQLite handles to reproduce WinError 32 deterministically.

    The normal SQLite transaction context manager does not close connections.
    A retained handle would block TemporaryDirectory on Windows and fail the
    explicit closed-handle assertion on Linux.
    """
    original_connect = QueueStore._connect
    opened = []

    def track_connections(self):
        connection = original_connect(self)
        opened.append(connection)
        return connection

    monkeypatch.setattr(QueueStore, "_connect", track_connections)
    service = FakeDrive()
    service.fail_translation_once = fail_translation
    drive = service.client()
    try:
        if fail_translation:
            # A remote failure must not be masked by temporary-DB cleanup.
            with pytest.raises(DriveExportError, match="503"):
                run_drive_acceptance(
                    drive, source_folder_id=SOURCES,
                    translation_folder_id=TRANSLATIONS, live=True,
                )
        else:
            receipt = run_drive_acceptance(
                drive, source_folder_id=SOURCES,
                translation_folder_id=TRANSLATIONS, live=True,
            )
            assert receipt["stage"] == "live_two_folder_export_verified"
    finally:
        drive.http.close()

    assert opened, "Expected real SQLite connections to be exercised"
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


# RE-09A-04: export work is atomically persisted, but opt-in and isolated.
from chaptera_server.drive_outbox import DriveOutbox


def finish_pending_book_job(db, jid):
    claimed = db.claim_next()
    assert claimed is not None and claimed[0]["id"] == jid
    with db._connect() as con:
        source_hash = con.execute(
            "SELECT source_hash FROM book_chapters WHERE book_id='book-1'"
        ).fetchone()[0]
    assert db.complete(jid, claimed[1], "<p>Машинный перевод.</p>", source_hash, 1)


def test_drive_outbox_default_disabled_no_historical_backfill(tmp_path):
    db,_,jid = make_job(tmp_path / "history")
    outbox = DriveOutbox(db)
    assert outbox.status() == {
        "enabled":False,"pending":0,"running":0,"done":0,"hold":0,
    }
    assert outbox.set_enabled(True)["enabled"] is True
    assert outbox.run_once(lambda _: pytest.fail("No historical Drive writes"))["stage"] == "no_eligible_export"
    assert db.get_job(jid)["status"] == "completed"


def test_drive_outbox_durable_completions_and_edits(tmp_path):
    db,_,jid = make_job(tmp_path / "incoming", completed=False)
    outbox = DriveOutbox(db)
    outbox.set_enabled(True)
    finish_pending_book_job(db,jid)
    assert outbox.status()["pending"] == 1
    # New process / QueueStore reuses the same durable SQLite event.
    restored = DriveOutbox(QueueStore(db.db_path))
    service = FakeDrive()
    first = restored.run_once(lambda job: run_export(db,job,service))
    assert first["stage"] == "exported"
    assert restored.status()["done"] == 1
    source_id = first["source_file_id"]
    translation_id = first["translation_file_id"]
    service.bodies[source_id] = b"edited original"
    service.bodies[translation_id] = b"edited translation"
    assert restored.run_once(lambda _: pytest.fail("No duplicate export"))["stage"] == "no_eligible_export"
    assert service.bodies[source_id] == b"edited original"
    assert service.bodies[translation_id] == b"edited translation"


def test_drive_outbox_partial_failure_requires_review_then_reconcile(tmp_path):
    db,_,jid = make_job(tmp_path / "partial", completed=False)
    outbox = DriveOutbox(db)
    outbox.set_enabled(True)
    finish_pending_book_job(db,jid)
    service = FakeDrive()
    service.fail_translation_once = True
    failed = outbox.run_once(lambda job: run_export(db,job,service))
    assert failed["stage"] == "requires_review"
    assert len(service.creates) == 5
    assert outbox.status()["hold"] == 1
    assert outbox.run_once(lambda _: pytest.fail("Must hold ambiguous outcome"))["stage"] == "no_eligible_export"
    assert outbox.retry_held(jid)
    again = outbox.run_once(lambda job: run_export(db,job,service))
    assert again["stage"] == "exported"
    assert len(service.creates) == 6


def test_drive_outbox_expired_lease_requires_explicit_review(tmp_path):
    db,_,jid = make_job(tmp_path / "crash", completed=False)
    outbox = DriveOutbox(db)
    outbox.set_enabled(True)
    finish_pending_book_job(db,jid)
    assert outbox.claim_next() is not None
    with db._connect() as con:
        con.execute("UPDATE drive_export_outbox SET lease_until=0 WHERE job_id=?", (jid,))
    assert outbox.claim_next() is None
    assert outbox.status()["hold"] == 1
    assert outbox.retry_held(jid)
    assert outbox.status()["pending"] == 1


def test_drive_outbox_refuses_stale_input_without_remote_mutation(tmp_path):
    db,lib,jid = make_job(tmp_path / "stale", completed=False)
    outbox = DriveOutbox(db)
    outbox.set_enabled(True)
    finish_pending_book_job(db,jid)
    lib.put_chapter(
        "book-1","chapter-1",chapter_index=1,
        chapter_path="chapters/1.xhtml",
        source_html="<p>Changed after Gemini.</p>",expected_revision=1,
    )
    remote = FakeDrive()
    receipt = outbox.run_once(lambda job: run_export(db,job,remote))
    assert receipt["stage"] == "requires_review"
    assert remote.creates == []


def test_drive_outbox_refuses_non_book_completion(tmp_path):
    from chaptera_server.storage import JobSpec
    db = QueueStore(tmp_path / "ordinary.sqlite3")
    outbox = DriveOutbox(db)
    outbox.set_enabled(True)
    job = db.create_job("moderator", JobSpec(
        chapter_id="generic", source_html="<p>test</p>", max_attempts=1,
    ))
    claimed = db.claim_next()
    assert claimed is not None
    assert db.complete(job["id"],claimed[1],"<p>ru</p>","a"*64,1)
    assert outbox.status()["pending"] == 0


def test_drive_outbox_expired_lease_blocks_all_other_jobs(tmp_path):
    """Never export a second chapter while an expired owner may still write."""
    db,library,first_id = make_job(tmp_path / "serialized", completed=False)
    outbox = DriveOutbox(db)
    outbox.set_enabled(True)
    second_spec = library.new_job(
        "book-1","chapter-1",
        source_language="en",target_language="Russian",
        style_instructions="",max_attempts=1,
    )
    second_id = db.create_job("another-moderator",second_spec)["id"]
    finish_pending_book_job(db,first_id)
    finish_pending_book_job(db,second_id)
    assert outbox.status()["pending"] == 2
    claim = outbox.claim_next()
    assert claim is not None and claim[0] == first_id
    with db._connect() as con:
        con.execute("UPDATE drive_export_outbox SET lease_until=0 WHERE job_id=?", (first_id,))
    assert outbox.claim_next() is None
    assert outbox.status()["hold"] == 1
    assert outbox.status()["pending"] == 1
    assert outbox.run_once(lambda _: pytest.fail("must not overlap stale Drive writer"))["stage"] == "no_eligible_export"
    assert outbox.retry_held(first_id)
    assert outbox.claim_next() is not None


def test_drive_outbox_retry_held_refuses_active_export(tmp_path):
    db,library,first_id = make_job(tmp_path / "mutex", completed=False)
    outbox = DriveOutbox(db)
    outbox.set_enabled(True)
    second_spec = library.new_job(
        "book-1","chapter-1",
        source_language="en",target_language="Russian",
        style_instructions="",max_attempts=1,
    )
    second_id = db.create_job("another-moderator",second_spec)["id"]
    finish_pending_book_job(db,first_id)
    finish_pending_book_job(db,second_id)
    first = outbox.claim_next()
    assert first and first[0] == first_id
    assert outbox.hold(first_id,first[1],error_code="ambiguous_remote_write")
    second = outbox.claim_next()
    assert second and second[0] == second_id
    assert not outbox.retry_held(first_id)
    assert outbox.status()["hold"] == 1
    assert outbox.finish(second_id,second[1],{"status":"ok"})
    assert outbox.retry_held(first_id)


def _another_completed_book(db, lib, *, book_id, title, owner, actor):
    lib.create_book(book_id,title,owner_member_id=owner,actor_id="administrator")
    chapter = lib.put_chapter(
        book_id, "ch-1",chapter_index=1,chapter_path="chapters/1.xhtml",
        source_html=f"<p>{book_id} source</p>",expected_revision=0,
    )
    spec = lib.new_job(
        book_id,"ch-1",source_language="en",target_language="Russian",
        style_instructions="",max_attempts=1,
    )
    job = db.create_job(actor,spec)
    claim = db.claim_next()
    assert claim and claim[0]["id"] == job["id"]
    assert db.complete(
        job["id"],claim[1],f"<p>{book_id} translation</p>",
        chapter["source_hash"],1,
    )
    return job["id"]


def test_two_books_same_moderator_share_moderator_folder(tmp_path):
    db,lib,jid = make_job(tmp_path)
    service = FakeDrive()
    first = run_export(db,jid,service)
    second_job = _another_completed_book(
        db,lib,book_id="book-2",title="Ещё одна книга",
        owner="moderator",actor="different-worker",
    )
    second = run_export(db,second_job,service)
    for part in ("source","translation"):
        assert first[f"{part}_moderator_folder_id"] == second[f"{part}_moderator_folder_id"]
        assert first[f"{part}_book_folder_id"] != second[f"{part}_book_folder_id"]
        assert service.files[second[f"{part}_book_folder_id"]]["parents"] == [
            first[f"{part}_moderator_folder_id"]
        ]
    assert len(service.creates) == 10  # 2 moderator + 4 book + 4 TXT
    assert run_export(db,second_job,service) == second
    assert len(service.creates) == 10


def test_different_book_owners_get_separate_moderator_folders(tmp_path):
    db,lib,jid = make_job(tmp_path)
    service = FakeDrive()
    original = run_export(db,jid,service)
    different_id = _another_completed_book(
        db,lib,book_id="book-new",title="Книга второго",
        owner="SecondModerator",actor="operator",
    )
    second = run_export(db,different_id,service)
    for part in ("source","translation"):
        assert original[f"{part}_moderator_folder_id"] != second[f"{part}_moderator_folder_id"]
        assert service.files[second[f"{part}_moderator_folder_id"]]["name"] == "SecondModerator"
        assert service.files[second[f"{part}_book_folder_id"]]["parents"] == [
            second[f"{part}_moderator_folder_id"]
        ]
    assert len(service.creates) == 12


def test_book_ownership_reassignment_preserves_old_remote_data(tmp_path):
    db,lib,jid=make_job(tmp_path)
    service=FakeDrive()
    first=run_export(db,jid,service)
    lib.reassign_book_owner(
        "book-1",new_owner_member_id="other",
        expected_owner_revision=0,actor_id="operator",
    )
    before = len(service.creates)
    with pytest.raises(DriveExportError,match="reviewed transfer required"):
        run_export(db,jid,service)
    assert len(service.creates) == before
    assert first["source_file_id"] in service.files
    assert first["translation_file_id"] in service.files


def test_book_reassignment_before_first_export_uses_new_owner(tmp_path):
    db,lib,jid=make_job(tmp_path)
    lib.reassign_book_owner(
        "book-1",new_owner_member_id="other",
        expected_owner_revision=0,actor_id="operator",
    )
    service=FakeDrive()
    result=run_export(db,jid,service)
    assert result["owner_member_id"] == "other"
    assert service.files[result["source_moderator_folder_id"]]["name"] == "other"


def test_legacy_flat_book_folder_blocks_shadow_copy(tmp_path):
    import hashlib
    db,_,jid=make_job(tmp_path)
    service=FakeDrive()
    book_key=hashlib.sha256(b"book-1").hexdigest()[:20]
    service.make(
        "Проверка романа том 1 ["+book_key[:10]+"]",SOURCES,
        mime="application/vnd.google-apps.folder",
        props={"readeraBookKey":book_key},
    )
    before=len(service.creates)
    with pytest.raises(DriveExportError,match="Legacy root-level book"):
        run_export(db,jid,service)
    assert len(service.creates)==before


def test_foreign_moderator_folder_name_blocks_adoption_before_writes(tmp_path):
    db,_,jid=make_job(tmp_path)
    service=FakeDrive()
    service.make("moderator",TRANSLATIONS,
                 mime="application/vnd.google-apps.folder")
    before=len(service.creates)
    with pytest.raises(DriveExportError,match="different identity"):
        run_export(db,jid,service)
    assert len(service.creates)==before


def test_two_managed_moderator_folders_are_ambiguous(tmp_path):
    import hashlib
    db,_,jid=make_job(tmp_path)
    service=FakeDrive()
    key=hashlib.sha256(b"moderator").hexdigest()[:20]
    for name in ("moderator","renamed moderator"):
        service.make(name,SOURCES,mime="application/vnd.google-apps.folder",
                     props={"readeraModeratorKey":key})
    before=len(service.creates)
    with pytest.raises(DriveExportError,match="Duplicate managed"):
        run_export(db,jid,service)
    assert len(service.creates)==before
