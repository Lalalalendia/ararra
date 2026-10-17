"""ASGI composition root. HTTP contracts are implemented in http/routers/."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .config import Settings
from .library import BookLibrary
from .import_sessions import ImportSessions
from .editorial import EditorialStore
from .chapter_qa import ChapterQaStore
from .team_credentials import TeamCredentials
from .team_sessions import TeamSessions
from .storage import QueueStore
from .http.routers import books, health, identity, imports, jobs, quota, ui, editorial, session
from .http.routers.ui import ASSETS as UI_ASSETS

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
    app.state.editorial = EditorialStore(repo)
    app.state.chapter_qa = ChapterQaStore(repo)
    app.state.team_credentials = TeamCredentials(repo)
    app.state.team_sessions = TeamSessions(repo)

    app.include_router(health.router)
    app.include_router(identity.router)
    app.include_router(session.router)
    app.include_router(books.router)
    app.include_router(imports.router)
    app.include_router(editorial.router)
    app.include_router(jobs.router)
    app.include_router(quota.router)
    # Jinja2 shell + static sources copied/adapted from books_platform.
    # Existing /v1 API and the optional React pilot are untouched.
    app.mount("/app-static", StaticFiles(directory=str(UI_ASSETS / "static")),
              name="readera-static")
    app.include_router(ui.router)
    return app


def create_default_app() -> FastAPI:
    return create_app(Settings.from_env())
