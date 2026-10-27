"""Archive-only Erida: disable website capture, preserve real ZIP workflow."""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore

FIXTURE = Path(__file__).parent / "fixtures" / "full_cycle_actual_exporter_20261017.zip.b64"


def test_archive_only_mode_keeps_full_cycle_import_and_editor_routes(tmp_path):
    settings = Settings(
        db_path=tmp_path / "archive-only.sqlite3",
        team_tokens={"chief": "chief-test-token", "operator": "operator-test-token"},
        admin_member_id="chief", archive_only=True,
    )
    app = create_app(settings, QueueStore(settings.db_path))
    routes = {route.path for route in app.routes}
    assert "/v1/imports/preview" in routes
    assert "/v1/imports/stages" in routes
    assert "/v1/books" in routes
    assert "/v1/books/{book_id}/chapters/{chapter_id}" in routes
    assert "/v1/source-books/sites" not in routes
    assert not any(path.startswith("/v1/source-books/") for path in routes)
    assert not hasattr(app.state, "public_source_fetcher")
    assert not hasattr(app.state, "source_job_store")
    assert not hasattr(app.state, "source_request_limiter")
    fixture = base64.b64decode(FIXTURE.read_text(encoding="ascii"))
    headers = {"X-API-Key": "operator-test-token"}
    files = {"file": ("from-full-cycle.zip", fixture, "application/zip")}

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://offline.test",
        ) as client:
            ui = await client.get("/app/import")
            assert ui.status_code == 200
            assert 'id="source-import"' not in ui.text
            assert "Full Cycle App" in ui.text
            noauth = await client.post("/v1/imports/preview", files=files)
            assert noauth.status_code == 401
            preview = await client.post("/v1/imports/preview", files=files, headers=headers)
            assert preview.status_code == 200, preview.text
            assert preview.json()["chapter_count"] == 3
            assert preview.json()["source_archive"]["boundaries"] == "manifest"
            blocked = await client.get("/v1/source-books/sites", headers=headers)
            assert blocked.status_code == 404
            stage = await client.post("/v1/imports/stages", files=files, headers=headers)
            assert stage.status_code == 201, stage.text
            stage_id = stage.json()["stage_id"]
            choices = [{
                "sequence": idx + 1, "action": "create",
                "chapter_id": f"chapter-{number}", "chapter_index": number,
                "chapter_path": f"chapters/{number}.xhtml", "expected_revision": 0,
            } for idx, number in enumerate((7, 9, 10))]
            receipt = await client.post(
                f"/v1/imports/stages/{stage_id}/confirm",
                headers=headers,
                json={
                    "book_id": "archive-only-book",
                    "book_mode": "create",
                    "title": "Offline fixture",
                    "owner_member_id": "operator",
                    "prefix_policy": "reject",
                    "chapters": choices,
                },
            )
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()["imported_count"] == 3
            raw = await client.get(
                "/v1/imports/books/archive-only-book/chapters/chapter-9/archive-original",
                headers=headers,
            )
            assert raw.status_code == 200, raw.text
            assert raw.json()["original_text"] != raw.json()["working_text"]
            chapter = await client.get(
                "/v1/books/archive-only-book/chapters/chapter-9", headers=headers,
            )
            assert chapter.status_code == 200
    asyncio.run(run())


def test_full_mode_remains_compatible(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "full.sqlite3"))
    assert "/v1/source-books/sites" in {route.path for route in app.routes}
    assert hasattr(app.state, "public_source_fetcher")


def test_archive_only_source_worker_fails_closed(tmp_path):
    environment = dict(os.environ)
    environment.update(CHAPTERA_ARCHIVE_ONLY="1", CHAPTERA_DB_PATH=str(tmp_path / "must-not-open.sqlite3"))
    result = subprocess.run(
        [sys.executable, "-m", "chaptera_server.cli", "source-worker"],
        capture_output=True, text=True, check=False, timeout=15, env=environment,
    )
    assert result.returncode == 2
    assert "source-worker unavailable" in result.stderr
    assert not (tmp_path / "must-not-open.sqlite3").exists()


@pytest.mark.parametrize("flag,expected", [
    ("1", True), ("yes", True), ("off", False), ("0", False),
])
def test_archive_only_env(monkeypatch, flag, expected):
    monkeypatch.setenv("CHAPTERA_ARCHIVE_ONLY", flag)
    assert Settings.from_env().archive_only is expected


def test_archive_only_env_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("CHAPTERA_ARCHIVE_ONLY", "maybe")
    with pytest.raises(ValueError, match="CHAPTERA_ARCHIVE_ONLY"):
        Settings.from_env()
