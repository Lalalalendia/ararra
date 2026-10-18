"""Safe, transport-independent book collection preview built on MED parsers.

A network adapter is injected explicitly: this module never makes outbound
requests and never writes to BookLibrary or bypasses the staged import gate.
"""
from __future__ import annotations

import hashlib
import html
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .source_catalog_med import IngestError, make_adapter, validate_url, source_book_id
from .source_toc_discovery import merge_observed_pages, discover_pages

MAX_CHAPTERS = 100
MAX_HTML_CHARS = 2_000_000
MAX_BODY_CHARS = 1_000_000

FetchHtml = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class SourceChapter:
    ordinal: int
    title: str
    source_id: str
    url: str


@dataclass(frozen=True)
class SourceCatalog:
    source: str
    book_url: str
    chapters: tuple[SourceChapter, ...]
    catalog_sha256: str
    page_urls: tuple[str, ...] = ()
    declared_chapter_count: int | None = None
    coverage_state: str = "observed_html_only"


@dataclass(frozen=True)
class CollectedChapter:
    ordinal: int
    title: str
    url: str
    source_id: str
    body: str
    sha256: str


@dataclass(frozen=True)
class CollectedBook:
    catalog_sha256: str
    requested: tuple[int, ...]
    chapters: tuple[CollectedChapter, ...]
    failures: tuple[tuple[int, str], ...]

    @property
    def complete(self) -> bool:
        return not self.failures and len(self.chapters) == len(self.requested)

    def as_import_html(self) -> bytes:
        """Generate markup with explicit heading nodes for existing ImportSessions.

        A body line that resembles a chapter heading remains a <p>, so the
        HTML importer cannot accidentally split a chapter mid-paragraph.
        """
        if not self.complete:
            raise ValueError("Partial collection cannot be staged as a complete selection")
        blocks = ["<html><body>"]
        for item in self.chapters:
            blocks.append("<h1>Глава " + str(item.ordinal) + ". " +
                          html.escape(item.title) + "</h1>")
            for paragraph in item.body.split("\n\n"):
                if paragraph.strip():
                    escaped_lines = [html.escape(line) for line in paragraph.split("\n")]
                    blocks.append("<p>" + "<br/>".join(escaped_lines) + "</p>")
        blocks.append("</body></html>")
        return "\n".join(blocks).encode("utf-8")

    def as_txt(self) -> bytes:
        if not self.complete:
            raise ValueError("Partial collection cannot be exported as a complete book")
        blocks = [
            f"Глава {item.ordinal}. {item.title}\n\n{item.body}"
            for item in self.chapters
        ]
        return ("\n\n".join(blocks) + "\n").encode("utf-8")


def _build_catalog(source, book_url, discovery) -> SourceCatalog:
    entries, pages, declared, coverage = discovery
    chapters = tuple(
        SourceChapter(ch.ordinal, ch.title, ch.source_id, ch.url)
        for ch in entries
    )
    canonical = "\n".join(
        f"{ch.ordinal}\t{ch.source_id}\t{ch.url}" for ch in chapters
    )
    return SourceCatalog(
        source, book_url, chapters,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        pages, declared, coverage,
    )


def catalog_from_html(source: str, book_url: str, page_html: str) -> SourceCatalog:
    validate_url(book_url, source)
    return _build_catalog(
        source, book_url,
        merge_observed_pages(source, book_url, [(1,book_url,page_html)]),
    )


async def fetch_catalog(source: str, book_url: str, fetch_html: FetchHtml) -> SourceCatalog:
    validate_url(book_url, source)
    return _build_catalog(
        source, book_url,
        await discover_pages(source, book_url, fetch_html),
    )

async def collect_selected(
    catalog: SourceCatalog,
    ordinals: list[int],
    fetch_html: FetchHtml,
    *,
    expected_catalog_sha256: str,
    min_body_chars: int = 100,
) -> CollectedBook:
    """Collect exact chosen chapters, retaining explicit failure receipts.

    The caller must separately supply a transport with DNS pinning, redirect
    refusal, public-address checks, content-type/size limits and rate control.
    The catalog digest prevents accidental selection against an older TOC.
    """
    if expected_catalog_sha256 != catalog.catalog_sha256:
        raise ValueError("Stale book catalog; preview again")
    if not ordinals or len(ordinals) > MAX_CHAPTERS:
        raise ValueError("Choose 1..100 chapters per batch")
    if len(set(ordinals)) != len(ordinals):
        raise ValueError("Duplicate chapter selection")
    index = {ch.ordinal: ch for ch in catalog.chapters}
    if any(n not in index for n in ordinals):
        raise ValueError("Selected chapter not found in catalog")
    adapter = make_adapter(catalog.source, catalog.book_url)
    completed: list[CollectedChapter] = []
    failed: list[tuple[int, str]] = []
    for n in sorted(ordinals):
        ch = index[n]
        # URL and stable book ownership must still be rechecked at the handoff.
        validate_url(ch.url, catalog.source)
        # Require exact originating book ID on every discovered link.
        if adapter.book_id != source_book_id(ch.url, catalog.source):
            raise ValueError("Cross-book chapter URL rejected")
        try:
            page = await fetch_html(ch.url)
            if not 0 < len(page) <= MAX_HTML_CHARS:
                raise ValueError("Chapter HTML outside safety limits")
            body = adapter.parse_body(page)
            if not min_body_chars <= len(body) <= MAX_BODY_CHARS:
                raise ValueError("Missing, truncated or oversized chapter text")
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            completed.append(CollectedChapter(n, ch.title, ch.url, ch.source_id, body, digest))
        except IngestError as exc:
            if exc.status == "site_busy":
                # Admission is transient. Do not mislabel this selected
                # chapter as missing/blocked source content.
                raise
            failed.append((n, str(exc)[:160]))
        except ValueError as exc:
            failed.append((n, str(exc)[:160]))
    # An identical body in two different chapters needs review, not silent loss.
    seen: dict[str, int] = {}
    for item in completed:
        if item.sha256 in seen:
            failed.append((item.ordinal, f"Duplicate body of chapter {seen[item.sha256]}"))
        else:
            seen[item.sha256] = item.ordinal
    if failed:
        blocked = {n for n, _ in failed}
        completed = [c for c in completed if c.ordinal not in blocked]
    return CollectedBook(catalog.catalog_sha256, tuple(sorted(ordinals)), tuple(completed), tuple(sorted(failed)))
