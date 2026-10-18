"""Read-only team archive of saved originals, with explicit admin takedown.

No source HTTP, background collection, automatic cache reuse or job mutation.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ...source_original_archive import (
    SourceArchiveConflict, SourceArchiveMissing,
)
from ..dependencies import require_admin, require_member

router = APIRouter(prefix="/v1/source-books/archive",
                   dependencies=[Depends(require_member)])


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc,SourceArchiveMissing):
        return HTTPException(status_code=404,detail="Archived original not found")
    if isinstance(exc,SourceArchiveConflict):
        return HTTPException(status_code=409,detail=str(exc)[:150])
    return HTTPException(status_code=422,detail=str(exc)[:150])


def _private(response: Response) -> None:
    response.headers["Cache-Control"]="no-store, private"
    response.headers["X-Content-Type-Options"]="nosniff"


@router.get("")
def archive_books(
    request: Request,response: Response,
    limit: int = Query(default=30,ge=1,le=50),
    offset: int = Query(default=0,ge=0,le=10000),
    actor: str = Depends(require_member),
) -> dict:
    _private(response)
    try:
        return request.app.state.source_job_store.archive.list_books(
            limit=limit,offset=offset,
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/revisions/{revision_id}")
def archive_chapter(
    revision_id: str,request: Request,response: Response,
    actor: str = Depends(require_member),
) -> dict:
    """Only intact source-revision bytes, never executable HTML."""
    _private(response)
    try:
        return request.app.state.source_job_store.archive.chapter(revision_id)
    except (SourceArchiveMissing,SourceArchiveConflict,ValueError) as exc:
        raise _error(exc) from exc


@router.delete("/revisions/{revision_id}")
def purge_archived_revision(
    revision_id: str,request: Request,response: Response,
    actor: str = Depends(require_member),
    admin_actor: str = Depends(require_admin),
) -> dict:
    """An authenticated administrator may remove archive data for takedown.

    It does not silently alter the BookLibrary or other stored source jobs.
    """
    _private(response)
    try:
        return request.app.state.source_job_store.archive.purge_revision(
            revision_id,admin_member_id=admin_actor,
        )
    except (SourceArchiveMissing,SourceArchiveConflict,ValueError) as exc:
        raise _error(exc) from exc


@router.get("/{book_id}")
def archive_chapters(
    book_id: str,request: Request,response: Response,
    limit: int = Query(default=50,ge=1,le=100),
    offset: int = Query(default=0,ge=0,le=10000),
    actor: str = Depends(require_member),
) -> dict:
    _private(response)
    try:
        return request.app.state.source_job_store.archive.chapters(
            book_id,limit=limit,offset=offset,
        )
    except (SourceArchiveMissing,SourceArchiveConflict,ValueError) as exc:
        raise _error(exc) from exc
