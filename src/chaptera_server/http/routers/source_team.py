"""Authenticated team READ of verified public-source original chapters.

Separate from owner-only source job mutation, retry, import stage and quotas.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from ...source_job_export import VerifiedSourceTXT, SourceExportNotReady
from ...source_job_store import SourceJobConflict, SourceJobMissing
from ...source_team_reader import SourceTeamReader
from ..dependencies import require_member

router = APIRouter(prefix="/v1/source-books/shared",
                   dependencies=[Depends(require_member)])


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, SourceJobMissing):
        return HTTPException(status_code=404,detail="Verified original not found")
    if isinstance(exc, SourceJobConflict):
        return HTTPException(status_code=409,detail=str(exc)[:160])
    return HTTPException(status_code=422,detail=str(exc)[:160])


@router.get("")
def shared_originals(
    request: Request, limit: int = Query(default=20,ge=1,le=50),
    actor: str = Depends(require_member),
) -> dict:
    """Only jobs with at least one checkpointed original, no personal statuses."""
    try:
        return {
            "sources": SourceTeamReader(request.app.state.store).list_recent(
                actor,limit=limit,
            ),
            "scope": "team_verified_originals",
            "read_only": True,
        }
    except (SourceJobConflict,ValueError) as exc:
        raise _error(exc) from exc


@router.get("/{job_id}")
def shared_chapters(
    job_id: str, request: Request,
    offset: int = Query(default=0,ge=0,le=2000),
    limit: int = Query(default=50,ge=1,le=100),
    actor: str = Depends(require_member),
) -> dict:
    try:
        return SourceTeamReader(request.app.state.store).chapters(
            job_id,offset=offset,limit=limit,
        )
    except (SourceJobMissing,SourceJobConflict,ValueError) as exc:
        raise _error(exc) from exc


@router.get("/{job_id}/chapters/{ordinal}")
def shared_chapter(
    job_id: str, ordinal: int, request: Request, response: Response,
    actor: str = Depends(require_member),
) -> dict:
    """Exactly one source original, with integrity and no browser caching."""
    try:
        chapter = SourceTeamReader(request.app.state.store).chapter(job_id,ordinal)
    except (SourceJobMissing,SourceJobConflict,ValueError) as exc:
        raise _error(exc) from exc
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return chapter


@router.get("/{job_id}/assembly")
def shared_assembly(
    job_id: str, request: Request, actor: str = Depends(require_member),
) -> dict:
    """Exact-manifest TXT receipt, no chapter body in preview."""
    try:
        return VerifiedSourceTXT(request.app.state.store).inspect(
            actor,job_id,team_read=True,
        )
    except (SourceJobMissing,SourceJobConflict,ValueError) as exc:
        raise _error(exc) from exc


def _stream_file(handle):
    try:
        while part := handle.read(128 * 1024):
            yield part
    finally:
        handle.close()


@router.get("/{job_id}/export.txt")
def shared_txt(
    job_id: str, request: Request,
    expected_manifest_sha256: str = Query(pattern=r"^[a-f0-9]{64}$"),
    expected_content_sha256: str = Query(pattern=r"^[a-f0-9]{64}$"),
    actor: str = Depends(require_member),
):
    """Download only complete original selection with exact SHA-256 receipt."""
    try:
        _, result = VerifiedSourceTXT(request.app.state.store).render(
            actor,job_id,expected_manifest_sha256,expected_content_sha256,
            team_read=True,
        )
    except (SourceJobMissing,SourceJobConflict,ValueError) as exc:
        raise _error(exc) from exc
    except SourceExportNotReady as exc:
        raise HTTPException(status_code=409,detail={
            "message":"Shared original collection incomplete or changed after preview",
            "report":exc.report,
        }) from exc
    name = "".join(c for c in result.source if c.isascii() and c.isalnum())[:25] or "source"
    return StreamingResponse(
        _stream_file(result.file),media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="readera-source-{name}-{result.start_ordinal}-{result.end_ordinal}.txt"',
            "Content-Length": str(result.byte_length),
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
            "X-ReadEra-Manifest-SHA256":result.manifest_sha256,
            "X-ReadEra-Content-SHA256":result.content_sha256,
            "X-ReadEra-Source-Scope":"selected_catalog_range_only",
            "ETag":f'"{result.content_sha256}"',
        },
    )
