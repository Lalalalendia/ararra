"""Timotxt novel adapter: observed whole-book TOC and bounded chapter parts.

Works only on explicit same-book HTML. Network, robots, DNS pinning, pacing,
database checkpoints and permission enforcement are owned by ReadEra.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .source_catalog_med import (
    Chapter, IngestError, PublicHtmlAdapter, chapter_identity,
    normalize_body, parse_chinese_chapter_number, validate_url,
)
from .source_catalog_semantics import classify_toc_title

_BOOK_ROOT = re.compile(r"/[0-9]{10}/?")
_CHAPTER_PAGE = re.compile(r"/([0-9]{10})/([1-9][0-9]{0,8})(?:_([2-9]|1[0-2]))?\.html")
_FULL_TOC = re.compile(r"全部章[節节]|完整章[節节]|所有章[節节]")
_AUTHOR_NUMBER = re.compile(r"^\s*第\s*([零〇一二三四五六七八九十百千0-9]+)\s*[章節节回]")
_OBFUSCATED = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")
_PUA = re.compile(r"[\ue000-\uf8ff]")
_TAIL = (
    "請記住本書首發域名：", "请记住本书首发域名：",
    "章節錯誤", "章节错误",
)
_NEXT_PART_WORDS = ("下一頁", "下一页", "下頁", "下页")
_BLOCKED = (
    "too many requests", "verify you are human", "checking your browser",
    "captcha", "access denied", "需要登錄", "请先登录", "訪問過於頻繁",
    "访问过于频繁", "請輸入驗證碼", "请输入验证码",
)


def timotxt_declared_count(html: str) -> int | None:
    """Read the directory's own chapter count, not the 'latest chapters' list."""
    soup = BeautifulSoup(html, "html.parser")
    header = soup.find(["h1", "h2"])
    if header is None or "章節列表" not in header.get_text(" ", strip=True):
        return None
    # Limit inspection to the header/metadata vicinity, not chapter prose.
    nearby = soup.get_text(" ", strip=True)[:1200]
    match = re.search(r"(?:章節|章节)\s*[:：]\s*([1-9][0-9]{0,4})(?![0-9])", nearby)
    return int(match.group(1)) if match else None


def observed_directory_url(book_url: str, html: str) -> str:
    """The directory must be linked by the public book page; never guess it."""
    root = urlsplit(validate_url(book_url, "timotxt"))
    book_id = root.path.strip("/")
    expected_path = f"/{book_id}/dir"
    soup = BeautifulSoup(html, "html.parser")
    valid: set[str] = set()
    for link in soup.select("a[href]"):
        url = urljoin(book_url, link["href"])
        p = urlsplit(url)
        if (p.scheme == "https" and p.hostname == root.hostname
                and p.path == expected_path and not p.query and not p.fragment
                and not p.username and not p.password and p.port in (None, 443)):
            valid.add(url)
    if len(valid) != 1:
        raise IngestError("catalog_missing", "Timotxt full book directory link missing or ambiguous")
    return valid.pop()


class TimotxtAdapter(PublicHtmlAdapter):
    """One book, one observed directory, stable HTML chapter IDs."""

    def __init__(self, source: str, book_url: str):
        super().__init__(source, book_url)
        if (source != "timotxt" or not _BOOK_ROOT.fullmatch(urlsplit(book_url).path)
                or urlsplit(book_url).query or urlsplit(book_url).fragment):
            raise ValueError("Timotxt import requires the book root /<10-digit-id>/")

    def parse_catalog(self, html: str) -> list[Chapter]:
        soup = BeautifulSoup(html, "html.parser")
        marker = soup.find(string=_FULL_TOC)
        if marker is None:
            raise IngestError("catalog_missing", "Timotxt full directory section missing")
        # The directory contains 'latest chapters' in reverse order and a
        # separate full list in ascending order. Only lists AFTER the full
        # directory marker are eligible; the largest same-book list wins.
        lists = marker.parent.find_all_next(["ul", "ol"])
        best: list[tuple[str, str, str]] = []
        for group in lists:
            observed: dict[str, tuple[str, str, str]] = {}
            for link in group.select("a[href]"):
                url = urljoin(self.book_url, link["href"])
                if urlsplit(url).hostname != urlsplit(self.book_url).hostname:
                    continue
                try:
                    source_id = chapter_identity(url, "timotxt", self.book_id)
                except ValueError:
                    continue
                if source_id is None:
                    continue
                title = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
                if not title or urlsplit(url).query or urlsplit(url).fragment:
                    continue
                old = observed.get(source_id)
                if old is not None and old[1] != url:
                    raise IngestError("catalog_conflict", "Timotxt chapter ID has conflicting URLs")
                if old is None:
                    observed[source_id] = (source_id, url, title)
            if len(observed) >= len(best):
                best = list(observed.values())
        if not best:
            raise IngestError("catalog_missing", "Timotxt full directory has no valid chapter links")

        chapters: list[Chapter] = []
        for source_id, url, title in best:
            kind, author, method, confidence = classify_toc_title(title)
            match = _AUTHOR_NUMBER.match(title)
            if match is not None:
                number = parse_chinese_chapter_number(match.group(1))
                if number is not None and number > 0:
                    kind, author, method, confidence = (
                        "chapter", number, "title_pattern", "medium"
                    )
            elif re.fullmatch(
                r"(?:完本感言|上架感言|請假條|请假条|停更通知|更新通知)[～~!！?？。. ]*",
                title,
            ):
                kind, author, method, confidence = (
                    "service", None, "title_pattern", "medium"
                )
            elif title.lstrip().startswith(("番外", "番外篇", "外傳", "外传")):
                kind, author, method, confidence = "extra", None, "title_pattern", "medium"
            chapters.append(Chapter(
                len(chapters) + 1, title, url, source_id,
                kind, author, method, confidence,
            ))
        return chapters

    def parse_body(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one(".content")
        if node is None:
            text = soup.get_text(" ", strip=True).lower()
            if any(word in text for word in _BLOCKED):
                raise IngestError("blocked", "Timotxt returned a challenge or rate limit")
            raise IngestError("missing_body", "Timotxt chapter content container not found")
        if node.select_one(".chaptererror, .chapter-error"):
            raise IngestError("needs_review", "Timotxt reported an invalid chapter body")
        for junk in node.select(
            "script,style,noscript,iframe,svg,nav,form,button,"
            ".foot-nav,.chapter-nav,.advertisement,.ad,.read_tj"
        ):
            junk.decompose()
        lines = node.get_text("\n", strip=True).splitlines()
        lines = [line for line in lines if not line.strip().startswith(_TAIL)]
        body = normalize_body("\n".join(lines))
        if not body:
            raise IngestError("missing_body", "Timotxt returned empty chapter text")
        if any(word in body.lower() for word in _BLOCKED):
            raise IngestError("blocked", "Timotxt chapter is a rate-limit or access page")
        # Timotxt has historically substituted Korean glyphs/PUA for Chinese
        # characters. Without an evidence-backed map do not fake a clean RAW.
        if _OBFUSCATED.search(body) or _PUA.search(body):
            raise IngestError("needs_review", "Timotxt RAW contains unresolved obfuscated glyphs")
        return body

    def next_part_url(self, html: str, current_url: str) -> str | None:
        """Follow only an actually linked, contiguous next page of the SAME chapter."""
        validate_url(current_url, "timotxt")
        current = urlsplit(current_url)
        match = _CHAPTER_PAGE.fullmatch(current.path)
        if match is None or match.group(1) != self.book_id:
            raise IngestError("catalog_conflict", "Not a Timotxt owned chapter page")
        chapter_id = match.group(2)
        part = int(match.group(3) or "1")
        soup = BeautifulSoup(html, "html.parser")
        next_urls: set[str] = set()
        next_label_seen = False
        for link in soup.select(".foot-nav a[href], .chapter-nav a[href], .pagination a[href]"):
            label = link.get_text(" ", strip=True)
            target = urljoin(current_url, link["href"])
            try:
                validate_url(target, "timotxt")
            except ValueError:
                if any(word in label for word in _NEXT_PART_WORDS):
                    raise IngestError("catalog_incomplete", "Unsafe Timotxt next-page link")
                continue
            p = urlsplit(target)
            if p.hostname != current.hostname:
                continue
            candidate = _CHAPTER_PAGE.fullmatch(p.path)
            if (candidate is not None and candidate.group(1) == self.book_id
                    and candidate.group(2) == chapter_id):
                to_part = int(candidate.group(3) or "1")
                if to_part > part + 1:
                    raise IngestError("catalog_incomplete", "Timotxt chapter part skipped")
                if to_part == part + 1:
                    next_urls.add(target)
            if any(word in label for word in _NEXT_PART_WORDS):
                next_label_seen = True
        if len(next_urls) > 1:
            raise IngestError("catalog_conflict", "Ambiguous Timotxt continuation page")
        if next_urls:
            if part >= 12:
                raise IngestError("catalog_incomplete", "Timotxt chapter exceeds 12 parts")
            return next_urls.pop()
        if next_label_seen:
            raise IngestError("catalog_incomplete", "Timotxt continuation advertised but not linked")
        return None
