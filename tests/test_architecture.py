"""Architecture acceptance: owned routes, dependency boundaries and monolith limits."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import httpx
from fastapi.routing import APIRoute

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.http.dependencies import require_member
from chaptera_server.http.routers import books, health, identity, imports, jobs, editorial, quota, session, source_books
from chaptera_server.storage import QueueStore

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'tools' / 'check_architecture.py'
SPEC = importlib.util.spec_from_file_location('chaptera_architecture_guard', GUARD)
assert SPEC is not None and SPEC.loader is not None
architecture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(architecture)

# Canonical v0.6 HTTP contract; additions are intentional policy edits, not accidental router drift.
ROUTE_CONTRACT = {
    ('GET', '/healthz'),
    ('GET', '/v1/me'),
    ('POST', '/v1/session'),
    ('POST', '/v1/session/password'),
    ('POST', '/v1/session/password/change'),
    ('GET', '/v1/session'),
    ('POST', '/v1/session/logout'),
    ('GET', '/v1/admin/members'),
    ('POST', '/v1/admin/members'),
    ('POST', '/v1/admin/members/{member_id}/rotate'),
    ('POST', '/v1/admin/members/{member_id}/disable'),
    ('POST', '/v1/admin/members/{member_id}/password'),
    ('POST', '/v1/books'),
    ('GET', '/v1/books'),
    ('GET', '/v1/books/{book_id}'),
    ('GET', '/v1/books/{book_id}/export/preview'),
    ('GET', '/v1/books/{book_id}/export'),
    ('PUT', '/v1/books/{book_id}/glossary'),
    ('PUT', '/v1/books/{book_id}/owner'),
    ('PUT', '/v1/books/{book_id}/chapters/{chapter_id}'),
    ('GET', '/v1/books/{book_id}/chapters'),
    ('GET', '/v1/books/{book_id}/chapters/{chapter_id}'),
    ('PUT', '/v1/books/{book_id}/chapters/{chapter_id}/accepted-translation'),
    ('GET', '/v1/books/{book_id}/chapters/{chapter_id}/draft'),
    ('PUT', '/v1/books/{book_id}/chapters/{chapter_id}/draft'),
    ('GET', '/v1/books/{book_id}/chapters/{chapter_id}/notes'),
    ('POST', '/v1/books/{book_id}/chapters/{chapter_id}/notes'),
    ('GET', '/v1/books/{book_id}/chapters/{chapter_id}/qa'),
    ('POST', '/v1/books/{book_id}/chapters/{chapter_id}/qa'),
    ('PATCH', '/v1/books/{book_id}/chapters/{chapter_id}/qa/findings/{finding_id}'),
    ('PATCH', '/v1/books/{book_id}/chapters/{chapter_id}/notes/{note_id}'),
    ('GET', '/v1/books/{book_id}/chapters/{chapter_id}/accepted-revisions'),
    ('GET', '/v1/books/{book_id}/chapters/{chapter_id}/accepted-revisions/{revision}'),
    ('POST', '/v1/books/{book_id}/chapters/{chapter_id}/restore-accepted'),
    ('GET', '/v1/books/{book_id}/chapters/{chapter_id}/context'),
    ('POST', '/v1/books/{book_id}/chapters/{chapter_id}/translate'),
    ('POST', '/v1/imports/preview'),
    ('POST', '/v1/imports/stages'),
    ('GET', '/v1/imports/stages/{stage_id}'),
    ('DELETE', '/v1/imports/stages/{stage_id}'),
    ('POST', '/v1/imports/stages/{stage_id}/confirm'),
    ('GET', '/v1/imports/books/{book_id}/unassigned'),
    ('POST', '/v1/jobs'),
    ('GET', '/v1/jobs'),
    ('GET', '/v1/jobs/{job_id}'),
    ('GET', '/v1/jobs/{job_id}/events'),
    ('GET', '/v1/quota/gemini'),
    ('POST', '/v1/jobs/{job_id}/cancel'),
    ('GET', '/v1/source-books/sites'),
    ('POST', '/v1/source-books/catalogs'),
    ('GET', '/v1/source-books/catalogs/{catalog_id}'),
    ('POST', '/v1/source-books/catalogs/{catalog_id}/collect'),
    ('GET', '/v1/source-books/stages/{stage_id}/provenance'),
}


def test_repo_architecture_boundaries_pass():
    assert architecture.inspect_repository(ROOT) == []


def test_retired_react_and_jinja_asset_gates_fail_closed(tmp_path):
    """Guard must reject a reintroduced Vite workspace and missing real UI."""
    (tmp_path / 'web').mkdir()
    errors = architecture.inspect_repository(tmp_path)
    assert any('obsolete React/Vite web/' in error for error in errors)
    assert any('web_ui/templates/shell.html' in error for error in errors)


def test_ui_architecture_rejects_browser_storage_and_misowned_fetch(tmp_path):
    js_dir = tmp_path / 'src/chaptera_server/web_ui/static/js'
    js_dir.mkdir(parents=True)
    (js_dir / 'library-view.mjs').write_text(
        "const token = localStorage.getItem('api_key');\n"
        "fetch('/v1/books');\n"
        "const leak = import.meta.env.VITE_TEAM_SECRET;\n",
        encoding='utf-8',
    )
    errors = architecture.inspect_repository(tmp_path)
    assert any('must not persist team/API keys' in error for error in errors)
    assert any('direct fetch belongs' in error for error in errors)
    assert any('legacy Vite keys' in error for error in errors)


def test_violations_detected_for_cross_layer_imports_and_misowned_endpoints():
    cases = [
        ('from fastapi import Depends\n', 'src/chaptera_translation_core/core/bad.py', 'core imports'),
        ('from chaptera_server.http.dependencies import require_member\n', 'src/chaptera_server/worker.py', 'imports HTTP'),
        ('from fastapi import APIRouter\nrouter = APIRouter()\n', 'src/chaptera_server/worker.py', 'APIRouter'),
        ('@app.get("/bad")\ndef handler(): pass\n', 'src/chaptera_server/api.py', 'HTTP endpoint decorators'),
    ]
    for source, path, needle in cases:
        errors = architecture.scan_python(Path(path), source, path_relative=Path(path))
        assert any(needle in error for error in errors), (path, errors)


def test_http_paths_and_methods_are_unique_and_stable(tmp_path):
    store = QueueStore(tmp_path / 'jobs.sqlite3')
    app = create_app(Settings(db_path=store.db_path, team_tokens={'alice': 'alice-secret'}), store)
    # FastAPI >=0.137 uses lazy _IncludedRouter objects in app.routes. Inspect
    # the canonical APIRouters instead of version-dependent app internals.
    http_routes = [
        route
        for owner in (books, health, identity, imports, jobs, editorial, quota, session, source_books)
        for route in owner.router.routes
        if isinstance(route, APIRoute)
    ]
    routes = [(method, route.path) for route in http_routes for method in sorted(route.methods or ())]
    # OpenAPI is the public app-level inventory on both lazy/flattened routers.
    visible = {
        (method.upper(), path)
        for path, operations in app.openapi()['paths'].items()
        for method in operations
        if method.upper() in {'GET', 'POST', 'PUT', 'DELETE', 'PATCH'}
    }
    assert visible == ROUTE_CONTRACT
    assert len(routes) == len(set(routes)), 'Duplicate handlers shadow one another'
    assert set(routes) == ROUTE_CONTRACT
    for route in http_routes:
        # Login/restore/logout validate session credentials in their own router.
        if route.path in {'/healthz', '/v1/session', '/v1/session/logout',
                              '/v1/session/password', '/v1/session/password/change'}:
            continue
        # Import preview authenticates at router level, others at endpoint level.
        assert any(dep.call is require_member for dep in route.dependant.dependencies), (
            route.path, 'All /v1 endpoints must require team authentication'
        )


def test_service_keeps_authentication_status_and_legacy_schema(tmp_path):
    store = QueueStore(tmp_path / 'jobs.sqlite3')
    app = create_app(Settings(db_path=store.db_path, team_tokens={'alice': 'alice-secret'}), store)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/healthz')).status_code == 200
            assert (await client.get('/v1/jobs')).status_code == 401
            assert (await client.post('/v1/imports/preview')).status_code == 401
            assert (await client.post('/v1/books')).status_code == 401
            assert (await client.post('/v1/jobs', headers={'X-API-Key': 'alice-secret'}, json={'chapter_id': '1', 'source_html': '<p>文本</p>'})).status_code == 201

    asyncio.run(check())
