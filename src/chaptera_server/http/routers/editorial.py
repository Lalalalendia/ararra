"""Editorial workspace API; every route requires an authenticated team member.

Drafts and notes never enter the accepted chapter text or worker input.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ...editorial import EditorialStore
from ...chapter_qa import ChapterQaStore
from ...library import LibraryConflict, LibraryNotFound
from ..dependencies import get_editorial, get_chapter_qa, map_library_error, require_member
from ..schemas import (
    CreateEditorNote, RestoreEditorAccepted,
    SaveEditorDraft, SetEditorNoteStatus, RunChapterQa, ReviewChapterQa,
)

router = APIRouter(prefix="/v1/books/{book_id}/chapters/{chapter_id}")


@router.get("/draft")
def read_draft(book_id: str, chapter_id: str,
               actor: str = Depends(require_member),
               store: EditorialStore = Depends(get_editorial)):
    try:
        return store.get_draft(book_id, chapter_id)
    except LibraryNotFound as exc:
        raise map_library_error(exc) from exc


@router.put("/draft")
def write_draft(book_id: str, chapter_id: str, body: SaveEditorDraft,
                actor: str = Depends(require_member),
                store: EditorialStore = Depends(get_editorial)):
    try:
        return store.save_draft(
            book_id, chapter_id, actor_id=actor, **body.model_dump()
        )
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.get("/notes")
def read_notes(book_id: str, chapter_id: str,
               actor: str = Depends(require_member),
               store: EditorialStore = Depends(get_editorial)):
    try:
        return {"notes": store.list_notes(book_id, chapter_id)}
    except LibraryNotFound as exc:
        raise map_library_error(exc) from exc


@router.post("/notes", status_code=201)
def create_note(book_id: str, chapter_id: str, body: CreateEditorNote,
                actor: str = Depends(require_member),
                store: EditorialStore = Depends(get_editorial)):
    try:
        return store.add_note(book_id, chapter_id, actor_id=actor, **body.model_dump())
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.patch("/notes/{note_id}")
def update_note(book_id: str, chapter_id: str, note_id: str,
                body: SetEditorNoteStatus,
                actor: str = Depends(require_member),
                store: EditorialStore = Depends(get_editorial)):
    try:
        return store.resolve_note(
            book_id, chapter_id, note_id, actor_id=actor, **body.model_dump()
        )
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.get("/accepted-revisions")
def read_history(book_id: str, chapter_id: str,
                 actor: str = Depends(require_member),
                 store: EditorialStore = Depends(get_editorial)):
    try:
        return {"revisions": store.accepted_history(book_id, chapter_id)}
    except LibraryNotFound as exc:
        raise map_library_error(exc) from exc


@router.get("/accepted-revisions/{revision}")
def read_accepted_revision(book_id: str, chapter_id: str, revision: int,
                           actor: str = Depends(require_member),
                           store: EditorialStore = Depends(get_editorial)):
    try:
        return store.accepted_version(book_id, chapter_id, revision)
    except LibraryNotFound as exc:
        raise map_library_error(exc) from exc


@router.post("/restore-accepted")
def restore_history(book_id: str, chapter_id: str, body: RestoreEditorAccepted,
                    actor: str = Depends(require_member),
                    store: EditorialStore = Depends(get_editorial)):
    try:
        return store.restore_accepted(
            book_id, chapter_id, actor_id=actor, **body.model_dump()
        )
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.get("/qa")
def read_chapter_qa(book_id: str, chapter_id: str,
                    actor: str = Depends(require_member),
                    qa: ChapterQaStore = Depends(get_chapter_qa)):
    try:
        return qa.current(book_id, chapter_id)
    except (LibraryNotFound, LibraryConflict) as exc:
        raise map_library_error(exc) from exc


@router.post("/qa")
def scan_chapter_qa(book_id: str, chapter_id: str, body: RunChapterQa,
                    actor: str = Depends(require_member),
                    qa: ChapterQaStore = Depends(get_chapter_qa)):
    try:
        return qa.scan(book_id, chapter_id, actor_id=actor, **body.model_dump())
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.patch("/qa/findings/{finding_id}")
def review_chapter_qa(book_id: str, chapter_id: str, finding_id: str,
                      body: ReviewChapterQa,
                      actor: str = Depends(require_member),
                      qa: ChapterQaStore = Depends(get_chapter_qa)):
    try:
        return qa.review(
            book_id, chapter_id, finding_id, actor_id=actor,
            **body.model_dump(),
        )
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc
