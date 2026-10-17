"""Pure MED adapter and ReadEra TOC/collection contract tests (offline)."""
import asyncio
from dataclasses import replace

import pytest

from chaptera_server.source_catalog_med import IngestError, make_adapter, validate_url
from chaptera_server.source_collect import catalog_from_html, collect_selected, fetch_catalog

URL = "https://www.royalroad.com/fiction/142007/aura"
CATALOG = """<div id="chapters">
<a href="/fiction/142007/aura/chapter/60001/1-first">1 - First</a>
<a href="/fiction/142007/aura/chapter/60002/2-second">2 - Second</a>
<a href="/fiction/123456/other/chapter/9001/3-foreign">3 - Foreign</a>
</div>"""
BODY1 = '<div class="chapter-content"><p>' + ('First hero walks on. ' * 20) + '</p></div>'
BODY2 = '<div class="chapter-content"><p>' + ('Second hero arrives. ' * 20) + '</p></div>'


def test_med_catalog_selects_only_same_book_and_stable_ids():
    cat = catalog_from_html("royalroad", URL, CATALOG)
    assert [(ch.ordinal, ch.source_id) for ch in cat.chapters] == [(1, "60001"), (2, "60002")]
    assert len(cat.catalog_sha256) == 64


def test_med_rejects_untrusted_urls():
    for url in ("http://www.royalroad.com/fiction/142007/aura",
                "https://evil.invalid/fiction/142007/aura",
                "https://www.royalroad.com@evil.invalid/fiction/142007/aura"):
        with pytest.raises(ValueError):
            validate_url(url, "royalroad")


def test_conflicting_chapter_numbers_never_silently_deduplicated():
    bad = CATALOG.replace("2 - Second", "1 - Second").replace("/2-second", "/1-second")
    with pytest.raises(IngestError):
        catalog_from_html("royalroad", URL, bad)


def test_exact_selection_and_successful_ordered_export():
    async def fake(url):
        if url == URL:
            return CATALOG
        return BODY1 if "/60001/" in url else BODY2
    async def exercise():
        cat = await fetch_catalog("royalroad", URL, fake)
        out = await collect_selected(cat, [2, 1], fake, expected_catalog_sha256=cat.catalog_sha256)
        assert out.complete
        assert [c.ordinal for c in out.chapters] == [1, 2]
        assert out.as_txt().decode().index("Глава 1.") < out.as_txt().decode().index("Глава 2.")
    asyncio.run(exercise())


def test_partial_chapter_is_not_a_complete_book():
    async def fake(url):
        return BODY1 if "/60001/" in url else "<h1>Just a moment, verify you are human</h1>"
    async def exercise():
        cat = catalog_from_html("royalroad", URL, CATALOG)
        out = await collect_selected(cat, [1, 2], fake, expected_catalog_sha256=cat.catalog_sha256)
        assert not out.complete
        assert [n for n, _ in out.failures] == [2]
        with pytest.raises(ValueError, match="Partial"):
            out.as_txt()
    asyncio.run(exercise())


def test_duplicate_body_stays_visible_as_conflict():
    async def fake(url):
        return BODY1
    async def exercise():
        cat = catalog_from_html("royalroad", URL, CATALOG)
        out = await collect_selected(cat, [1, 2], fake, expected_catalog_sha256=cat.catalog_sha256)
        assert not out.complete
        assert len(out.chapters) == 1
        assert out.failures[0][0] == 2
    asyncio.run(exercise())


def test_stale_toc_and_ambiguous_selection_rejected_before_fetch():
    async def nofetch(url):
        raise AssertionError("Unexpected outbound request")
    async def exercise():
        cat = catalog_from_html("royalroad", URL, CATALOG)
        for selected, digest in (([1], "old"), ([1, 1], cat.catalog_sha256), ([8], cat.catalog_sha256)):
            with pytest.raises(ValueError):
                await collect_selected(cat, selected, nofetch, expected_catalog_sha256=digest)
    asyncio.run(exercise())
