"""Optional web-crawler composition: never imported in archive-only releases.

The Full Cycle ZIP importer uses book_import/import_sessions and does NOT
depend on any of the modules in this file. Existing crawler data is untouched.
"""
from __future__ import annotations

from .source_site_storage import SourceCatalogStore
from .source_selection_store import SourceSelectionStore
from .source_job_store import SourceJobStore
from .source_site_http import PublicHtmlFetcher
from .source_request_limiter import SourceRequestLimiter
from .http.routers import (
    source_books, source_team, source_archive, source_jobs, source_reviews,
)


def install_source_collection(app, store, settings) -> None:
    """Attach crawler stores/routes only when explicitly in full mode."""
    app.state.source_catalog_store = SourceCatalogStore(store)
    app.state.source_selection_store = SourceSelectionStore(store)
    app.state.source_job_store = SourceJobStore(
        store,
        actor_quota_bytes=settings.source_actor_quota_mib * 1024 * 1024,
        global_quota_bytes=settings.source_global_quota_mib * 1024 * 1024,
    )
    app.state.source_request_limiter = SourceRequestLimiter(store)
    app.state.public_source_fetcher = PublicHtmlFetcher(
        request_limiter=app.state.source_request_limiter,
    )
    for router in (
        source_books.router, source_team.router, source_archive.router,
        source_jobs.router, source_reviews.router,
    ):
        app.include_router(router)
