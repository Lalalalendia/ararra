"""RE-09A: isolated, *explicit* live Google Drive exporter acceptance.

This tests the real chapter-job-to-two-TXT code using an owner-approved OAuth
client and synthetic chapter only. No Gemini request, production SQLite writes,
publishing, acceptance or automatic deletion of Drive files. The only updates
target the two synthetic TXT files created during THIS invocation.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .drive_export import (
    DriveExportError, FOLDER_MIME, TEXT_MIME, GoogleDriveClient,
    _clean_name, export_completed_job, read_completed_job,
)
from .library import BookLibrary
from .storage import QueueStore


class _AcceptanceQueueStore(QueueStore):
    """Close every temporary SQLite handle before Windows removes smoke.sqlite3.

    A sqlite3.Connection context manager commits or rolls back but does NOT
    close the connection. The shared QueueStore deliberately stays unchanged;
    the isolated acceptance queue owns and closes its own per-call handles.
    """

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = super()._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()


_SOURCE = (
    "<p>RE-09A synthetic original source for a Google Drive integration smoke.</p>"
    "<p>This is not an excerpt from any real book.</p>"
)
_TRANSLATION = (
    "<p>RE-09A контрольный машинный перевод для теста Google Drive.</p>"
    "<p>Никакая настоящая книга не используется.</p>"
)
_TEST_PREFIX = "__RE09A_SMOKE_"
_EDIT_MARKER = "\n[RE09A smoke edit-in-place; synthetic text only]\n"


def _create_synthetic_completed_job(directory: Path) -> tuple[QueueStore, str]:
    """Use a completely independent temporary SQLite queue, never user data."""
    store = _AcceptanceQueueStore(directory / "smoke.sqlite3")
    library = BookLibrary(store)
    identifier = secrets.token_hex(12)
    book_id = f"re09a-smoke-{identifier}"
    title = f"{_TEST_PREFIX}{identifier}_no_real_book"
    library.create_book(
        book_id, title, owner_member_id="drive-smoke",
        actor_id="drive-smoke",
    )
    chapter = library.put_chapter(
        book_id, "test-chapter", chapter_index=1,
        chapter_path="chapters/1.xhtml",
        source_html=_SOURCE, expected_revision=0,
    )
    spec = library.new_job(
        book_id, "test-chapter",
        source_language="en", target_language="Russian",
        style_instructions="", max_attempts=1,
    )
    job = store.create_job("drive-smoke", spec)
    claim = store.claim_next()
    if claim is None or claim[0]["id"] != job["id"]:
        raise DriveExportError("Failed to claim isolated synthetic chapter job")
    if not store.complete(
        job["id"], claim[1], _TRANSLATION, chapter["source_hash"], 1,
    ):
        raise DriveExportError("Failed to complete isolated synthetic chapter job")
    return store, job["id"]


def _read_verified_text(
    drive: GoogleDriveClient, file_id: str, *,
    expected_parent: str, job_id: str, part: str, expected: bytes,
) -> None:
    fid = drive._check_id(file_id)
    meta = drive._json(drive._request(
        "GET", f"https://www.googleapis.com/drive/v3/files/{fid}",
        params={"fields": "id,mimeType,parents,trashed,appProperties"},
    ))
    props = meta.get("appProperties")
    if (
        meta.get("id") != fid or meta.get("mimeType") != TEXT_MIME
        or meta.get("trashed") is True
        or meta.get("parents") != [expected_parent]
        or not isinstance(props, dict)
        or props.get("readeraJobId") != job_id
        or props.get("readeraPart") != part
    ):
        raise DriveExportError("Synthetic Drive file identity or parent changed")
    remote = drive._request(
        "GET", f"https://www.googleapis.com/drive/v3/files/{fid}",
        params={"alt": "media"},
        headers={"Accept": "text/plain"},
    ).content
    if remote != expected:
        raise DriveExportError("Synthetic Drive TXT readback differs from expected bytes")


def _edit_synthetic_file(
    drive: GoogleDriveClient, file_id: str, text: bytes,
) -> None:
    """Only called for a newly created, already verified synthetic file ID.

    Normal export_completed_job never uses PATCH/DELETE; this is an explicit
    operator test of protecting both sides against subsequent edits.
    """
    fid = drive._check_id(file_id)
    response = drive._json(drive._request(
        "PATCH", f"https://www.googleapis.com/upload/drive/v3/files/{fid}",
        params={"uploadType": "media", "fields": "id,mimeType"},
        headers={"Content-Type": "text/plain; charset=utf-8"},
        content=text,
    ))
    if response.get("id") != fid or response.get("mimeType") != TEXT_MIME:
        raise DriveExportError("Synthetic in-place TXT edit was not acknowledged")


def run_drive_acceptance(
    drive: GoogleDriveClient,
    *, source_folder_id: str, translation_folder_id: str,
    root_folder_id: str = "", live: bool = False,
) -> dict:
    """Read-only old-root preflight or explicit new shared-library smoke.

    A live run exercises root / moderator / book / source+translation folders.
    All data is synthetic, generated in an isolated SQLite database, and
    existing Google Drive user-edited files are never rewritten.
    """
    source = drive._check_id(source_folder_id)
    translation = drive._check_id(translation_folder_id)
    if source == translation:
        raise DriveExportError("Two distinct existing Drive folders are required")
    drive.check_destination(source)
    drive.check_destination(translation)
    drive.children(source)
    drive.children(translation)
    root = ""
    if root_folder_id:
        root = drive._check_id(root_folder_id)
        drive.check_destination(root)
        drive.children(root)
    if not live:
        return {
            "stage": "read_only_preflight",
            "both_folders_writable": True,
            "listing_access_verified": True,
            "shared_root_verified": bool(root),
            "drive_mutations": 0,
        }
    if not root:
        raise DriveExportError(
            "CHAPTERA_DRIVE_ROOT_FOLDER_ID is required for the new live smoke"
        )

    with tempfile.TemporaryDirectory(prefix="readera-drive-acceptance-") as temp:
        store, job_id = _create_synthetic_completed_job(Path(temp))
        snapshot = read_completed_job(store, job_id)
        first = export_completed_job(
            store, drive, job_id, root_folder_id=root,
            legacy_source_folder_id=source,
            legacy_translation_folder_id=translation,
        )
        owner_id = first["moderator_folder_id"]
        book_id = first["book_folder_id"]
        book_key = hashlib.sha256(snapshot.book_id.encode("utf-8")).hexdigest()[:20]
        moderator_key = hashlib.sha256(snapshot.owner_member_id.encode("utf-8")).hexdigest()[:20]
        moderator_name = _clean_name(snapshot.owner_member_id)
        expected_book_name = f"{_clean_name(snapshot.book_title)} [{book_key[:10]}]"
        moderator = [
            x for x in drive.children(root) if x.get("id") == owner_id
        ]
        if (len(moderator) != 1 or moderator[0].get("name") != moderator_name
            or moderator[0].get("mimeType") != FOLDER_MIME
            or (moderator[0].get("appProperties") or {}).get("readeraModeratorKey")
            != moderator_key):
            raise DriveExportError("Synthetic moderator parent identity changed")
        book = [x for x in drive.children(owner_id) if x.get("id") == book_id]
        if (len(book) != 1 or book[0].get("name") != expected_book_name
            or book[0].get("mimeType") != FOLDER_MIME
            or (book[0].get("appProperties") or {}).get("readeraBookKey") != book_key):
            raise DriveExportError("Synthetic book parent identity changed")
        folders = {}
        for part, folder_name in (("source", "Источники"), ("translation", "Переводы")):
            folder_id = first[f"{part}_folder_id"]
            folder = [x for x in drive.children(book_id) if x.get("id") == folder_id]
            if (len(folder) != 1 or folder[0].get("name") != folder_name
                or folder[0].get("mimeType") != FOLDER_MIME
                or (folder[0].get("appProperties") or {}).get("readeraPartFolder") != part
                or (folder[0].get("appProperties") or {}).get("readeraBookKey") != book_key):
                raise DriveExportError("Synthetic source/translation parent identity changed")
            folders[part] = folder_id

        for part, body in (("source", snapshot.source), ("translation", snapshot.translation)):
            _read_verified_text(
                drive, first[f"{part}_file_id"], expected_parent=folders[part],
                job_id=job_id, part=part, expected=body,
            )
        marker = _EDIT_MARKER.encode("utf-8")
        edited = {
            "source": snapshot.source + marker,
            "translation": snapshot.translation + marker,
        }
        for part in ("source", "translation"):
            _edit_synthetic_file(drive, first[f"{part}_file_id"], edited[part])
            _read_verified_text(
                drive, first[f"{part}_file_id"], expected_parent=folders[part],
                job_id=job_id, part=part, expected=edited[part],
            )
        second = export_completed_job(
            store, drive, job_id, root_folder_id=root,
            legacy_source_folder_id=source,
            legacy_translation_folder_id=translation,
        )
        if first != second:
            raise DriveExportError("Repeated exporter changed a synthetic file or folder ID")
        for part in ("source", "translation"):
            _read_verified_text(
                drive, first[f"{part}_file_id"], expected_parent=folders[part],
                job_id=job_id, part=part, expected=edited[part],
            )
            matching = [
                item for item in drive.children(folders[part])
                if (item.get("appProperties") or {}).get("readeraJobId") == job_id
            ]
            if len(matching) != 1 or matching[0].get("id") != first[f"{part}_file_id"]:
                raise DriveExportError("Repeated export created duplicate synthetic chapters")

        return {
            "stage": "live_two_folder_export_verified",
            "layout": "moderator/book/source_translation",
            "job_id": job_id,
            "moderator_folder_id": owner_id,
            "book_folder_id": book_id,
            "source_folder_id": folders["source"],
            "translation_folder_id": folders["translation"],
            "source_file_id": first["source_file_id"],
            "translation_file_id": first["translation_file_id"],
            "both_edits_preserved": True,
            "file_ids_unchanged": True,
            "no_gemini_requests": True,
            "production_db_unchanged": True,
            "synthetic_drive_files_left_for_review": True,
        }
