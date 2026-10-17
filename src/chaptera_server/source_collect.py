"""Safe, transport-independent book collection preview built on MED parsers.

A network adapter is injected explicitly: this module never makes outbound
requests and never writes to BookLibrary or bypasses the staged import gate.
"""
from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .source_catalog_med import IngestError, make_adapter, validate_url, source_book_id

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

    def as_txt(self) -> bytes:
        if not self.complete:
            raise ValueError("Partial collection cannot be exported as a complete book")
        blocks = [
            f"Глава {item.ordinal}. {item.title}\n\n{item.body}"
            for item in self.chapters
        ]
        return ("\n\n".join(blocks) + "\n").encode("utf-8")


def catalog_from_html(source: str, book_url: str, page_html: str) -> SourceCatalog:
    """Build immutable TOC preview from an already fetched public book page."""
    validate_url(book_url, source)
    if not 0 < len(page_html) <= MAX_HTML_CHARS:
        raise ValueError("Source catalog HTML outside safety limits")
    adapter = make_adapter(source, book_url)
    parsed = adapter.parse_catalog(page_html)
    if not parsed:
        raise IngestError("catalog_missing", "No source-owned numbered chapters")
    if len(parsed) > 50_000:
        raise ValueError("Source catalog too large")
    chapters = tuple(SourceChapter(ch.ordinal, ch.title, ch.source_id, ch.url) for ch in parsed)
    if len({c.ordinal for c in chapters}) != len(chapters):
        raise ValueError("Duplicate chapter ordinals")
    if len({c.source_id for c in chapters}) != len(chapters):
        raise ValueError("Duplicate source chapter IDs")
    fingerprint = "\n".join(f"{c.ordinal}\t{c.source_id}\t{c.url}" for c in chapters)
    return SourceCatalog(source, book_url, chapters, hashlib.sha256(fingerprint.encode()).hexdigest())


async def fetch_catalog(source: str, book_url: str, fetch_html: FetchHtml) -> SourceCatalog:
    validate_url(book_url, source)
    return catalog_from_html(source, book_url, await fetch_html(book_url))


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
        except (IngestError, ValueError) as exc:
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
