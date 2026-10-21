"""Actor-owned Timotxt RAW review. Never permits stage, export or translation."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ...source_job_store import SourceJobConflict, SourceJobMissing
from ..dependencies import require_member

router = APIRouter(
    prefix="/v1/source-books/jobs", dependencies=[Depends(require_member)],
)


class GlossaryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(ge=0,le=1_000_000)
    original: str = Field(min_length=1,max_length=120)
    rus: str = Field(min_length=1,max_length=160)


class DeriveReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_raw_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    glossary_rows: list[GlossaryEvidence] = Field(min_length=2,max_length=200)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc,SourceJobMissing):
        return HTTPException(status_code=404,detail="Source review not found")
    if isinstance(exc,(SourceJobConflict,sqlite3.IntegrityError)):
        return HTTPException(status_code=409,detail=str(exc)[:140])
    return HTTPException(status_code=422,detail=str(exc)[:140])


@router.get("/{job_id}/reviews")
def list_reviews(
    job_id: str, request: Request, response: Response,
    actor: str = Depends(require_member),
) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        snapshots = request.app.state.source_job_store.reviews.list(actor,job_id)
        return {"job_id":job_id,"snapshots":snapshots,"exportable":False}
    except (SourceJobMissing,SourceJobConflict,ValueError) as exc:
        raise _http_error(exc) from exc


@router.get("/{job_id}/reviews/{snapshot_id}")
def read_review(
    job_id: str, snapshot_id: str, request: Request, response: Response,
    actor: str = Depends(require_member),
) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return request.app.state.source_job_store.reviews.detail(
            actor,job_id,snapshot_id,
        )
    except (SourceJobMissing,SourceJobConflict,ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/{job_id}/reviews/{snapshot_id}/candidates",status_code=201)
def derive_candidate(
    job_id: str, snapshot_id: str, body: DeriveReview,
    request: Request, response: Response,
    actor: str = Depends(require_member),
) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return request.app.state.source_job_store.reviews.derive(
            actor,job_id,snapshot_id,
            expected_raw_sha256=body.expected_raw_sha256,
            glossary_rows=[x.model_dump() for x in body.glossary_rows],
        )
    except (SourceJobMissing,SourceJobConflict,ValueError,sqlite3.IntegrityError) as exc:
        raise _http_error(exc) from exc
