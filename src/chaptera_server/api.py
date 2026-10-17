"""ASGI composition root. HTTP contracts are implemented in http/routers/."""
from __future__ import annotations

from fastapi import FastAPI

from .config import Settings
from .library import BookLibrary
from .import_sessions import ImportSessions
from .storage import QueueStore
from .http.routers import books, health, imports, jobs

# Keep historical Python imports of request models working for existing callers.
from .http.schemas import (  # noqa: F401
    AcceptTranslation, CreateBook, SaveChapter, SaveGlossary,
    SubmitChapter, TranslateBookChapter,
)


def create_app(settings: Settings, store: QueueStore | None = None) -> FastAPI:
    app = FastAPI(
        title="Chaptera Team Translation API", version="0.6.0",
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    repo = store if store is not None else QueueStore(settings.db_path)
    app.state.settings = settings
    app.state.store = repo
    app.state.library = BookLibrary(repo)
    app.state.import_sessions = ImportSessions(repo)

    app.include_router(health.router)
    app.include_router(books.router)
    app.include_router(imports.router)
    app.include_router(jobs.router)
    return app


def create_default_app() -> FastAPI:
    return create_app(Settings.from_env())
