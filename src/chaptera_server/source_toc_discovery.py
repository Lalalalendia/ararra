"""Exact-scope public TOC discovery, without guessing site AJAX APIs."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from .source_catalog_med import Chapter, IngestError, make_adapter, validate_url
from .source_toc_pages import (
    MAX_TOC_PAGES, MAX_TOC_CHAPTERS,
    declared_royalroad_count, pagination_links,
)

MAX_TOC_HTML_CHARS = 2_000_000


def merge_observed_pages(
    source: str, book_url: str, pages: list[tuple[int, str, str]],
) -> tuple[tuple[Chapter, ...], tuple[str, ...], int | None, str]:
    adapter = make_adapter(source, book_url)
    found_ids: dict[str, Chapter] = {}
    found_numbers: dict[int, Chapter] = {}
    collected: list[Chapter] = []
    declared: int | None = None
    for number, url, html in pages:
        if not 0 < len(html) <= MAX_TOC_HTML_CHARS:
            raise IngestError("catalog_incomplete", "TOC page is empty or too large")
        count = declared_royalroad_count(source, html)
        if count is not None:
            if declared is not None and declared != count:
                raise IngestError("catalog_conflict", "Inconsistent advertised chapter counts")
            declared = count
        for ch in adapter.parse_catalog(html):
            if ch.source_id in found_ids:
                prior = found_ids[ch.source_id]
                if prior.url != ch.url:
                    raise IngestError("catalog_conflict", "Source ID changes URL across pages")
                if source != "royalroad" and prior.ordinal != ch.ordinal:
                    raise IngestError("catalog_conflict", "Source ordinal changes across pages")
                continue
            # Royal Road's official TOC order also includes unnumbered
            # prologues, interludes, and chapters with Roman numerals.
            ordinal = len(collected) + 1 if source == "royalroad" else ch.ordinal
            if ordinal in found_numbers:
                raise IngestError("catalog_conflict", "Two chapters share one ordinal")
            item = Chapter(
                ordinal, ch.title, ch.url, ch.source_id, ch.entry_type,
                ch.author_number, ch.classification_source,
                ch.classification_confidence,
            )
            found_ids[ch.source_id] = item
            found_numbers[ordinal] = item
            collected.append(item)
            if len(collected) > MAX_TOC_CHAPTERS:
                raise IngestError("catalog_incomplete", "Over 2000 chapters in observed TOC")
    if not collected:
        raise IngestError("catalog_missing", "No source-owned chapter links")
    if declared is not None and declared != len(collected):
        raise IngestError("catalog_incomplete",
                          f"Source advertises {declared} chapters, found {len(collected)}")
    ordered = tuple(sorted(collected, key=lambda x: x.ordinal))
    # This is explicitly NOT a promise that the website's unpublished,
    # paginated-with-JavaScript or stubbed chapters were available.
    coverage = "declared_count_matched" if declared is not None else "observed_html_only"
    return ordered, tuple(url for _, url, _ in pages), declared, coverage


async def discover_pages(
    source: str, book_url: str, fetch_html: Callable[[str], Awaitable[str]],
) -> tuple[tuple[Chapter, ...], tuple[str, ...], int | None, str]:
    validate_url(book_url, source)
    if "page" in parse_qs(urlsplit(book_url).query):
        raise IngestError("catalog_incomplete", "Use book root URL, not page N")
    initial = await fetch_html(book_url)
    pages: dict[int, tuple[str, str]] = {1: (book_url, initial)}
    scanned: set[int] = set()
    while len(scanned) < len(pages):
        current = min(set(pages) - scanned)
        url, body = pages[current]
        next_pages = pagination_links(source, book_url, body)
        scanned.add(current)
        for n, target in sorted(next_pages.items()):
            if n == 1:
                continue
            if n in pages:
                if pages[n][0] != target:
                    raise IngestError("catalog_conflict", "TOC page URL changed")
                continue
            if len(pages) >= MAX_TOC_PAGES:
                raise IngestError("catalog_incomplete", "TOC has over 12 pages")
            pages[n] = (target, await fetch_html(target))
    if set(pages) != set(range(1, max(pages) + 1)):
        raise IngestError("catalog_incomplete", "TOC skipped a pagination page")
    return merge_observed_pages(
        source, book_url,
        [(n, pages[n][0], pages[n][1]) for n in sorted(pages)],
    )
