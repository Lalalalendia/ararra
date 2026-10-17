"""Bounded discovery of explicit, same-book HTML table-of-contents pages.

A listed source page is evidence of observed chapters, not evidence that
JavaScript-only or unlinked chapters do not exist. Never guess AJAX APIs or
follow query parameters outside one numeric ?page=... key.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup

from .source_catalog_med import IngestError, source_book_id, validate_url

MAX_TOC_PAGES = 12
MAX_TOC_CHAPTERS = 2000
MAX_TOC_PAGE_NUMBER = 999

_DECLARED_ROYAL_ROAD = re.compile(r"^\s*([1-9][0-9,]{0,6})\s+chapters?\s*$",re.I)
_PAGER_SELECTOR = (
    ".pagination a[href], .chapters-pagination a[href], "
    ".chapter-pagination a[href], nav[aria-label='Pagination'] a[href], "
    "nav[aria-label='pagination'] a[href], a[rel='next'][href]"
)
_UNSUPPORTED_LOADER = (
    "#chapters button.load-more, #chapters [data-next-page], "
    ".chapter-list button.load-more, .chapter-list [data-next-page]"
)


def declared_royalroad_count(source: str, html: str) -> int | None:
    """Find a strict '278 Chapters' marker, ignoring arbitrary book prose."""
    if source != "royalroad":
        return None
    soup = BeautifulSoup(html,"html.parser")
    matches: set[int] = set()
    for node in soup.find_all(string=_DECLARED_ROYAL_ROAD):
        value = _DECLARED_ROYAL_ROAD.fullmatch(str(node).strip())
        if value:
            count=int(value.group(1).replace(",",""))
            if count > 0:
                matches.add(count)
    if len(matches)>1:
        raise IngestError("catalog_conflict","Inconsistent advertised chapter counts")
    return next(iter(matches)) if matches else None


def page_number_for_link(source: str, book_url: str, url: str) -> int | None:
    """Allow only an exact book-path page link on the same HTTPS host."""
    parts = urlsplit(url)
    root = urlsplit(validate_url(book_url,source))
    try:
        validate_url(url,source)
        if source_book_id(url,source)!=source_book_id(book_url,source):
            return None
    except ValueError:
        return None
    if (parts.hostname != root.hostname or
        parts.path.rstrip("/") != root.path.rstrip("/") or
        parts.fragment or parts.username or parts.password or
        parts.port not in (None,443)):
        return None
    query = parse_qsl(parts.query, keep_blank_values=True)
    if len(query)!=1 or query[0][0]!="page":
        return None
    raw=query[0][1]
    if not re.fullmatch(r"[1-9][0-9]{0,2}",raw):
        return None
    number=int(raw)
    return number if number <= MAX_TOC_PAGE_NUMBER else None


def pagination_links(source: str, book_url: str, html: str) -> dict[int,str]:
    """Follow only explicit TOC paging hints; reject ambiguous page markers."""
    soup=BeautifulSoup(html,"html.parser")
    if soup.select_one(_UNSUPPORTED_LOADER):
        raise IngestError("catalog_incomplete","Unresolved dynamic chapter-list pagination")
    found:dict[int,str]={}
    for link in soup.select(_PAGER_SELECTOR):
        href=link.get("href")
        if not href:
            continue
        # Anchors and disabled controls are not evidence of a next TOC page.
        if href.strip().startswith("#") or link.has_attr("disabled"):
            continue
        url=urljoin(book_url,href)
        page=page_number_for_link(source,book_url,url)
        if page is None:
            # Some general site pagers are unrelated. Restrict failure to
            # explicit TOC-like numeric or rel=next navigation.
            if ((link.get("rel") and "next" in link.get("rel")) or
                re.fullmatch(r"\s*\d+\s*",link.get_text(" ",strip=True))):
                raise IngestError("catalog_incomplete",
                                  "Unsupported or cross-book TOC pagination URL")
            continue
        previous=found.get(page)
        if previous is not None and previous != url:
            raise IngestError("catalog_conflict","Conflicting TOC page addresses")
        found[page]=url
    if len(found)>MAX_TOC_PAGES:
        raise IngestError("catalog_incomplete","More than 12 TOC pages advertised")
    return found
