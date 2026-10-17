"""ReadEra Jinja2 UI acceptance: security, packaged files and API coexistence."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore


def test_jinja_pages_render_without_credentials_but_never_expose_team_state(tmp_path):
    config = Settings(
        db_path=tmp_path / "ui.sqlite3",
        team_tokens={"owner": "secret-admin-key", "moderator": "secret-mod-key"},
        admin_member_id="owner",
    )
    app = create_app(config, QueueStore(config.db_path))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://readera.test"
        ) as client:
            for path in ["/app", "/app/", "/app/overview", "/app/books", "/app/import", "/app/team", "/app/jobs"]:
                response = await client.get(path)
                assert response.status_code == 200, (path, response.text[:1000])
                assert "text/html" in response.headers["content-type"]
                assert "no-store" in response.headers["cache-control"]
                assert "script-src 'self'" in response.headers["content-security-policy"]
                assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
                assert 'data-initial-page="' in response.text
                assert 'src="/app-static/js/readera.mjs"' in response.text
                assert "secret-admin-key" not in response.text
                assert "secret-mod-key" not in response.text
                assert "owner" not in response.text
            assert (await client.get("/app/missing")).status_code == 404
            assert (await client.get("/v1/books")).status_code == 401
            assert (await client.get("/v1/admin/members")).status_code == 401
            assert (await client.get("/v1/books", headers={"X-API-Key": "secret-mod-key"})).status_code == 200

            css = await client.get("/app-static/css/books-platform-chapter-import.css")
            assert css.status_code == 200
            assert "chapter-import-upload" in css.text
            assert "Vendored unchanged from books_platform" in css.text
            js = await client.get("/app-static/js/readera.mjs")
            assert js.status_code == 200
            assert "import-model.mjs" in js.text
            assert "localStorage" not in js.text
            assert "sessionStorage" not in js.text
            model = await client.get("/app-static/js/import-model.mjs")
            assert model.status_code == 200
            assert "validateDraft" in model.text

    asyncio.run(scenario())


def test_page_template_uses_books_platform_components_without_paid_features():
    templates = Path(__file__).resolve().parents[1] / "src" / "chaptera_server" / "web_ui" / "templates"
    source = (templates / "shell.html").read_text(encoding="utf-8")
    assert 'class="chapter-import-upload"' in source
    assert 'class="chapter-import-history' in source
    assert "books_platform" in source
    assert "is_paid" not in source
    assert "price_amount" not in source
    assert "data-page" in source
    assert "React" not in source


def test_bundled_resource_files_exist_in_source_tree():
    root = Path(__file__).resolve().parents[1]
    files = (
        "src/chaptera_server/web_ui/templates/shell.html",
        "src/chaptera_server/web_ui/static/css/books-platform-chapter-import.css",
        "src/chaptera_server/web_ui/static/css/readera.css",
        "src/chaptera_server/web_ui/static/js/import-model.mjs",
        "src/chaptera_server/web_ui/static/js/readera.mjs",
    )
    for item in files:
        assert (root / item).is_file(), item
