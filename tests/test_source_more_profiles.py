"""RE-02 add-on profiles: offline domain, TOC, body, and browser API gates.

No live crawling, auth bypass, browser emulation, or model requests.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_catalog_med import (
    IngestError, chapter_identity, make_adapter, source_book_id, validate_url,
)
from chaptera_server.source_collect import (
    catalog_from_html, collect_selected, fetch_catalog,
)
from chaptera_server.source_site_profiles import SITE_PROFILES, public_site_profiles
from chaptera_server.storage import QueueStore

EN = "https://en.wikisource.org/wiki/The_Wonderful_Wizard_of_Oz"
RU = "https://ru.wikisource.org/wiki/%D0%9A%D0%BD%D0%B8%D0%B3%D0%B0"
AO3 = "https://archiveofourown.org/works/12345"
INDEX = AO3 + "/navigate"
ONE = "https://archiveofourown.org/works/12345/chapters/7001"
TWO = "https://archiveofourown.org/works/12345/chapters/7002"
EN_A = EN + "/Chapter_1"
EN_B = EN + "/Chapter_2"
RU_A = RU + "/%D0%93%D0%BB%D0%B0%D0%B2%D0%B0_I"

EN_TOC = """
<div id="mw-content-text"><div class="mw-parser-output">
  <div class="mw-editsection"><a href="/wiki/Evil">Edit</a></div>
  <ol>
    <li><a href="/wiki/The_Wonderful_Wizard_of_Oz/Chapter_1">Chapter I - The Cyclone</a></li>
    <li><a href="/wiki/The_Wonderful_Wizard_of_Oz/Chapter_2">Chapter II - The Council</a></li>
    <li><a href="/wiki/Another_Book/Chapter_3">Never include foreign chapter</a></li>
    <li><a href="https://evil.invalid/wiki/The_Wonderful_Wizard_of_Oz/Chapter_4">Bad site</a></li>
  </ol>
</div></div>
"""
RU_TOC = """
<div id="mw-content-text"><div class="mw-parser-output">
  <ul><li><a href="/wiki/%D0%9A%D0%BD%D0%B8%D0%B3%D0%B0/%D0%93%D0%BB%D0%B0%D0%B2%D0%B0_I">Глава I. Вступление</a></li></ul>
</div></div>
"""
AO3_ROOT = """
<html><body><h2>Work title</h2>
<a href="/works/12345/navigate">Full-page index</a>
<a href="/works/99999/chapters/7003">Do not import a different work</a>
</body></html>
"""
AO3_INDEX = """
<ol class="chapter index group">
  <li><a href="/works/12345/chapters/7001">Prologue</a></li>
  <li><a href="/works/12345/chapters/7002">Chapter II - Main</a></li>
  <li><a href="/works/99999/chapters/7003">Foreign</a></li>
  <li><a href="https://evil.invalid/works/12345/chapters/7004">Bad host</a></li>
</ol>
"""


def wiki_body(text: str) -> str:
    return (
        '<div id="mw-content-text"><div class="mw-parser-output">'
        '<div class="noprint">Site navigation</div>'
        '<table><tr><td>Not novel</td></tr></table>'
        f'<p>{text}</p><p>{text}</p>'
        '</div></div>'
    )


def ao3_body(text: str) -> str:
    return (
        '<div id="chapters"><div class="chapter">'
        '<div class="userstuff module" role="article">'
        f'<p>{text}</p><p>{text}</p>'
        '</div></div></div>'
        '<div id="feedback">Reader comments should not be included.</div>'
    )


def test_registry_has_exact_six_enabled_profiles_with_bounded_limits():
    expected = {"royalroad", "webnovel", "uukanshu",
                "wikisource_en", "wikisource_ru", "ao3"}
    assert set(SITE_PROFILES) == expected
    listed = public_site_profiles()
    assert {row["id"] for row in listed} == expected
    assert all(row["minimum_delay_seconds"] >= 1.5
               and row["daily_requests"] <= 240 for row in listed)
    assert SITE_PROFILES["wikisource_en"].hosts == frozenset({"en.wikisource.org"})
    assert SITE_PROFILES["ao3"].hosts == frozenset({"archiveofourown.org"})


@pytest.mark.parametrize("source,url,expected", [
    ("wikisource_en", EN, "The_Wonderful_Wizard_of_Oz"),
    ("wikisource_en", EN_A, "The_Wonderful_Wizard_of_Oz"),
    ("wikisource_ru", RU, "Книга"),
    ("wikisource_ru", RU_A, "Книга"),
    ("ao3", AO3, "12345"),
    ("ao3", ONE, "12345"),
])
def test_source_book_identity_is_stable_across_chapter_urls(source, url, expected):
    assert source_book_id(url, source) == expected


@pytest.mark.parametrize("source,url", [
    ("wikisource_en", "https://en.wikisource.org/wiki/Book?redirect=evil"),
    ("wikisource_en", "https://en.wikisource.org/wiki/Book#chapter"),
    ("wikisource_ru", "https://en.wikisource.org/wiki/Book"),
    ("wikisource_ru", "http://ru.wikisource.org/wiki/Book"),
    ("wikisource_en", "https://evil.invalid/wiki/The_Wonderful_Wizard_of_Oz"),
    ("wikisource_en", "https://en.wikisource.org/wiki/Page:Book.pdf/12"),
    ("ao3", "https://archiveofourown.org/works/12345?view_adult=true"),
    ("ao3", "https://archiveofourown.org/works/12345?view_full_work=true"),
    ("ao3", "https://archiveofourown.org@evil.invalid/works/12345"),
    ("ao3", "https://secure.ao3.org/works/12345"),
    ("ao3", "http://archiveofourown.org/works/12345"),
])
def test_mismatched_hosts_or_controls_are_rejected(source,url):
    with pytest.raises(ValueError):
        if source.startswith("wikisource") and "Page:" in url:
            source_book_id(url, source)
        else:
            validate_url(url, source)


def test_wikisource_restricts_to_subpages_of_user_supplied_work_root():
    cat = catalog_from_html("wikisource_en", EN, EN_TOC)
    assert [(x.ordinal, x.source_id, x.title) for x in cat.chapters] == [
        (1, "Chapter_1", "Chapter I - The Cyclone"),
        (2, "Chapter_2", "Chapter II - The Council"),
    ]
    assert cat.coverage_state == "observed_html_only"
    ru = catalog_from_html("wikisource_ru", RU, RU_TOC)
    assert len(ru.chapters) == 1
    assert ru.chapters[0].source_id == "Глава_I"
    assert chapter_identity(EN_B, "wikisource_en", "The_Wonderful_Wizard_of_Oz") == "Chapter_2"
    assert chapter_identity(
        "https://en.wikisource.org/wiki/Another_Book/Chapter_2",
        "wikisource_en", "The_Wonderful_Wizard_of_Oz",
    ) is None
    deep_root = EN + "/Edition_1900"
    with pytest.raises(IngestError, match="No source-owned chapter"):
        catalog_from_html("wikisource_en", deep_root, EN_TOC)


def test_wikisource_body_keeps_prose_and_excludes_navigation():
    body = make_adapter("wikisource_en", EN).parse_body(wiki_body(
        "Dorothy wandered across the quiet fields and returned to the farmhouse."
    ))
    assert "Dorothy wandered" in body
    assert "Site navigation" not in body
    assert "Not novel" not in body
    with pytest.raises(IngestError, match="chapter article"):
        make_adapter("wikisource_ru", RU).parse_body("<html><h1>Access blocked</h1></html>")


def test_ao3_index_uses_observed_full_index_not_guessed_api():
    calls = []
    async def fetch(url):
        calls.append(url)
        return AO3_ROOT if url == AO3 else AO3_INDEX if url == INDEX else None

    cat = asyncio.run(fetch_catalog("ao3", AO3, fetch))
    assert calls == [AO3, INDEX]
    assert cat.page_urls == (AO3, INDEX)
    assert cat.coverage_state == "observed_html_only"
    assert [(c.ordinal, c.source_id, c.title) for c in cat.chapters] == [
        (1, "7001", "Prologue"), (2, "7002", "Chapter II - Main"),
    ]


def test_ao3_requires_work_root_not_chapter_or_navigation_url():
    for url in (ONE, TWO, INDEX, AO3 + "/chapter/7001"):
        with pytest.raises(ValueError, match="canonical work root"):
            make_adapter("ao3", url)


def test_ao3_without_observed_index_fails_without_endpoint_guessing():
    calls = []
    async def fetch(url):
        calls.append(url)
        return "<html><body><h1>Public work, index loaded via JavaScript only</h1></body></html>"
    with pytest.raises(IngestError, match="No source-owned chapter"):
        asyncio.run(fetch_catalog("ao3", AO3, fetch))
    assert calls == [AO3]


def test_ao3_rejects_restricted_body_and_ignores_site_notes():
    adapter = make_adapter("ao3", AO3)
    narrative = "The narrator had arrived to meet the old friend at the station."
    body = adapter.parse_body(ao3_body(narrative))
    assert narrative in body
    assert "Reader comments" not in body
    with pytest.raises(IngestError, match="permission"):
        adapter.parse_body("<html>Only available to registered users</html>")
    with pytest.raises(IngestError, match="No public AO3"):
        adapter.parse_body('<div class="userstuff"><p>Summary only.</p></div>')


def test_added_sources_feed_existing_staged_collection_without_new_network_code():
    async def run(source, book_url, first_html, body_urls):
        async def fetch(url):
            if source == "ao3" and url == INDEX:
                return AO3_INDEX
            if url == book_url:
                return first_html
            return body_urls[url]
        cat = await fetch_catalog(source, book_url, fetch)
        out = await collect_selected(
            cat, list(range(1, len(cat.chapters) + 1)), fetch,
            expected_catalog_sha256=cat.catalog_sha256,
        )
        assert out.complete
        assert len(out.chapters) == len(cat.chapters)
        assert out.as_import_html().startswith(b"<html><body>")
        assert len(out.as_txt()) > 100

    asyncio.run(run("wikisource_en", EN, EN_TOC, {
        EN_A: wiki_body("The first scene gave the readers many important details. " * 6),
        EN_B: wiki_body("The second scene presented a different dramatic situation. " * 6),
    }))
    asyncio.run(run("ao3", AO3, AO3_ROOT, {
        ONE: ao3_body("A fictional narrative begins without a login wall. " * 6),
        TWO: ao3_body("Another chapter continues with wholly new prose. " * 6),
    }))


def test_api_and_jinja_import_picker_share_registry(tmp_path):
    db = QueueStore(tmp_path / "sites.sqlite3")
    app = create_app(
        Settings(db_path=db.db_path, team_tokens={"mod": "test-moderator-key"}),
        db,
    )
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            assert (await client.get("/v1/source-books/sites")).status_code == 401
            reply = await client.get(
                "/v1/source-books/sites", headers={"X-API-Key": "test-moderator-key"},
            )
            assert reply.status_code == 200
            names = {x["id"] for x in reply.json()["sources"]}
            shell = await client.get("/app/import")
            assert shell.status_code == 200
            for name in names:
                assert f'<option value="{name}">' in shell.text
            denied = await client.post(
                "/v1/source-books/catalogs",
                headers={"X-API-Key": "test-moderator-key"},
                json={"source": "some_unknown", "book_url": EN},
            )
            assert denied.status_code == 422
    asyncio.run(check())
