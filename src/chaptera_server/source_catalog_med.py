"""Read-only site-specific HTML parsers, adapted from the user's med repository.

Source: https://github.com/Lalalalendia/med/blob/5046ff53207ce2fc89fb83277d9ceec1fee30935/tools/novel-ingest/source_ingest.py
Only catalog/body parsing is ported; CLI, disk writes and network fetching are
intentionally replaced by ReadEra's authenticated and bounded site collector.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .source_site_profiles import SITE_PROFILES
from .source_catalog_semantics import classify_toc_title

# One authoritative, explicitly reviewed host allowlist per site profile.
HOSTS = {name: profile.hosts for name, profile in SITE_PROFILES.items()}
USER_AGENT = "NovelSourceIngest/1.0 (public chapter pages; no authentication)"
NUMBERED_TITLE = re.compile(r"^\s*(?:chapter\s*)?(\d{1,5})\s*(?:[-:.\u2013\u2014]\s*|\s+)(\S.*?)\s*$", re.I)
BLOCK_PATTERNS = (
    "just a moment", "verify you are human", "checking your browser",
    "captcha", "access denied", "enable javascript and cookies",
    "chapter is locked", "unlock this chapter", "purchase this chapter",
    "login to read", "log in to read",
    "正在验证浏览器", "正在驗證瀏覽器",
)


class IngestError(Exception):
    def __init__(self, status: str, message: str):
        self.status = status
        super().__init__(message)


def validate_url(url: str, source: str) -> str:
    """Restrict all network requests, including discovered links, to trusted HTTPS hosts."""
    if source not in HOSTS:
        raise ValueError(f"Unsupported source: {source}")
    p = urlsplit(url)
    if (p.scheme != "https" or p.hostname not in HOSTS[source]
            or p.username or p.password or p.port not in (None, 443)
            or not p.path.startswith("/")):
        raise ValueError(f"Unsafe or unsupported {source} URL: {url}")
    # Newly reviewed profiles admit exactly URL identities, not URL-driven
    # filtering, age-consent/query toggles or arbitrary fragments.
    if source in ("wikisource_en", "wikisource_ru", "ao3") and (p.query or p.fragment):
        raise ValueError(f"Unsupported {source} URL parameters")
    return url


def source_book_id(url: str, source: str) -> str:
    p = urlsplit(validate_url(url, source))
    if source == "royalroad":
        match = re.match(r"^/fiction/(\d+)(?:/|$)", p.path)
    elif source == "uukanshu":
        match = re.match(r"^/book/(\d+)(?:/|$)", p.path)
    elif source == "ao3":
        match = re.match(r"^/works/([1-9]\d*)(?:/|$)", p.path)
    elif source in ("wikisource_en", "wikisource_ru"):
        decoded = unquote(p.path)
        # Subpages share the first page-name segment; full book-root
        # ownership is additionally checked by WikisourceAdapter below.
        match = re.match(r"^/wiki/([^/:?#]+)(?:/|$)", decoded)
        if match and (match.group(1) in (".", "..") or len(match.group(1)) > 512):
            match = None
    else:
        match = re.match(r"^/book/(?:[^/]*_)?(\d{6,})(?:/|$)", p.path)
    if not match:
        raise ValueError(f"Not a {source} book URL: {url}")
    return match.group(1)


def chapter_identity(url: str, source: str, expected_book_id: str) -> str | None:
    p = urlsplit(validate_url(url, source))
    if source == "royalroad":
        m = re.match(r"^/fiction/(\d+)/(?:[^/]+/)?chapter/(\d+)(?:/|$)", p.path)
    elif source == "uukanshu":
        m = re.match(r"^/book/(\d+)/(\d+)\.html$", p.path)
    elif source == "ao3":
        m = re.match(r"^/works/([1-9]\d*)/chapters/([1-9]\d*)/?$", p.path)
    elif source in ("wikisource_en", "wikisource_ru"):
        decoded = unquote(p.path)
        m = re.match(r"^/wiki/([^/:?#]+)/([^?#]+)$", decoded)
        if m and any(piece in (".", "..", "") for piece in m.group(2).split("/")):
            m = None
    else:
        m = re.match(r"^/book/(?:[^/]*_)?(\d{6,})/[^/]+_(\d{8,})(?:/|$)", p.path)
    if not m or m.group(1) != expected_book_id:
        return None
    return m.group(2)


def normalize_body(value: str) -> str:
    lines = [re.sub(r"[\t \u00a0]+", " ", x).strip()
             for x in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    result: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if result and not blank:
                result.append("")
            blank = True
        else:
            result.append(line)
            blank = False
    return "\n".join(result).strip()


@dataclass(frozen=True)
class Chapter:
    ordinal: int
    title: str
    url: str
    source_id: str
    entry_type: str = "unknown"
    author_number: int | None = None
    classification_source: str = "none"
    classification_confidence: str = "unverified"


class PublicHtmlAdapter:
    catalog_selectors: tuple[str, ...] = ()
    content_selectors: tuple[str, ...] = ()

    def __init__(self, source: str, book_url: str):
        self.source = source
        self.book_url = validate_url(book_url, source)
        self.book_id = source_book_id(book_url, source)

    def parse_catalog(self, html: str) -> list[Chapter]:
        soup = BeautifulSoup(html, "html.parser")
        candidates = []
        for selector in self.catalog_selectors:
            candidates.extend(soup.select(selector))
        if not candidates:
            # Fallback is still constrained to same-book, numbered chapter URLs.
            candidates = soup.select("a[href]")
        found: dict[int, Chapter] = {}
        for link in candidates:
            href = link.get("href")
            if not href:
                continue
            url = urljoin(self.book_url, href)
            try:
                chapter_id = chapter_identity(url, self.source, self.book_id)
            except ValueError:
                continue
            if not chapter_id:
                continue
            label = re.sub(r"\s+", " ", link.get_text(" ", strip=True))
            m = NUMBERED_TITLE.match(label)
            if not m:
                # Auxiliary/glossary and unnumbered entries are not story chapters.
                continue
            number = int(m.group(1))
            title = m.group(2).strip()
            if number < 1 or not title:
                continue
            # Contents tables can expose numeric comment/view links.
            # Counts must never be interpreted as chapter ordinals.
            if re.fullmatch(r"\d+\s+(?:comments?|views?|reviews?|likes?|replies)", label, re.I):
                continue
            if self.source == "royalroad":
                parts = urlsplit(url).path.split("/chapter/", 1)[-1].split("/")
                slug = parts[1] if len(parts) > 1 else ""
                slug_number = re.match(r"^(\d{1,5})(?:[-_]|$)", slug)
                if slug_number and int(slug_number.group(1)) != number:
                    continue
            candidate = Chapter(
                number, title, url, chapter_id, "chapter", number,
                "title_pattern", "medium",
            )
            prior = found.get(number)
            if prior and prior.source_id != chapter_id:
                raise IngestError(
                    "catalog_conflict",
                    f"Chapter {number} conflict: {prior.title!r} {prior.url} vs {title!r} {url}",
                )
            # Prefer a descriptive title when the same chapter has duplicate anchors.
            if prior is None or len(candidate.title) > len(prior.title):
                found[number] = candidate
        return [found[n] for n in sorted(found)]

    def parse_body(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for selector in self.content_selectors:
            node = soup.select_one(selector)
            if not node:
                continue
            # Deliberately no body fallback: a 200 login/error page is not a chapter.
            for junk in node.select(
                "script,style,noscript,iframe,svg,nav,header,footer,form,button,"
                ".author-note,.author-note-portlet,.chapter-nav,.advertisement,"
                ".comments,.chapter-comments,.spoiler-toggle"
            ):
                junk.decompose()
            return normalize_body(node.get_text("\n", strip=True))
        low = soup.get_text(" ", strip=True).lower()
        if any(marker in low for marker in BLOCK_PATTERNS):
            raise IngestError("blocked", "Login, anti-bot, or locked content")
        raise IngestError("missing_body", "No recognised chapter content container")


class RoyalRoadAdapter(PublicHtmlAdapter):
    catalog_selectors = ("#chapters a[href]", ".chapter-row a[href]")
    content_selectors = (".chapter-content", ".chapter-inner.chapter-content")

    def parse_catalog(self, html: str) -> list[Chapter]:
        """Follow the *published TOC order*, not the author's title numbering.

        Royal Road permits prologues, interludes, Roman numerals and author
        rearrangements. Requiring "Chapter <arabic>" silently drops valid
        chapters. The observed DOM's official TOC order is the only ordering
        input; it is exposed as a sequence index, not a claimed author number.
        """
        soup = BeautifulSoup(html, "html.parser")
        anchors = soup.select("#chapters a[href], .chapter-row a[href]")
        if not anchors:
            raise IngestError("catalog_missing", "Royal Road table of contents not found")
        collected: dict[str, Chapter] = {}
        for link in anchors:
            href = link.get("href")
            if not href:
                continue
            url = urljoin(self.book_url, href)
            try:
                chapter_id = chapter_identity(url, self.source, self.book_id)
            except ValueError:
                continue
            if chapter_id is None:
                continue
            # No tracking query or fragment is part of a chapter identity.
            if urlsplit(url).query or urlsplit(url).fragment:
                continue
            title = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
            if not title:
                continue
            old = collected.get(chapter_id)
            if old is not None:
                if old.url != url:
                    raise IngestError("catalog_conflict", "Same chapter ID has different URLs in TOC")
                continue
            kind, author, method, confidence = classify_toc_title(title)
            collected[chapter_id] = Chapter(
                len(collected) + 1, title, url, chapter_id,
                kind, author, method, confidence,
            )
        if not collected:
            raise IngestError("catalog_missing", "No recognized Royal Road chapter links")
        return list(collected.values())


class WebNovelAdapter(PublicHtmlAdapter):
    catalog_selectors = (
        ".volume-list a[href]", ".chapter-list a[href]", ".cha-list a[href]",
        ".chapter-item a[href]", ".volume a[href]",
    )
    content_selectors = ("#chapterContent", ".cha-content", ".chapter-content")


CHINESE_CHAPTER_TITLE = re.compile(
    r"^\s*第\s*([零〇一二三四五六七八九十百千\d]+)\s*章\s*(.*?)\s*$"
)
CHINESE_DIGITS = {ch: i for i, ch in enumerate("零一二三四五六七八九")}
CHINESE_DIGITS["〇"] = 0
CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000}


def parse_chinese_chapter_number(value: str) -> int | None:
    if value.isdecimal():
        return int(value)
    total = 0
    digit = 0
    for char in value:
        if char in CHINESE_DIGITS:
            digit = CHINESE_DIGITS[char]
        elif char in CHINESE_UNITS:
            total += (digit or 1) * CHINESE_UNITS[char]
            digit = 0
        else:
            return None
    return total + digit



class UUKanshuAdapter(PublicHtmlAdapter):
    """Catalog identity derives from Chinese chapter labels, not link position."""
    content_selectors = ("div.content", "#content", ".content")

    def parse_catalog(self, html: str) -> list[Chapter]:
        soup = BeautifulSoup(html, "html.parser")
        found: dict[int, Chapter] = {}
        for link in soup.select("a[href]"):
            url = urljoin(self.book_url, link["href"])
            try:
                source_id = chapter_identity(url, self.source, self.book_id)
            except ValueError:
                continue
            if not source_id:
                continue
            label = re.sub(r"\s+", " ", link.get_text(" ", strip=True))
            match = CHINESE_CHAPTER_TITLE.match(label)
            if not match:
                continue
            ordinal = parse_chinese_chapter_number(match.group(1))
            if ordinal is None or ordinal < 1:
                continue
            title = match.group(2).strip() or f"第{ordinal}章"
            item = Chapter(
                ordinal, title, url, source_id, "chapter", ordinal,
                "structured", "high",
            )
            prior = found.get(ordinal)
            if prior and prior.source_id != source_id:
                raise IngestError(
                    "catalog_conflict",
                    f"Chapter {ordinal} has conflicting source IDs {prior.source_id}/{source_id}",
                )
            if prior is None or len(item.title) > len(prior.title):
                found[ordinal] = item
        return [found[n] for n in sorted(found)]

    def parse_body(self, html: str) -> str:
        if any(marker in html[:12000] for marker in ("正在验证浏览器", "正在驗證瀏覽器")):
            raise IngestError("blocked", "Chinese browser verification challenge")
        body = super().parse_body(html)
        cjk_chars = len(re.findall(r"[\u3400-\u9fff]", body))
        if cjk_chars < 180:
            raise IngestError("too_short", f"Only {cjk_chars} Chinese characters")
        return body



class WikisourceAdapter(PublicHtmlAdapter):
    """Explicit same-work subpages only; full text is never guessed from an index.

    Two separately allowlisted language subdomains share this parser. The
    authorial numbering is independent of the observed contents order.
    """

    def parse_catalog(self, html: str) -> list[Chapter]:
        soup = BeautifulSoup(html, "html.parser")
        content = soup.select_one("#mw-content-text .mw-parser-output, .mw-parser-output")
        if content is None:
            raise IngestError("catalog_missing", "Wikisource article body not found")
        root_path = unquote(urlsplit(self.book_url).path).rstrip("/") + "/"
        found: dict[str, Chapter] = {}
        # Only links published inside book contents lists or tables; never
        # scan whole-page footer/sidebar/navigation links.
        for link in content.select("ol a[href], ul a[href], table a[href]"):
            target = urljoin(self.book_url, link["href"])
            try:
                identity = chapter_identity(target, self.source, self.book_id)
            except ValueError:
                continue
            if not identity or not unquote(urlsplit(target).path).startswith(root_path):
                continue
            label = re.sub(r"\s+", " ", link.get_text(" ", strip=True))
            if not label:
                continue
            previous = found.get(identity)
            if previous is not None:
                if previous.url != target:
                    raise IngestError("catalog_conflict", "Wikisource chapter URL changed")
                continue
            kind, author, method, confidence = classify_toc_title(label)
            found[identity] = Chapter(
                len(found) + 1, label, target, identity,
                kind, author, method, confidence,
            )
        return list(found.values())

    def parse_body(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one("#mw-content-text .mw-parser-output, .mw-parser-output")
        if node is None:
            raise IngestError("missing_body", "Wikisource chapter article not found")
        for junk in node.select(
            "script,style,noscript,iframe,form,nav,header,footer,button,"
            ".mw-editsection,.toc,.metadata,.navbox,.printfooter,"
            ".mw-references-wrap,.reflist,.ws-noexport,.catlinks,"
            ".noprint,.mw-cite-backlink,sup.reference,table"
        ):
            junk.decompose()
        text = normalize_body(node.get_text("\n", strip=True))
        if not text:
            raise IngestError("missing_body", "Wikisource chapter has no visible prose")
        return text


class AO3Adapter(PublicHtmlAdapter):
    """Public, non-login AO3 works with HTML chapter-index links.

    No age-gate, account, cookie, redirect, restricted work or paywall bypass.
    A separate work-owned /navigate index is requested only if explicitly
    linked from the public work page.
    """

    def parse_catalog(self, html: str) -> list[Chapter]:
        soup = BeautifulSoup(html, "html.parser")
        links = soup.select(
            "#chapter_index a[href], .chapter.index a[href],"
            " ol.chapter.index.group a[href]"
        )
        found: dict[str, Chapter] = {}
        for link in links:
            target = urljoin(self.book_url, link["href"])
            try:
                identity = chapter_identity(target, self.source, self.book_id)
            except ValueError:
                continue
            if not identity:
                continue
            title = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
            if not title:
                continue
            previous = found.get(identity)
            if previous is not None:
                if previous.url != target:
                    raise IngestError("catalog_conflict", "AO3 chapter ID URL conflict")
                continue
            kind, author, method, confidence = classify_toc_title(title)
            found[identity] = Chapter(
                len(found) + 1, title, target, identity,
                kind, author, method, confidence,
            )
        return list(found.values())

    def parse_body(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one(
            "#chapters .userstuff.module[role='article'],"
            " #chapters div.userstuff[role='article'],"
            " #chapters .chapter .userstuff.module"
        )
        if node is None:
            low = soup.get_text(" ", strip=True).lower()
            if any(marker in low for marker in BLOCK_PATTERNS) or (
                "only available to registered users" in low
            ):
                raise IngestError("blocked", "AO3 access or permission required")
            raise IngestError("missing_body", "No public AO3 chapter text")
        for junk in node.select("script,style,noscript,iframe,form,nav,button"):
            junk.decompose()
        return normalize_body(node.get_text("\n", strip=True))


def make_adapter(source: str, book_url: str) -> PublicHtmlAdapter:
    classes = {"royalroad": RoyalRoadAdapter, "webnovel": WebNovelAdapter,
               "uukanshu": UUKanshuAdapter, "ao3": AO3Adapter,
               "wikisource_en": WikisourceAdapter, "wikisource_ru": WikisourceAdapter}
    if source not in classes:
        raise ValueError(f"Unknown source: {source}")
    return classes[source](source, book_url)


