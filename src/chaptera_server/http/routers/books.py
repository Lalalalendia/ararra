"""Book, glossary and chapter endpoints with revision-safe updates."""
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Query, Response
from typing import Literal
import re

from chaptera_translation_core.prompt_policy import GlossaryVersionConflict, scoped_glossary

from ...library import BookLibrary, LibraryConflict, LibraryNotFound
from ...book_export import ExportNotReady, VerifiedBookExport
from ...storage import IdempotencyConflict, QueueStore
from ..dependencies import get_library, get_store, map_library_error, require_member, require_admin
from ..schemas import AcceptTranslation, CreateBook, ReassignBookOwner, SaveChapter, SaveGlossary, TranslateBookChapter

router = APIRouter(prefix="/v1/books")


@router.post("", status_code=201)
def create_book(body: CreateBook, request: Request, actor: str = Depends(require_member),
                library: BookLibrary = Depends(get_library)):
    from ...auth import role_for
    owner = body.owner_member_id or actor
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
    try:
        return library.create_book(body.book_id, body.title, owner_member_id=owner, actor_id=actor)
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.get("")
def list_books(actor: str = Depends(require_member),
               library: BookLibrary = Depends(get_library)):
    return {"books": library.list_books()}


@router.put("/{book_id}/owner")
def reassign_book_owner(book_id: str, body: ReassignBookOwner, request: Request,
                        actor: str = Depends(require_admin),
                        authenticated_actor: str = Depends(require_member),
                        library: BookLibrary = Depends(get_library)):
    from ...auth import role_for
    settings = request.app.state.settings
    managed = getattr(request.app.state, "team_credentials", None)
    if managed is None:
        is_moderator = (body.owner_member_id in settings.team_tokens
                        and role_for(body.owner_member_id, settings) == "moderator")
    else:
        is_moderator = (managed.is_active(body.owner_member_id, settings)
                        and role_for(body.owner_member_id, settings, managed) == "moderator")
    if not is_moderator:
        raise HTTPException(status_code=422, detail="Book owner must be an active moderator")
    try:
        return library.reassign_book_owner(
            book_id, new_owner_member_id=body.owner_member_id,
            expected_owner_revision=body.expected_owner_revision, actor_id=actor,
        )
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.get("/{book_id}/export/preview")
def preview_book_export(book_id: str, expected_last_chapter: int = Query(ge=1, le=2000),
                        actor: str = Depends(require_member),
                        repo: QueueStore = Depends(get_store)):
    """Read-only manifest/diagnostics, never accepted manuscript text."""
    try:
        return VerifiedBookExport(repo).inspect(book_id, expected_last_chapter)[0]
    except LibraryNotFound as exc:
        raise map_library_error(exc) from exc


@router.get("/{book_id}/export")
def download_book_export(book_id: str, expected_last_chapter: int = Query(ge=1, le=2000),
                         expected_manifest_sha256: str = Query(pattern=r"^[a-f0-9]{64}$"),
                         format: Literal["txt", "html"] = Query(),
                         actor: str = Depends(require_member),
                         repo: QueueStore = Depends(get_store)):
    """Require preview's exact revision receipt; never silently ship stale RU."""
    try:
        result = VerifiedBookExport(repo).render(
            book_id, expected_last_chapter, expected_manifest_sha256, format,
        )
    except LibraryNotFound as exc:
        raise map_library_error(exc) from exc
    except ExportNotReady as exc:
        raise HTTPException(status_code=409, detail={
            "message": "Book export is incomplete or changed after preview",
            "preview": exc.preview,
        }) from exc
    filename = re.sub(r"[^A-Za-z0-9_-]", "_", book_id)[:50] or "book"
    return Response(content=result.data, media_type=result.content_type, headers={
        "Content-Disposition": f'attachment; filename="readera-{filename}.{result.extension}"',
        "Cache-Control": "no-store, private",
        "X-Content-Type-Options": "nosniff",
        "X-ReadEra-Manifest-SHA256": result.manifest_sha256,
        "X-ReadEra-Content-SHA256": result.content_sha256,
        "ETag": f'"{result.content_sha256}"',
    })


@router.get("/{book_id}")
def get_book(book_id: str, actor: str = Depends(require_member),
             library: BookLibrary = Depends(get_library)):
    try:
        return library.get_book(book_id)
    except LibraryNotFound as exc:
        raise map_library_error(exc) from exc


@router.put("/{book_id}/glossary")
def save_glossary(book_id: str, body: SaveGlossary, actor: str = Depends(require_member),
                  library: BookLibrary = Depends(get_library)):
    try:
        return library.put_glossary(
            book_id, glossary=body.glossary, versions=body.glossary_versions,
            authority=body.glossary_authority, expected_revision=body.expected_revision, actor_id=actor,
        )
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.put("/{book_id}/chapters/{chapter_id}")
def save_chapter(book_id: str, chapter_id: str, body: SaveChapter,
                 actor: str = Depends(require_member), library: BookLibrary = Depends(get_library)):
    try:
        return library.put_chapter(book_id, chapter_id, **body.model_dump())
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.get("/{book_id}/chapters")
def list_book_chapters(book_id: str, actor: str = Depends(require_member),
                       library: BookLibrary = Depends(get_library)):
    try:
        return {"book_id": book_id, "chapters": library.list_chapters(book_id)}
    except LibraryNotFound as exc:
        raise map_library_error(exc) from exc


@router.get("/{book_id}/chapters/{chapter_id}")
def get_chapter(book_id: str, chapter_id: str, actor: str = Depends(require_member),
                library: BookLibrary = Depends(get_library)):
    try:
        return library.get_chapter(book_id, chapter_id)
    except LibraryNotFound as exc:
        raise map_library_error(exc) from exc


@router.put("/{book_id}/chapters/{chapter_id}/accepted-translation")
def accept_translation(book_id: str, chapter_id: str, body: AcceptTranslation,
                       actor: str = Depends(require_member), library: BookLibrary = Depends(get_library)):
    try:
        return library.accept_translation(book_id, chapter_id, **body.model_dump(), actor_id=actor)
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.get("/{book_id}/chapters/{chapter_id}/context")
def get_context(book_id: str, chapter_id: str, actor: str = Depends(require_member),
                library: BookLibrary = Depends(get_library)):
    try:
        return library.context_for_chapter(book_id, chapter_id)
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


@router.post("/{book_id}/chapters/{chapter_id}/translate", status_code=201)
def enqueue_book_translation(book_id: str, chapter_id: str, body: TranslateBookChapter,
                             actor: str = Depends(require_member),
                             library: BookLibrary = Depends(get_library),
                             repo: QueueStore = Depends(get_store),
                             idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    try:
        spec = library.new_job(book_id, chapter_id, **body.model_dump())
        scoped_glossary(spec.glossary, spec.glossary_versions, spec.chapter_path, spec.glossary_authority)
        return repo.create_job(actor, spec, idempotency_key=idempotency_key)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (LibraryNotFound, LibraryConflict, GlossaryVersionConflict, ValueError) as exc:
        raise map_library_error(exc) from exc
