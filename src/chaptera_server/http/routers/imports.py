"""Safe read-only preview plus explicitly confirmed, revision-safe team imports."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile, Request

from ...book_import import ImportFormatError, MAX_UPLOAD_BYTES, preview_book_import
from ...auth import role_for
from ...import_sessions import (
    ImportSessions, StageConflict, StageExpired, StageMissing, StageTooLarge,
)
from ...library import LibraryConflict, LibraryNotFound
from ..dependencies import get_import_sessions, require_member
from ..schemas import ConfirmBookImport

router = APIRouter(prefix="/v1/imports", dependencies=[Depends(require_member)])


async def _upload_bytes(file: UploadFile) -> bytes:
    try:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File exceeds 12 MiB limit")
        return content
    finally:
        await file.close()


def _import_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (StageMissing, LibraryNotFound)):
        return HTTPException(status_code=404, detail=exc.args[0])
    if isinstance(exc, StageExpired):
        return HTTPException(status_code=410, detail=str(exc))
    if isinstance(exc, StageTooLarge):
        return HTTPException(status_code=413, detail=str(exc))
    if isinstance(exc, (StageConflict, LibraryConflict)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ImportFormatError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/preview")
async def inspect_upload(file: UploadFile = File(...)):
    content = await _upload_bytes(file)
    try:
        return preview_book_import(file.filename or "", content)
    except ImportFormatError as exc:
        raise _import_error(exc) from exc


@router.post("/stages", status_code=201)
async def stage_upload(file: UploadFile = File(...),
                       actor: str = Depends(require_member),
                       sessions: ImportSessions = Depends(get_import_sessions),
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    content = await _upload_bytes(file)
    try:
        return sessions.stage(actor, file.filename or "", content, idempotency_key=idempotency_key)
    except (ImportFormatError, StageConflict, StageTooLarge, ValueError) as exc:
        raise _import_error(exc) from exc


@router.get("/stages/{stage_id}")
def get_stage(stage_id: str, actor: str = Depends(require_member),
              sessions: ImportSessions = Depends(get_import_sessions)):
    try:
        return sessions.get(actor, stage_id)
    except (StageMissing, StageExpired) as exc:
        raise _import_error(exc) from exc


@router.delete("/stages/{stage_id}")
def cancel_stage(stage_id: str, actor: str = Depends(require_member),
                 sessions: ImportSessions = Depends(get_import_sessions)):
    try:
        return sessions.cancel(actor, stage_id)
    except (StageMissing, StageExpired, StageConflict) as exc:
        raise _import_error(exc) from exc


@router.post("/stages/{stage_id}/confirm")
def confirm_stage(stage_id: str, body: ConfirmBookImport, request: Request,
                  actor: str = Depends(require_member),
                  sessions: ImportSessions = Depends(get_import_sessions)):
    payload = body.model_dump()
    if payload["book_mode"] == "create":
        owner = payload["owner_member_id"] or actor
        settings = request.app.state.settings
        managed = getattr(request.app.state, "team_credentials", None)
        if managed is None:
            is_moderator = owner in settings.team_tokens and role_for(owner, settings) == "moderator"
            actor_role = role_for(actor, settings)
        else:
            is_moderator = managed.is_active(owner, settings) and role_for(owner, settings, managed) == "moderator"
            actor_role = role_for(actor, settings, managed)
        if not is_moderator:
            raise HTTPException(status_code=422, detail="Book owner must be an active moderator")
        if actor_role != "admin" and owner != actor:
            raise HTTPException(status_code=403, detail="Only administrator can assign another owner")
        payload["owner_member_id"] = owner
    elif payload["owner_member_id"] is not None:
        raise HTTPException(status_code=422, detail="Existing book owner cannot change during import")
    try:
        return sessions.confirm(actor, stage_id, payload)
    except (StageMissing, StageExpired, StageConflict, LibraryConflict,
            LibraryNotFound, ValueError) as exc:
        raise _import_error(exc) from exc


@router.get("/books/{book_id}/unassigned")
def get_unassigned(book_id: str, actor: str = Depends(require_member),
                   sessions: ImportSessions = Depends(get_import_sessions)):
    try:
        return {"book_id": book_id, "prefixes": sessions.prefixes(book_id)}
    except LibraryNotFound as exc:
        raise _import_error(exc) from exc
