"""RE-02 TOC completeness: Royal Road nonnumeric titles and bounded paging."""
from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_catalog_med import IngestError
from chaptera_server.source_collect import catalog_from_html, fetch_catalog
from chaptera_server.source_site_storage import SourceCatalogStore
from chaptera_server.source_toc_pages import (
    page_number_for_link, pagination_links,
)
from chaptera_server.storage import QueueStore

BOOK = "https://www.royalroad.com/fiction/142007/aura"
SECOND = BOOK + "?page=2"
ONE = """<div class="book"><strong>4 Chapters</strong></div>
<div id="chapters">
  <a href="/fiction/142007/aura/chapter/60001/prologue">Prologue: Before the storm</a>
  <a href="/fiction/142007/aura/chapter/60002/roman">Chapter I: The storm</a>
</div>
<nav class="pagination"><a href="?page=2">2</a></nav>"""
TWO = """<div id="chapters">
  <a href="/fiction/142007/aura/chapter/60003/interlude">Interlude: The first rain</a>
  <a href="/fiction/142007/aura/chapter/60004/4-end">Chapter 4 - Aftermath</a>
</div>
<nav class="pagination"><a href="?page=1">1</a></nav>"""


async def crawl(chapters=None):
    calls=[]
    page2 = chapters or TWO
    async def fetch(url):
        calls.append(url)
        if url == BOOK: return ONE
        if url == SECOND: return page2
        raise AssertionError("unexpected fetch: " + url)
    return await fetch_catalog("royalroad",BOOK,fetch),calls


def test_royalroad_uses_toc_positions_not_author_numbering():
    page = """<strong>4 Chapters</strong>
    <div id="chapters">
    <a href="/fiction/142007/aura/chapter/60001/prologue">Prologue</a>
    <a href="/fiction/142007/aura/chapter/60002/roman">Chapter I: Rain</a>
    <a href="/fiction/142007/aura/chapter/60003/roman-two">Chapter II: Thunder</a>
    <a href="/fiction/142007/aura/chapter/60004/interlude">Interlude before book two</a>
    </div>"""
    cat=catalog_from_html("royalroad",BOOK,page)
    assert len(cat.chapters)==4
    assert [(c.ordinal,c.title,c.source_id) for c in cat.chapters]==[
        (1,"Prologue","60001"),(2,"Chapter I: Rain","60002"),
        (3,"Chapter II: Thunder","60003"),
        (4,"Interlude before book two","60004"),
    ]
    assert cat.coverage_state=="declared_count_matched"
    assert cat.declared_chapter_count==4
    assert cat.page_urls==(BOOK,)


def test_paged_catalog_merges_exact_source_and_avoids_refetch():
    cat,calls=asyncio.run(crawl())
    assert calls==[BOOK,SECOND]
    assert cat.page_urls==(BOOK,SECOND)
    assert cat.coverage_state=="declared_count_matched"
    assert cat.declared_chapter_count==4
    assert [(x.ordinal,x.source_id) for x in cat.chapters]==[
        (1,"60001"),(2,"60002"),(3,"60003"),(4,"60004"),
    ]


def test_site_advertised_count_mismatch_fails_closed():
    bad=TWO.replace('60004/4-end','99999/4-end').replace(
        '<a href="/fiction/142007/aura/chapter/99999/4-end">Chapter 4 - Aftermath</a>','')
    async def invalid():
        await crawl(bad)
    with pytest.raises(IngestError,match="advertises 4 chapters"):
        asyncio.run(invalid())


def test_pagination_rejects_private_or_cross_book_and_unsupported_params():
    for bad in (
        'http://127.0.0.1/?page=2',
        'https://www.royalroad.com/fiction/123456/nope?page=2',
        'https://www.royalroad.com/fiction/142007/aura?page=2&redirect=evil',
        'https://www.royalroad.com/fiction/142007/aura?page=1000',
    ):
        html='<div id="chapters"><a href="/fiction/142007/aura/chapter/60001/prologue">Prologue</a></div>' + (
            '<nav class="pagination"><a href="'+bad+'">2</a></nav>'
        )
        with pytest.raises(IngestError,match="pagination"):
            pagination_links("royalroad",BOOK,html)
    assert page_number_for_link("royalroad",BOOK,SECOND)==2


def test_pagination_rejects_holes_without_guessing_unlinked_pages():
    html=ONE.replace('?page=2','?page=3').replace('>2</a>','>3</a>')
    async def fetch(url):
        if url==BOOK:return html
        if url==BOOK+"?page=3":return TWO
        raise AssertionError("must not guess and request unlinked page 2")
    with pytest.raises(IngestError,match="skipped"):
        asyncio.run(fetch_catalog("royalroad",BOOK,fetch))


def test_dynamic_load_more_hint_blocks_incomplete_catalog():
    html=ONE.replace(
        '<nav class="pagination"><a href="?page=2">2</a></nav>',
        '<div id="chapters"><button class="load-more">Show more</button></div>',
    )
    async def fetch(url):
        assert url==BOOK
        return html
    with pytest.raises(IngestError,match="dynamic"):
        asyncio.run(fetch_catalog("royalroad",BOOK,fetch))


def test_cross_page_chapter_id_collision_rejected():
    bad=TWO.replace("60003/interlude","60001/interlude")
    with pytest.raises(IngestError,match="changes URL"):
        asyncio.run(crawl(bad))


def test_sqlite_migrates_old_catalog_schema_and_preserves_coverage(tmp_path):
    db=QueueStore(tmp_path/"toc.sqlite3")
    # Mimic the previously deployed schema without the additive columns.
    with db._connect() as con:
        con.execute("""CREATE TABLE source_book_catalogs(
            id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, source TEXT NOT NULL,
            book_url TEXT NOT NULL, catalog_sha256 TEXT NOT NULL,
            chapters_json TEXT NOT NULL, created_at REAL NOT NULL,
            expires_at REAL NOT NULL)""")
    storage=SourceCatalogStore(db)
    cat,calls=asyncio.run(crawl())
    preview=storage.save("alice",cat)
    assert preview["toc_page_count"]==2
    assert preview["toc_page_urls"]==[BOOK,SECOND]
    assert preview["declared_chapter_count"]==4
    assert preview["coverage_state"]=="declared_count_matched"
    other=storage.load("alice",preview["catalog_id"])
    assert other==cat
    with db._connect() as con:
        con.execute("UPDATE source_book_catalogs SET declared_chapter_count=777 WHERE id=?",
                    (preview["catalog_id"],))
    with pytest.raises(Exception,match="invalid|diverged"):
        storage.load("alice",preview["catalog_id"])


def test_actor_route_scans_pages_and_exposes_explicit_scope(tmp_path):
    db=QueueStore(tmp_path/"api.sqlite3")
    app=create_app(Settings(db_path=db.db_path,team_tokens={
        "alice":"alice-key","bob":"bob-key",
    }),db)
    class Fake:
        def __init__(self): self.calls=[]
        async def fetch(self,source,url):
            self.calls.append(url)
            return ONE if url==BOOK else TWO if url==SECOND else None
    fake=Fake()
    app.state.public_source_fetcher=fake
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),base_url="http://test",
        ) as client:
            r=await client.post("/v1/source-books/catalogs",
                headers={"X-API-Key":"alice-key"},
                json={"source":"royalroad","book_url":BOOK})
            assert r.status_code==201,r.text
            data=r.json()
            assert data["chapter_count"]==4
            assert data["toc_page_count"]==2
            assert data["coverage_state"]=="declared_count_matched"
            assert data["scope"]=="observed_public_html_only"
            assert fake.calls==[BOOK,SECOND]
            foreign=await client.get("/v1/source-books/catalogs/"+data["catalog_id"],
                headers={"X-API-Key":"bob-key"})
            assert foreign.status_code==404
    asyncio.run(exercise())
