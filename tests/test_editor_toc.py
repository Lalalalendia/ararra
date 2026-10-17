"""ReadEra RE-05 chapter table-of-contents + accepted revision safety."""
from __future__ import annotations

import asyncio

import httpx

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore


def test_toc_is_shared_revision_only_and_follows_source_staleness(tmp_path):
    settings=Settings(db_path=tmp_path/"toc.sqlite3",
                      team_tokens={"admin":"admin-key","writer":"writer-key"},
                      admin_member_id="admin")
    app=create_app(settings,QueueStore(settings.db_path))
    library=app.state.library
    library.create_book("novel","Название",owner_member_id="writer",actor_id="admin")
    for id_,idx in (("beta",20),("alpha",10)):
        library.put_chapter("novel",id_,chapter_index=idx,
                            chapter_path=f"chapters/{idx}.xhtml",
                            source_html=f"<h1>Глава {idx}</h1><p>Оригинал</p>",
                            expected_revision=0)
    library.accept_translation("novel","alpha",translated_html="<p>Перевод</p>",
                               expected_revision=0,expected_source_revision=1,
                               actor_id="writer")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            path="/v1/books/novel/chapters"
            assert (await client.get(path)).status_code == 401
            assert (await client.get("/v1/books/missing/chapters",
                                     headers={"X-API-Key":"writer-key"})).status_code == 404
            for token in ("admin-key","writer-key"):
                response=await client.get(path,headers={"X-API-Key":token})
                assert response.status_code == 200,response.text
                rows=response.json()["chapters"]
                assert [row["chapter_id"] for row in rows] == ["alpha","beta"]
                assert [row["chapter_index"] for row in rows] == [10,20]
                assert rows[0]["accepted_revision"] == 1
                assert rows[0]["accepted_source_revision"] == 1
                assert rows[0]["accepted_current"] is True
                assert rows[1]["accepted_current"] is False
                assert "source_html" not in rows[0]
                assert "accepted_html" not in rows[0]

            # Accepted history is preserved, but stale after a source revision.
            changed=await client.put("/v1/books/novel/chapters/alpha",
                                     headers={"X-API-Key":"writer-key"},
                                     json={"chapter_index":10,"chapter_path":"chapters/10.xhtml",
                                           "source_html":"<h1>Изменённый исходник</h1>",
                                           "expected_revision":1})
            assert changed.status_code == 200,changed.text
            stale=(await client.get(path,headers={"X-API-Key":"writer-key"})).json()
            assert stale["chapters"][0]["source_revision"] == 2
            assert stale["chapters"][0]["accepted_revision"] == 1
            assert stale["chapters"][0]["accepted_current"] is False
            assert stale["chapters"][0]["accepted_source_revision"] == 1
            failed=await client.put(
                "/v1/books/novel/chapters/alpha/accepted-translation",
                headers={"X-API-Key":"writer-key"},
                json={"translated_html":"<p>Новый перевод</p>",
                      "expected_revision":1,"expected_source_revision":1},
            )
            assert failed.status_code == 409,failed.text

    asyncio.run(scenario())
