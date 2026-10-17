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
from ..dependencies import require_member, get_import_sessions

router = APIRouter(prefix="/v1/source-books/jobs", dependencies=[Depends(require_member)])


class CreateSourceJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    catalog_id: str = Field(min_length=1,max_length=128)
    expected_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_ordinal: int = Field(ge=1,le=10_000_000)
    end_ordinal: int = Field(ge=1,le=10_000_000)


class StageJobChapters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_ordinal: int = Field(ge=1,le=10_000_000)
    end_ordinal: int = Field(ge=1,le=10_000_000)


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc,(SourceJobMissing,SourceCatalogMissing)):
        return HTTPException(status_code=404,detail="Collection not found")
    if isinstance(exc,SourceCatalogExpired):
        return HTTPException(status_code=410,detail="Catalog expired; inspect book again")
    if isinstance(exc,(SourceJobConflict,sqlite3.IntegrityError)):
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
        if body.end_ordinal < body.start_ordinal or body.end_ordinal-body.start_ordinal+1 > 2000:
            raise SourceJobConflict("Choose at most 2000 consecutive chapter numbers")
        catalog = request.app.state.source_catalog_store.load(actor,body.catalog_id)
        if catalog.catalog_sha256 != body.expected_catalog_sha256:
            raise SourceJobConflict("Stale catalog; inspect the source again")
        return request.app.state.source_job_store.create(
            actor,body.catalog_id,catalog,
            list(range(body.start_ordinal,body.end_ordinal+1)),
            idempotency_key=idempotency_key,
        )
    except (SourceJobConflict,SourceCatalogMissing,SourceCatalogExpired) as exc:
        raise _errors(exc) from exc


@router.get("")
def recent_jobs(
    request: Request, limit: int = Query(default=20, ge=1, le=50),
    actor: str = Depends(require_member),
) -> dict:
    """Restore previous source collections without a copied UUID."""
    return {"jobs":request.app.state.source_job_store.list_recent(actor,limit=limit)}


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
    if (body.end_ordinal < body.start_ordinal or
        body.end_ordinal-body.start_ordinal+1 > 20):
        raise HTTPException(status_code=422,detail="Stage no more than 20 contiguous chapters")
    jobs = request.app.state.source_job_store
    try:
        selected=list(range(body.start_ordinal,body.end_ordinal+1))
        collected = jobs.materialize(actor,job_id,selected)
        payload = collected.as_import_html()
        # A stable key makes browser retry safe even if response was lost.
        fingerprint = sha256(payload).hexdigest()[:32]
        key = f"source-job:{job_id}:{body.start_ordinal}:{body.end_ordinal}:{fingerprint}"
        stage = sessions.stage(
            actor, f"verified-{job_id[:8]}-{body.start_ordinal}-{body.end_ordinal}.html",
            payload,idempotency_key=key[:128],
        )
        state = jobs.get(actor,job_id)
        details = [asdict(ch)|{"body":None} for ch in collected.chapters]
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
    """Completeness receipt for exactly the selected source TOC interval."""
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
    name=f"readera-source-{safe_source}-{result.start_ordinal}-{result.end_ordinal}.txt"
    return StreamingResponse(
        _stream_source_txt(result.file),media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition":f'attachment; filename="{name}"',
            "Content-Length":str(result.byte_length),
            "Cache-Control":"no-store, private",
            "X-Content-Type-Options":"nosniff",
            "X-ReadEra-Manifest-SHA256":result.manifest_sha256,
            "X-ReadEra-Content-SHA256":result.content_sha256,
            "X-ReadEra-Source-Scope":"selected_catalog_range_only",
            "ETag":f'"{result.content_sha256}"',
        },
    )
