"""RE-06C: preview then atomically submit an explicit book chapter selection."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Query

from chaptera_translation_core.prompt_policy import GlossaryVersionConflict
from ...chapter_batches import TranslationBatches
from ...library import LibraryConflict, LibraryNotFound
from ..dependencies import map_library_error, require_member
from ..schemas import PreviewTranslationBatch, SubmitTranslationBatch

router = APIRouter(prefix="/v1/books")


def get_batches(request: Request) -> TranslationBatches:
    return request.app.state.translation_batches


@router.post("/{book_id}/translation-batches/preview")
def preview_batch(book_id: str, body: PreviewTranslationBatch,
                  actor: str = Depends(require_member),
                  batches: TranslationBatches = Depends(get_batches)):
    """Read-only. Returns a deterministic digest of selected current revisions."""
    try:
        result = batches.preview(book_id, body.model_dump())
        if request.app.state.settings.gemini_vault_key:
            # Personal-key deployments must configure a LOCAL per-project RPD
            # ceiling before approving paid batch processing. This is not
            # a claim about Google-side remaining quota or token costs.
            choices = request.app.state.personal_gemini_keys.list_for(actor)["projects"]
            result["personal_budget_ready"] = any(
                project["daily_request_cap"] is not None for project in choices
            )
        else:
            result["personal_budget_ready"] = True
        return result
    except (LibraryNotFound, LibraryConflict, GlossaryVersionConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.post("/{book_id}/translation-batches", status_code=201)
def submit_batch(book_id: str, body: SubmitTranslationBatch,
                 actor: str = Depends(require_member),
                 idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                 batches: TranslationBatches = Depends(get_batches)):
    """Confirm the exact preview and atomically enqueue all jobs; never call Gemini here."""
    try:
        return batches.submit(book_id, body.model_dump(), actor_id=actor,
                              idempotency_key=idempotency_key or "")
    except (LibraryNotFound, LibraryConflict, GlossaryVersionConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.get("/{book_id}/translation-batches")
def list_batches(book_id: str, limit: int = Query(default=20, ge=1, le=50),
                 actor: str = Depends(require_member),
                 batches: TranslationBatches = Depends(get_batches)):
    try:
        return {"book_id": book_id, "batches": batches.list(book_id, limit=limit)}
    except LibraryNotFound as exc:
        raise map_library_error(exc) from exc


@router.get("/{book_id}/translation-batches/{batch_id}")
def get_batch(book_id: str, batch_id: str,
              actor: str = Depends(require_member),
              batches: TranslationBatches = Depends(get_batches)):
    try:
        return batches.get(book_id, batch_id)
    except LibraryNotFound as exc:
        raise map_library_error(exc) from exc
