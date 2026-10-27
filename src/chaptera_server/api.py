"""ASGI composition root. HTTP contracts are implemented in http/routers/."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .config import Settings
from .library import BookLibrary
from .chapter_batches import TranslationBatches
from .import_sessions import ImportSessions
from .editorial import EditorialStore
from .chapter_qa import ChapterQaStore
from .team_credentials import TeamCredentials
from .team_passwords import TeamPasswords
from .team_sessions import TeamSessions
from .team_login_throttle import BrowserLoginThrottle
from .personal_gemini_keys import PersonalGeminiKeys
from .storage import QueueStore
from .http.routers import books, batches, health, identity, imports, jobs, quota, ui, editorial, session, gemini_keys
from .http.routers.ui import ASSETS as UI_ASSETS

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
    app.state.translation_batches = TranslationBatches(
        repo, app.state.library, require_personal_budget=bool(settings.gemini_vault_key),
    )
    app.state.import_sessions = ImportSessions(repo)
    if not settings.archive_only:
        from .source_runtime import install_source_collection
        install_source_collection(app, repo, settings)
    app.state.editorial = EditorialStore(repo)
    app.state.chapter_qa = ChapterQaStore(repo)
    app.state.team_credentials = TeamCredentials(repo)
    app.state.team_sessions = TeamSessions(repo)
    # Keep historical password tables for backup/restore, not authentication.
    app.state.team_passwords = TeamPasswords(repo)
    app.state.personal_gemini_keys = PersonalGeminiKeys(repo, settings.gemini_vault_key)
    app.state.team_login_throttle = BrowserLoginThrottle(repo)

    app.include_router(health.router)
    app.include_router(identity.router)
    app.include_router(session.router)
    app.include_router(gemini_keys.router)
    app.include_router(books.router)
    app.include_router(batches.router)
    app.include_router(imports.router)
    app.include_router(editorial.router)
    app.include_router(jobs.router)
    app.include_router(quota.router)
    # Jinja2 shell and existing /v1 routes remain separate.
    app.mount("/app-static", StaticFiles(directory=str(UI_ASSETS / "static")),
              name="readera-static")
    app.include_router(ui.router)
    return app


def create_default_app() -> FastAPI:
    return create_app(Settings.from_env())
