"""RE-05: the library only advertises source-backed chapter progress."""
from __future__ import annotations

import asyncio

import httpx

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore


def test_book_directory_counts_follow_live_source_revisions_and_are_private(tmp_path):
    settings = Settings(
        db_path=tmp_path / "catalog.sqlite3",
        team_tokens={"admin": "admin-key", "moderator": "moderator-key"},
        admin_member_id="admin",
    )
    app = create_app(settings, QueueStore(settings.db_path))
    library = app.state.library
    library.create_book("novel", "Настоящая книга", owner_member_id="moderator", actor_id="admin")
    library.create_book("empty", "Пустая", owner_member_id="moderator", actor_id="admin")

    async def get_catalog(client, key):
        result = await client.get("/v1/books", headers={"X-API-Key": key})
        assert result.status_code == 200, result.text
        return {item["book_id"]: item for item in result.json()["books"]}

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://readera.test"
        ) as client:
            assert (await client.get("/v1/books")).status_code == 401
            for key in ("admin-key", "moderator-key"):
                books = await get_catalog(client, key)
                assert books["novel"]["chapter_count"] == 0
                assert books["novel"]["accepted_current_count"] == 0
                assert books["empty"]["chapter_count"] == 0
                assert "source_html" not in str(books)
                assert "published" not in str(books)

            for index, chapter in ((12, "first"), (13, "second")):
                library.put_chapter(
                    "novel", chapter, chapter_index=index,
                    chapter_path=f"chapters/{index}.xhtml",
                    source_html=f"<h1>Глава {index}</h1><p>原文</p>",
                    expected_revision=0,
                )
            catalog = await get_catalog(client, "moderator-key")
            assert catalog["novel"]["chapter_count"] == 2
            assert catalog["novel"]["accepted_current_count"] == 0

            library.accept_translation(
                "novel", "first", "<p>Перевод</p>", expected_revision=0,
                expected_source_revision=1, actor_id="moderator",
            )
            catalog = await get_catalog(client, "admin-key")
            assert catalog["novel"]["accepted_current_count"] == 1
            assert catalog["novel"]["chapter_count"] == 2

            # Accepted from the old source does NOT mean the current source
            # has a ready translation: no optimistic production badges.
            library.put_chapter(
                "novel", "first", chapter_index=12,
                chapter_path="chapters/12.xhtml",
                source_html="<p>Изменённый оригинал</p>",
                expected_revision=1,
            )
            catalog = await get_catalog(client, "moderator-key")
            assert catalog["novel"]["chapter_count"] == 2
            assert catalog["novel"]["accepted_current_count"] == 0
            assert catalog["empty"]["accepted_current_count"] == 0

    asyncio.run(scenario())
