"""Book, glossary and chapter endpoints with revision-safe updates."""
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from chaptera_translation_core.prompt_policy import GlossaryVersionConflict, scoped_glossary

from ...library import BookLibrary, LibraryConflict, LibraryNotFound
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
    if owner not in settings.team_tokens or role_for(owner, settings) != "moderator":
        raise HTTPException(status_code=422, detail="Book owner must be an active moderator")
    if role_for(actor, settings) != "admin" and owner != actor:
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
                        library: BookLibrary = Depends(get_library)):
    from ...auth import role_for
    if (body.owner_member_id not in request.app.state.settings.team_tokens or
            role_for(body.owner_member_id, request.app.state.settings) != "moderator"):
        raise HTTPException(status_code=422, detail="Book owner must be an active moderator")
    try:
        return library.reassign_book_owner(
            book_id, new_owner_member_id=body.owner_member_id,
            expected_owner_revision=body.expected_owner_revision, actor_id=actor,
        )
    except (LibraryNotFound, LibraryConflict, ValueError) as exc:
        raise map_library_error(exc) from exc


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
