"""Small closed-team HTTP boundary; queue worker is a separate process."""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Literal

from chaptera_translation_core.prompt_policy import GlossaryVersionConflict, scoped_glossary

from .auth import authenticate
from .config import Settings
from .storage import IdempotencyConflict, JobSpec, QueueStore
from .library import BookLibrary, LibraryConflict, LibraryNotFound


class CreateBook(BaseModel):
    book_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)


class SaveChapter(BaseModel):
    chapter_index: int = Field(ge=1)
    chapter_path: str = Field(min_length=1, max_length=1024)
    source_html: str = Field(min_length=1, max_length=2_000_000)
    expected_revision: int = Field(ge=0)


class SaveGlossary(BaseModel):
    glossary: dict[str, str | dict[str, Any]] = Field(default_factory=dict)
    glossary_versions: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    glossary_authority: Literal['READY', 'READY_WITH_WARNINGS', 'EMPTY_EXPECTED', 'UNAVAILABLE', 'DISABLED'] = 'READY'
    expected_revision: int = Field(ge=0)


class AcceptTranslation(BaseModel):
    translated_html: str = Field(min_length=1, max_length=2_000_000)
    expected_revision: int = Field(ge=0)
    expected_source_revision: int = Field(ge=1)


class TranslateBookChapter(BaseModel):
    source_language: str = Field(default='auto', max_length=64)
    target_language: str = Field(default='Russian', min_length=1, max_length=64)
    style_instructions: str = Field(default='', max_length=10_000)
    max_attempts: int = Field(default=3, ge=1, le=10)
    transport: Literal["html_v0", "atoms_v1"] = "html_v0"


class SubmitChapter(BaseModel):
    chapter_id: str = Field(min_length=1, max_length=255)
    source_html: str = Field(min_length=1, max_length=2_000_000)
    source_language: str = Field(default="auto", max_length=64)
    target_language: str = Field(default="Russian", min_length=1, max_length=64)
    glossary: dict[str, str | dict[str, Any]] = Field(default_factory=dict)
    context: str = Field(default="", max_length=100_000)
    chapter_path: str = Field(default="", max_length=1024)
    max_attempts: int = Field(default=3, ge=1, le=10)
    glossary_versions: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    glossary_authority: Literal['READY', 'READY_WITH_WARNINGS', 'EMPTY_EXPECTED', 'UNAVAILABLE', 'DISABLED'] = 'READY'
    chapter_context_snapshot: dict[str, Any] | None = None
    predecessor: dict[str, Any] | None = None
    style_instructions: str = Field(default='', max_length=10_000)
    transport: Literal["html_v0", "atoms_v1"] = "html_v0"


def create_app(settings: Settings, store: QueueStore | None = None) -> FastAPI:
    app = FastAPI(title="Chaptera Team Translation API", version="0.4.0",
                  docs_url=None, redoc_url=None, openapi_url=None)
    repo = store or QueueStore(settings.db_path)
    library = BookLibrary(repo)

    def require_member(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> str:
        return authenticate(x_api_key, settings)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Managed book endpoints: revisions protect shared edits by multiple team members.
    # These book jobs do not require clients to construct glossary hashes or context snapshots.
    def map_library_error(exc: Exception) -> HTTPException:
        if isinstance(exc, LibraryNotFound):
            return HTTPException(status_code=404, detail=str(exc.args[0]))
        if isinstance(exc, LibraryConflict):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, GlossaryVersionConflict):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    @app.post('/v1/books', status_code=201)
    def create_book(body: CreateBook, actor: str = Depends(require_member)):
        try:
            return library.create_book(body.book_id, body.title)
        except (LibraryNotFound, LibraryConflict, ValueError) as exc:
            raise map_library_error(exc) from exc

    @app.get('/v1/books/{book_id}')
    def get_book(book_id: str, actor: str = Depends(require_member)):
        try:
            return library.get_book(book_id)
        except LibraryNotFound as exc:
            raise map_library_error(exc) from exc

    @app.put('/v1/books/{book_id}/glossary')
    def save_glossary(book_id: str, body: SaveGlossary, actor: str = Depends(require_member)):
        try:
            return library.put_glossary(
                book_id, glossary=body.glossary, versions=body.glossary_versions,
                authority=body.glossary_authority, expected_revision=body.expected_revision, actor_id=actor,
            )
        except (LibraryNotFound, LibraryConflict, ValueError) as exc:
            raise map_library_error(exc) from exc

    @app.put('/v1/books/{book_id}/chapters/{chapter_id}')
    def save_chapter(book_id: str, chapter_id: str, body: SaveChapter, actor: str = Depends(require_member)):
        try:
            return library.put_chapter(book_id, chapter_id, **body.model_dump())
        except (LibraryNotFound, LibraryConflict, ValueError) as exc:
            raise map_library_error(exc) from exc

    @app.get('/v1/books/{book_id}/chapters/{chapter_id}')
    def get_chapter(book_id: str, chapter_id: str, actor: str = Depends(require_member)):
        try:
            return library.get_chapter(book_id, chapter_id)
        except LibraryNotFound as exc:
            raise map_library_error(exc) from exc

    @app.put('/v1/books/{book_id}/chapters/{chapter_id}/accepted-translation')
    def accept_translation(book_id: str, chapter_id: str, body: AcceptTranslation,
                           actor: str = Depends(require_member)):
        try:
            return library.accept_translation(book_id, chapter_id, **body.model_dump(), actor_id=actor)
        except (LibraryNotFound, LibraryConflict, ValueError) as exc:
            raise map_library_error(exc) from exc

    @app.get('/v1/books/{book_id}/chapters/{chapter_id}/context')
    def get_context(book_id: str, chapter_id: str, actor: str = Depends(require_member)):
        try:
            return library.context_for_chapter(book_id, chapter_id)
        except (LibraryNotFound, LibraryConflict, ValueError) as exc:
            raise map_library_error(exc) from exc

    @app.post('/v1/books/{book_id}/chapters/{chapter_id}/translate', status_code=201)
    def enqueue_book_translation(book_id: str, chapter_id: str, body: TranslateBookChapter,
                                 actor: str = Depends(require_member),
                                 idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
        try:
            spec = library.new_job(book_id, chapter_id, **body.model_dump())
            scoped_glossary(spec.glossary, spec.glossary_versions, spec.chapter_path, spec.glossary_authority)
            return repo.create_job(actor, spec, idempotency_key=idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (LibraryNotFound, LibraryConflict, GlossaryVersionConflict, ValueError) as exc:
            raise map_library_error(exc) from exc

    @app.post("/v1/jobs", status_code=201)
    def submit(body: SubmitChapter, actor: str = Depends(require_member),
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        try:
            scoped_glossary(body.glossary, body.glossary_versions, body.chapter_path, body.glossary_authority)
            return repo.create_job(actor, JobSpec(**body.model_dump()), idempotency_key=idempotency_key)
        except GlossaryVersionConflict as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/jobs")
    def list_jobs(limit: int = 50, actor: str = Depends(require_member)):
        return {"jobs": repo.list_jobs(limit=limit)}

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, actor: str = Depends(require_member)):
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.get("/v1/jobs/{job_id}/events")
    def get_events(job_id: str, actor: str = Depends(require_member)):
        if repo.get_job(job_id, include_result=False) is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"events": repo.events(job_id)}

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str, actor: str = Depends(require_member)):
        job = repo.request_cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    return app


def create_default_app() -> FastAPI:
    return create_app(Settings.from_env())
