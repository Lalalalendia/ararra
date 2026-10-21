"""Bounded source-chapter assembly from observed, source-owned part links.

No transport or database here. Keep the entire chapter provisional until
every observed part passes the same parser/identity/size checks.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from .source_catalog_med import (
    IngestError, make_adapter, normalize_body, source_book_id, validate_url,
)

MAX_SOURCE_PARTS = 12
MAX_PART_HTML_CHARS = 2_000_000
MAX_ASSEMBLED_BODY_CHARS = 1_000_000


async def fetch_complete_chapter(
    source: str,
    book_url: str,
    chapter_url: str,
    fetch_html: Callable[[str], Awaitable[str]],
) -> str:
    adapter = make_adapter(source, book_url)
    validate_url(chapter_url, source)
    if source_book_id(chapter_url, source) != adapter.book_id:
        raise IngestError("catalog_conflict", "Chapter URL belongs to another book")
    current = chapter_url
    visited: set[str] = set()
    parts: list[str] = []
    for _ in range(MAX_SOURCE_PARTS):
        if current in visited:
            raise IngestError("catalog_conflict", "Repeated source chapter part URL")
        visited.add(current)
        page = await fetch_html(current)
        if not 0 < len(page) <= MAX_PART_HTML_CHARS:
            raise IngestError("catalog_incomplete", "Source chapter part HTML outside limits")
        parts.append(adapter.parse_body(page))
        body = normalize_body("\n\n".join(parts))
        if len(body) > MAX_ASSEMBLED_BODY_CHARS:
            raise IngestError("too_large", "Source chapter exceeds assembled text limit")
        next_part = getattr(adapter, "next_part_url", None)
        if not callable(next_part):
            return body
        target = next_part(page, current)
        if target is None:
            return body
        validate_url(target, source)
        if source_book_id(target, source) != adapter.book_id:
            raise IngestError("catalog_conflict", "Next source part belongs to another book")
        current = target
    raise IngestError(
        "catalog_incomplete",
        f"Chapter advertises more than {MAX_SOURCE_PARTS} linked parts",
    )
