#!/usr/bin/env python3
"""Controlled metadata-only Timotxt acceptance probe.

Operator-driven ONLY, no chapter-body GET, account/session access, Gemini,
storage import, cookies, subprocess or worker. Never report chapter prose.
The normal ReadEra transport checks HTTPS/TLS, DNS/IP, redirects and robots.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter

from chaptera_server.source_catalog_med import IngestError
from chaptera_server.source_collect import SourceCatalog, fetch_catalog
from chaptera_server.source_site_http import PublicHtmlFetcher
from chaptera_server.source_request_limiter import SourceSiteBusy

# The user's real Atarax catalog, not arbitrary site wide crawling.
KNOWN_BOOK_IDS = frozenset({
    "2312587182", "2511556748", "0404595507",
    "0106158592", "2404580687", "1302454676",
    "2007523815", "1205152975", "2010493463",
})


def metadata_receipt(catalog: SourceCatalog) -> dict[str, object]:
    """No body text or chapter titles in a receipt, even on CI/terminal."""
    source_ids = [chapter.source_id for chapter in catalog.chapters]
    types = Counter(ch.entry_type for ch in catalog.chapters)
    return {
        "source": catalog.source,
        "book_id": catalog.book_url.rstrip("/").rsplit("/", 1)[-1],
        "observed_chapter_count": len(source_ids),
        "unique_chapter_ids": len(set(source_ids)),
        "declared_chapter_count": catalog.declared_chapter_count,
        "coverage_state": catalog.coverage_state,
        "toc_page_count": len(catalog.page_urls),
        "catalog_sha256": catalog.catalog_sha256,
        "entry_type_counts": dict(sorted(types.items())),
        "complete_directory_identity": (
            catalog.declared_chapter_count is not None
            and len(set(source_ids)) == len(source_ids)
            and catalog.declared_chapter_count == len(source_ids)
        ),
        "prose_logged": False,
        "network_scope": "robots + book landing + linked full directory",
        "chapter_body_requests": 0,
        "production_acceptance": "pending authorized body, VPS backup and quotas",
    }


async def inspect(book_id: str) -> dict[str, object]:
    if not re.fullmatch(r"[0-9]{10}", book_id) or book_id not in KNOWN_BOOK_IDS:
        raise ValueError("Book ID must be one of the nine reviewed Atarax Timotxt IDs")
    url = f"https://www.timotxt.com/{book_id}/"
    fetcher = PublicHtmlFetcher()
    # The site-specific standalone limiter enforces >=6s as well as robots.
    catalog = await fetch_catalog(
        "timotxt", url, lambda target: fetcher.fetch("timotxt", target),
    )
    return metadata_receipt(catalog)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-id", default="2010493463")
    parser.add_argument(
        "--approved-metadata-check", action="store_true",
        help="Operator verified that this exact metadata-only access is permitted",
    )
    args = parser.parse_args()
    if not args.approved_metadata_check:
        parser.error("Metadata access must be explicitly approved before network use")
    try:
        receipt = asyncio.run(inspect(args.book_id))
    except (IngestError, SourceSiteBusy, ValueError) as exc:
        # Error strings contain only our own transport/parser diagnostics, not
        # page HTML or original text. Never expose request tokens or cookies.
        print(json.dumps({
            "source": "timotxt",
            "status": "blocked_or_incomplete",
            "error_type": type(exc).__name__,
            "code": getattr(exc, "status", "invalid_book"),
            "chapter_body_requests": 0,
            "production_acceptance": "not_passed",
        }, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
