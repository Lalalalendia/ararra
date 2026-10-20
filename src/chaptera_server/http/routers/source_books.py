"""Authenticated book-by-URL discovery and safe handoff to RE-02 staged import.

Never confirms chapters, queues translation or accepts an untrusted chapter URL
from the browser. Only server-observed, actor-bound catalog IDs are selectable.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ...import_sessions import StageTooLarge, ImportSessions
from ...source_catalog_med import IngestError
from ...source_site_profiles import public_site_profiles
from ...source_request_limiter import SourceSiteBusy
from ...source_collect import fetch_catalog, collect_selected
from ...source_selection_store import (SourceSelectionMissing, SourceSelectionExpired,
                                         SourceSelectionConflict)
from ...source_site_storage import (
    SourceCatalogStore, SourceCatalogMissing, SourceCatalogExpired,
    SourceCatalogConflict,
)
from ..dependencies import require_member, get_import_sessions

router = APIRouter(prefix="/v1/source-books", dependencies=[Depends(require_member)])
SourceName = Literal["royalroad", "webnovel", "uukanshu"]


class InspectSourceBook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: SourceName
    book_url: str = Field(min_length=16, max_length=2048)


class SaveSelectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_selection_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    ordered_ordinals: list[int] = Field(min_length=1, max_length=2000)


class SelectSourceChapters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ordinals: list[int] = Field(min_length=1, max_length=20)
    expected_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _catalog_store(request: Request) -> SourceCatalogStore:
    return request.app.state.source_catalog_store


def _catalog_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (SourceCatalogMissing, SourceSelectionMissing)):
        return HTTPException(status_code=404, detail="Catalog not found")
    if isinstance(exc, (SourceCatalogExpired, SourceSelectionExpired)):
        return HTTPException(status_code=410, detail=str(exc))
    if isinstance(exc, (SourceCatalogConflict, SourceSelectionConflict)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, StageTooLarge):
        return HTTPException(status_code=413, detail="Collected file exceeds import staging budget")
    if isinstance(exc, SourceSiteBusy):
        return HTTPException(status_code=429,
                             headers={"Retry-After": str(exc.retry_after)},
                             detail=f"site_busy: {exc.reason}")
    if isinstance(exc, IngestError):
        status = 502 if exc.status in ("network_error", "http_error") else 422
        return HTTPException(status_code=status, detail=f"{exc.status}: {str(exc)[:160]}")
    return HTTPException(status_code=422, detail=str(exc)[:160])


@router.get("/sites")
def supported_sources() -> dict:
    return {"sources": public_site_profiles()}


@router.post("/catalogs", status_code=201)
async def discover_book(
    body: InspectSourceBook, request: Request,
    actor: str = Depends(require_member),
) -> dict:
    fetcher = request.app.state.public_source_fetcher
    try:
        catalog = await fetch_catalog(
            body.source, body.book_url,
            lambda url: fetcher.fetch(body.source, url),
        )
        return _catalog_store(request).save(actor, catalog)
    except (IngestError, ValueError, SourceCatalogConflict) as exc:
        raise _catalog_error(exc) from exc


@router.get("/catalogs/{catalog_id}")
def get_discovered_book(
    catalog_id: str, request: Request, actor: str = Depends(require_member),
) -> dict:
    storage = _catalog_store(request)
    try:
        storage.load(actor, catalog_id)
        with storage.store._connect() as con:
            row = con.execute(
                "SELECT * FROM source_book_catalogs WHERE id=? AND actor_id=?",
                (catalog_id, actor),
            ).fetchone()
            return storage._public(row)
    except (SourceCatalogMissing, SourceCatalogExpired, SourceCatalogConflict) as exc:
        raise _catalog_error(exc) from exc


@router.put("/catalogs/{catalog_id}/selection")
def save_selection(
    catalog_id: str, body: SaveSelectionPlan, request: Request,
    actor: str = Depends(require_member),
) -> dict:
    """Durably save an ordered subset; source URLs stay server-owned."""
    try:
        catalog = _catalog_store(request).load(actor, catalog_id)
        return request.app.state.source_selection_store.save(
            actor, catalog_id, catalog, body.ordered_ordinals,
            expected_catalog_sha256=body.expected_catalog_sha256,
            expected_selection_sha256=body.expected_selection_sha256,
        )
    except (SourceCatalogMissing, SourceCatalogExpired, SourceCatalogConflict,
            SourceSelectionMissing, SourceSelectionExpired, SourceSelectionConflict) as exc:
        raise _catalog_error(exc) from exc


@router.get("/catalogs/{catalog_id}/selection")
def read_selection(
    catalog_id: str, request: Request,
    actor: str = Depends(require_member),
) -> dict:
    try:
        return request.app.state.source_selection_store.get(actor, catalog_id)
    except (SourceSelectionMissing, SourceSelectionExpired, SourceSelectionConflict) as exc:
        raise _catalog_error(exc) from exc


@router.get("/selections/recent")
def recent_selection(request: Request, actor: str = Depends(require_member)) -> dict:
    try:
        return request.app.state.source_selection_store.recent(actor)
    except SourceSelectionConflict as exc:
        raise _catalog_error(exc) from exc


@router.post("/catalogs/{catalog_id}/collect", status_code=201)
async def collect_book_chapters(
    catalog_id: str, body: SelectSourceChapters, request: Request,
    actor: str = Depends(require_member),
    sessions: ImportSessions = Depends(get_import_sessions),
) -> dict:
    store = _catalog_store(request)
    try:
        catalog = store.load(actor, catalog_id)
        if body.expected_catalog_sha256 != catalog.catalog_sha256:
            raise SourceCatalogConflict("Catalog changed; inspect the book again")
        fetcher = request.app.state.public_source_fetcher
        collected = await collect_selected(
            catalog, body.ordinals,
            lambda url: fetcher.fetch(catalog.source, url),
            expected_catalog_sha256=body.expected_catalog_sha256,
        )
        if not collected.complete:
            return {
                "status": "partial", "catalog_id": catalog_id,
                "collected_count": len(collected.chapters),
                "failed_count": len(collected.failures),
                "failures": [
                    {"ordinal": number, "error": message}
                    for number, message in collected.failures
                ],
                "stage_id": None,
            }
        payload = collected.as_import_html()
        # ImportSessions.stage alone owns raw chapter persistence.
        stage = sessions.stage(
            actor, f"source-{catalog.source}-{catalog_id[:8]}.html", payload,
            source_entry_metadata=[{
                "ordinal": ch.ordinal, "title": ch.title,
                "source_id": ch.source_id, "url": ch.url,
                "entry_type": ch.entry_type, "author_number": ch.author_number,
                "classification_source": ch.classification_source,
                "classification_confidence": ch.classification_confidence,
            } for ch in collected.chapters],
        )
        store.save_stage_receipt(
            actor, catalog_id, stage["stage_id"],catalog.catalog_sha256,
            [asdict(ch) | {"body": None} for ch in collected.chapters],
        )
        return {
            "status": "staged", "catalog_id": catalog_id,
            "stage_id": stage["stage_id"], "file_sha256": stage["file_sha256"],
            "collected_count": len(collected.chapters),
            "catalog_sha256": catalog.catalog_sha256,
        }
    except (IngestError, StageTooLarge, SourceCatalogMissing, SourceCatalogExpired,
            SourceCatalogConflict, ValueError) as exc:
        raise _catalog_error(exc) from exc


@router.get("/stages/{stage_id}/provenance")
def imported_source_provenance(
    stage_id: str, request: Request, actor: str = Depends(require_member),
) -> dict:
    try:
        return _catalog_store(request).stage_receipt(actor, stage_id)
    except SourceCatalogMissing as exc:
        raise _catalog_error(exc) from exc
