"""Authenticated durable public chapter capture; no automatic library commit."""
from __future__ import annotations

import sqlite3
from dataclasses import asdict
from hashlib import sha256

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ...import_sessions import StageTooLarge
from ...source_job_store import SourceJobConflict, SourceJobMissing
from ...source_job_export import VerifiedSourceTXT, SourceExportNotReady
from ...source_site_storage import SourceCatalogMissing, SourceCatalogExpired
from ...source_selection_store import (
    SourceSelectionStore, SourceSelectionMissing,
    SourceSelectionExpired, SourceSelectionConflict,
)
from ..dependencies import require_member, get_import_sessions

router = APIRouter(prefix="/v1/source-books/jobs", dependencies=[Depends(require_member)])


class CreateSourceJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    catalog_id: str = Field(min_length=1,max_length=128)
    expected_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_ordinal: int | None = Field(default=None,ge=1,le=10_000_000)
    end_ordinal: int | None = Field(default=None,ge=1,le=10_000_000)
    selection_id: str | None = Field(default=None,min_length=1,max_length=128)
    expected_selection_sha256: str | None = Field(default=None,pattern=r"^[a-f0-9]{64}$")


class StageJobChapters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_ordinal: int | None = Field(default=None,ge=1,le=10_000_000)
    end_ordinal: int | None = Field(default=None,ge=1,le=10_000_000)
    start_position: int | None = Field(default=None,ge=1,le=2000)
    end_position: int | None = Field(default=None,ge=1,le=2000)


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc,(SourceJobMissing,SourceCatalogMissing,SourceSelectionMissing)):
        return HTTPException(status_code=404,detail="Collection not found")
    if isinstance(exc,(SourceCatalogExpired,SourceSelectionExpired)):
        return HTTPException(status_code=410,detail="Catalog expired; inspect book again")
    if isinstance(exc,(SourceJobConflict,SourceSelectionConflict,sqlite3.IntegrityError)):
        return HTTPException(status_code=409,detail=str(exc)[:160])
    if isinstance(exc,StageTooLarge):
        return HTTPException(status_code=413,detail="Selected chapter batch exceeds staging limit")
    return HTTPException(status_code=422,detail=str(exc)[:160])


@router.post("",status_code=201)
def begin_job(
    body: CreateSourceJob, request: Request,
    actor: str = Depends(require_member),
    idempotency_key: str | None = Header(default=None,alias="Idempotency-Key"),
) -> dict:
    try:
        catalog = request.app.state.source_catalog_store.load(actor,body.catalog_id)
        if catalog.catalog_sha256 != body.expected_catalog_sha256:
            raise SourceJobConflict("Stale catalog; inspect the source again")
        if body.selection_id is not None:
            if (body.start_ordinal is not None or body.end_ordinal is not None or
                body.expected_selection_sha256 is None):
                raise SourceJobConflict("An ordered plan cannot be mixed with a chapter range")
            selection = request.app.state.source_selection_store.load(actor,body.selection_id)
            if (selection["catalog_id"] != body.catalog_id or
                selection["catalog_sha256"] != catalog.catalog_sha256 or
                selection["selection_sha256"] != body.expected_selection_sha256):
                raise SourceJobConflict("Saved selection differs from the accepted plan")
            ordinals = selection["ordered_ordinals"]
            preserve_order = True
        else:
            if (body.expected_selection_sha256 is not None or
                body.start_ordinal is None or body.end_ordinal is None or
                body.end_ordinal < body.start_ordinal or
                body.end_ordinal - body.start_ordinal + 1 > 2000):
                raise SourceJobConflict("Choose at most 2000 consecutive chapter numbers")
            ordinals = list(range(body.start_ordinal, body.end_ordinal + 1))
            preserve_order = False
        return request.app.state.source_job_store.create(
            actor,body.catalog_id,catalog,ordinals,
            idempotency_key=idempotency_key,
            preserve_order=preserve_order,
            selection_sha256=(body.expected_selection_sha256 if preserve_order else None),
            selection_id=(body.selection_id if preserve_order else None),
        )
    except (SourceJobConflict,SourceCatalogMissing,SourceCatalogExpired,
            SourceSelectionMissing,SourceSelectionExpired,SourceSelectionConflict) as exc:
        raise _errors(exc) from exc


@router.get("")
def recent_jobs(
    request: Request, limit: int = Query(default=20, ge=1, le=50),
    actor: str = Depends(require_member),
) -> dict:
    """Restore previous source collections without a copied UUID."""
    store = request.app.state.source_job_store
    return {"jobs":store.list_recent(actor,limit=limit), "usage":store.usage(actor)}


@router.delete("/{job_id}")
def delete_finished_job(
    job_id: str, request: Request, actor: str = Depends(require_member),
) -> dict:
    """Only explicitly purge complete/partial/cancelled crawler checkpoints."""
    try:
        return request.app.state.source_job_store.delete_finished(actor,job_id)
    except (SourceJobMissing,SourceJobConflict) as exc:
        raise _errors(exc) from exc


@router.get("/{job_id}")
def read_job(job_id: str,request: Request,actor: str = Depends(require_member)) -> dict:
    try:
        return request.app.state.source_job_store.get(actor,job_id)
    except (SourceJobMissing,SourceJobConflict) as exc:
        raise _errors(exc) from exc


@router.post("/{job_id}/retry")
def retry_job(job_id: str,request: Request,actor: str = Depends(require_member)) -> dict:
    try:
        return request.app.state.source_job_store.retry_failures(actor,job_id)
    except (SourceJobMissing,SourceJobConflict) as exc:
        raise _errors(exc) from exc


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str,request: Request,actor: str = Depends(require_member)) -> dict:
    try:
        return request.app.state.source_job_store.cancel(actor,job_id)
    except (SourceJobMissing,SourceJobConflict) as exc:
        raise _errors(exc) from exc


@router.post("/{job_id}/stage",status_code=201)
def stage_job(
    job_id: str, body: StageJobChapters, request: Request,
    actor: str = Depends(require_member),
    sessions = Depends(get_import_sessions),
) -> dict:
    jobs = request.app.state.source_job_store
    try:
        state = jobs.get(actor,job_id)
        if state["selection_mode"] == "ordered":
            if (body.start_ordinal is not None or body.end_ordinal is not None or
                body.start_position is None or body.end_position is None or
                body.end_position < body.start_position or
                body.end_position - body.start_position + 1 > 20 or
                body.end_position > state["total"]):
                raise ValueError("Choose 1..20 ordered selection positions for staging")
            lo,hi = body.start_position,body.end_position
            selected=state["selected_ordinals"][lo-1:hi]
            hints=list(range(lo,hi+1))
        else:
            if (body.start_position is not None or body.end_position is not None or
                body.start_ordinal is None or body.end_ordinal is None or
                body.end_ordinal < body.start_ordinal or
                body.end_ordinal - body.start_ordinal + 1 > 20):
                raise ValueError("Stage no more than 20 contiguous chapters")
            selected=list(range(body.start_ordinal,body.end_ordinal+1))
            lo,hi = body.start_ordinal,body.end_ordinal
            hints=None
        collected = jobs.materialize(actor,job_id,selected)
        payload = collected.as_import_html()
        # Idempotent exact-byte and position fence across browser retries.
        fingerprint = sha256(payload).hexdigest()[:32]
        key = f"source-job:{job_id}:{lo}:{hi}:{fingerprint}"
        stage = sessions.stage(
            actor, f"verified-{job_id[:8]}-{lo}-{hi}.html",
            payload,idempotency_key=key[:128],suggested_positions=hints,
        )
        details = [asdict(ch)|{"body":None, "destination_position":hints[i] if hints else None}
                   for i,ch in enumerate(collected.chapters)]
        store=request.app.state.source_catalog_store
        try:
            store.save_stage_receipt(actor,state["catalog_id"],stage["stage_id"],
                                     state["catalog_sha256"],details)
        except sqlite3.IntegrityError:
            old=store.stage_receipt(actor,stage["stage_id"])
            if (old["catalog_id"] != state["catalog_id"] or
                old["catalog_sha256"] != state["catalog_sha256"] or
                old["chapters"] != details):
                raise SourceJobConflict("Source stage receipt changed")
        return {
            "status":"staged", "job_id":job_id, "stage_id":stage["stage_id"],
            "collected_count":len(collected.chapters),
            "file_sha256":stage["file_sha256"],
        }
    except (SourceJobMissing,SourceJobConflict,SourceCatalogMissing,
            sqlite3.IntegrityError,StageTooLarge,ValueError) as exc:
        raise _errors(exc) from exc


def _stream_source_txt(handle):
    """Always release spool space on normal delivery or early disconnect."""
    try:
        while True:
            part = handle.read(128 * 1024)
            if not part:
                break
            yield part
    finally:
        handle.close()


@router.get("/{job_id}/assembly")
def inspect_source_txt(
    job_id: str, request: Request, actor: str = Depends(require_member),
) -> dict:
    """Completeness receipt for the immutable selected chapter manifest."""
    try:
        return VerifiedSourceTXT(request.app.state.store).inspect(actor,job_id)
    except (SourceJobMissing,SourceJobConflict,ValueError) as exc:
        raise _errors(exc) from exc


@router.get("/{job_id}/export.txt")
def download_source_txt(
    job_id: str, request: Request,
    expected_manifest_sha256: str = Query(pattern=r"^[a-f0-9]{64}$"),
    expected_content_sha256: str = Query(pattern=r"^[a-f0-9]{64}$"),
    actor: str = Depends(require_member),
):
    """Only ship a complete, identical original after exact preview receipt."""
    try:
        report, result = VerifiedSourceTXT(request.app.state.store).render(
            actor,job_id,expected_manifest_sha256,expected_content_sha256,
        )
    except (SourceJobMissing,SourceJobConflict,ValueError) as exc:
        raise _errors(exc) from exc
    except SourceExportNotReady as exc:
        raise HTTPException(status_code=409,detail={
            "message":"Original source collection incomplete or changed after preview",
            "report":exc.report,
        }) from exc
    safe_source = "".join(c for c in result.source if c.isascii() and c.isalnum())[:25] or "source"
    label = ("selected" if report["scope"] == "selected_catalog_selection_only"
             else f"{result.start_ordinal}-{result.end_ordinal}")
    name=f"readera-source-{safe_source}-{label}.txt"
    return StreamingResponse(
        _stream_source_txt(result.file),media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition":f'attachment; filename="{name}"',
            "Content-Length":str(result.byte_length),
            "Cache-Control":"no-store, private",
            "X-Content-Type-Options":"nosniff",
            "X-ReadEra-Manifest-SHA256":result.manifest_sha256,
            "X-ReadEra-Content-SHA256":result.content_sha256,
            "X-ReadEra-Source-Scope":report["scope"],
            "ETag":f'"{result.content_sha256}"',
        },
    )
