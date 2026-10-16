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
    ('POST', '/v1/books'),
    ('GET', '/v1/books/{book_id}'),
    ('PUT', '/v1/books/{book_id}/glossary'),
    ('PUT', '/v1/books/{book_id}/chapters/{chapter_id}'),
    ('GET', '/v1/books/{book_id}/chapters/{chapter_id}'),
    ('PUT', '/v1/books/{book_id}/chapters/{chapter_id}/accepted-translation'),
    ('GET', '/v1/books/{book_id}/chapters/{chapter_id}/context'),
    ('POST', '/v1/books/{book_id}/chapters/{chapter_id}/translate'),
    ('POST', '/v1/imports/preview'),
    ('POST', '/v1/jobs'),
    ('GET', '/v1/jobs'),
    ('GET', '/v1/jobs/{job_id}'),
    ('GET', '/v1/jobs/{job_id}/events'),
    ('POST', '/v1/jobs/{job_id}/cancel'),
}


def test_repo_architecture_boundaries_pass():
    assert architecture.inspect_repository(ROOT) == []


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
    # Recent FastAPI versions expose a private _IncludedRouter marker in app.routes;
    # only actual APIRoute instances represent reachable HTTP endpoints.
    http_routes = [route for route in app.routes if isinstance(route, APIRoute)]
    routes = [(method, route.path) for route in http_routes for method in sorted(route.methods or ())]
    assert len(routes) == len(set(routes)), 'Duplicate handlers shadow one another'
    assert set(routes) == ROUTE_CONTRACT
    for route in http_routes:
        if route.path == '/healthz':
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
