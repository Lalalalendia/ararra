"""RE-06D: fail-closed accepted-RU export with a pinned SQLite revision manifest.

No Gemini calls or external storage. A complete book requires an explicitly
declared final chapter index: the database cannot infer missing trailing chapters.
The preview and download are separate; download requires the exact preview
manifest SHA256 and rechecks every chapter before returning any bytes.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import Literal

from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, Tag

from .library import LibraryNotFound
from .storage import QueueStore

MAX_EXPORT_CHAPTERS = 2000
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_EXPORT_BYTES = 36 * 1024 * 1024
_ALLOWED = frozenset({
    "html", "body", "p", "div", "span", "br", "strong", "b", "em", "i",
    "u", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "hr",
})
_BLOCK = frozenset({"p", "div", "blockquote", "ul", "ol", "li", "h1", "h2", "h3"})
_SOURCE_MARKER = re.compile(r"\b(?:TSEG_\d{1,12}|TSRC_[0-9a-f]{6,64})\b", re.I)
_SERVICE_CLASS = re.compile(r"(?:^|[-_])(?:qa|review|editor-note|comment|service|internal)(?:$|[-_])", re.I)
_HEAD = re.compile(r"^(?:глава|chapter)\s+(\d+)\s*[.:—-]?\s*$", re.I)


class ExportNotReady(ValueError):
    def __init__(self, preview: dict):
        self.preview = preview
        super().__init__("Book export is incomplete or no longer matches its preview")


class UnsafeAcceptedHtml(ValueError):
    """An accepted chapter contains unsupported or editorial-only markup."""


@dataclass(frozen=True)
class ExportFile:
    data: bytes
    content_type: str
    extension: str
    manifest_sha256: str
    content_sha256: str


def _validated_fragment(raw: str, chapter_index: int) -> tuple[str, str]:
    if _SOURCE_MARKER.search(raw):
        raise UnsafeAcceptedHtml("reserved_service_marker")
    if re.search(r"\b(?:https?://|www\.)", raw, flags=re.I):
        raise UnsafeAcceptedHtml("external_url_requires_editor_review")
    soup = BeautifulSoup(raw, "html.parser")
    for element in soup.descendants:
        if isinstance(element, (Comment, Doctype)):
            raise UnsafeAcceptedHtml("comment_or_declaration")
        if isinstance(element, Tag):
            if element.name not in _ALLOWED:
                raise UnsafeAcceptedHtml("unsupported_tag")
            classes = element.get("class", [])
            if any(_SERVICE_CLASS.search(str(name)) for name in classes):
                raise UnsafeAcceptedHtml("editorial_service_block")
            if any(key.startswith("data-qa") or key.startswith("data-review")
                   for key in element.attrs):
                raise UnsafeAcceptedHtml("editorial_service_attribute")
            # No original attributes (including event handlers, URLs or CSS)
            # may enter a standalone exported HTML document.
            element.attrs.clear()

    root = soup.body if soup.body is not None else soup
    first = root.find(["h1", "h2", "h3", "p"])
    if first is not None:
        match = _HEAD.fullmatch(first.get_text(" ", strip=True))
        if match and int(match.group(1)) == chapter_index:
            first.decompose()

    if not root.get_text(" ", strip=True):
        raise UnsafeAcceptedHtml("empty_after_heading")

    parts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, NavigableString):
            parts.append(str(node))
        elif isinstance(node, Tag):
            if node.name == "br":
                parts.append("\n")
                return
            if node.name in _BLOCK:
                parts.append("\n")
            for child in node.children:
                walk(child)
            if node.name in _BLOCK:
                parts.append("\n")

    for child in root.children:
        walk(child)
    plain = "".join(parts).replace("\xa0", " ")
    plain = re.sub(r"[ \t]+\n", "\n", plain)
    plain = re.sub(r"\n[ \t]+", "\n", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    if not plain:
        raise UnsafeAcceptedHtml("empty_text")
    cleaned_html = "".join(str(node) for node in root.contents).strip()
    return plain, cleaned_html


class VerifiedBookExport:
    """Build a complete, immutable-in-memory snapshot from one SQLite read tx."""

    def __init__(self, store: QueueStore):
        self.store = store

    def _snapshot(self, book_id: str) -> tuple[str, int, list[dict], bool]:
        with self.store._connect() as con:
            con.execute("BEGIN")
            book = con.execute(
                "SELECT title,glossary_revision FROM books WHERE id=?", (book_id,)
            ).fetchone()
            if book is None:
                raise LibraryNotFound("Book not found")
            title = str(book["title"])
            rows: list[dict] = []
            total_bytes = 0
            over_limit = False
            cursor = con.execute("""
                SELECT chapter_id,chapter_index,chapter_path,source_html,
                       source_hash,source_revision,accepted_revision,
                       accepted_source_revision,accepted_html
                FROM book_chapters WHERE book_id=?
                ORDER BY chapter_index,chapter_id
            """, (book_id,))
            for row in cursor:
                candidate = dict(row)
                total_bytes += (len(str(candidate["accepted_html"] or "").encode("utf-8"))
                                + len(str(candidate["source_html"] or "").encode("utf-8")))
                if total_bytes > MAX_SOURCE_BYTES or len(rows) >= MAX_EXPORT_CHAPTERS:
                    over_limit = True
                    break
                rows.append(candidate)
            con.execute("COMMIT")
        return title, int(book["glossary_revision"]), rows, over_limit

    def inspect(self, book_id: str, expected_last_chapter: int) -> tuple[dict, list[tuple[int, str, str]]]:
        if not 1 <= expected_last_chapter <= MAX_EXPORT_CHAPTERS:
            raise ValueError("Declared last chapter must be between 1 and 2000")
        title, glossary_revision, rows, over_limit = self._snapshot(book_id)
        issues: list[dict] = []
        if over_limit:
            issues.append({"code": "export_size_or_count_limit"})
        by_index = {row["chapter_index"]: row for row in rows}
        accepted: list[tuple[int, str, str]] = []
        manifest: list[dict] = []
        for index in range(1, expected_last_chapter + 1):
            row = by_index.get(index)
            if row is None:
                issues.append({"code": "missing_source", "chapter_index": index})
                continue
            raw = row["accepted_html"]
            if not raw or row["accepted_revision"] <= 0:
                issues.append({"code": "not_accepted", "chapter_index": index})
                continue
            if row["accepted_source_revision"] != row["source_revision"]:
                issues.append({"code": "stale_accepted", "chapter_index": index})
                continue
            try:
                plain, safe_html = _validated_fragment(raw, index)
            except UnsafeAcceptedHtml as exc:
                issues.append({"code": "unsafe_accepted_html",
                               "chapter_index": index, "reason": str(exc)})
                continue
            accepted.append((index, plain, safe_html))
            manifest.append({
                "chapter_id": row["chapter_id"], "chapter_index": index,
                "chapter_path": row["chapter_path"],
                "source_revision": row["source_revision"],
                "source_sha256": row["source_hash"],
                "source_blob_sha256": hashlib.sha256(row["source_html"].encode("utf-8")).hexdigest(),
                "accepted_revision": row["accepted_revision"],
                "accepted_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            })
        for row in rows:
            if row["chapter_index"] > expected_last_chapter:
                issues.append({"code": "source_beyond_declared_end",
                               "chapter_index": row["chapter_index"]})
        manifest_bytes = json.dumps(
            {"schema": "readera-accepted-export-v1", "book_id": book_id,
             "title": title, "expected_last_chapter": expected_last_chapter,
             "glossary_revision": glossary_revision, "chapters": manifest},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        preview = {
            "book_id": book_id, "title": title,
            "expected_last_chapter": expected_last_chapter,
            "source_chapter_count": len(rows),
            "ready_chapter_count": len(accepted),
            "ready": not issues,
            "issues": issues[:100],
            "issue_count": len(issues),
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest() if not issues else None,
        }
        return preview, accepted

    def render(self, book_id: str, expected_last_chapter: int,
               expected_manifest_sha256: str, fmt: Literal["txt", "html"]) -> ExportFile:
        preview, chapters = self.inspect(book_id, expected_last_chapter)
        if not preview["ready"] or preview["manifest_sha256"] != expected_manifest_sha256:
            raise ExportNotReady(preview)
        if fmt == "txt":
            body = "\n\n".join(
                f"Глава {index}\n\n{plain}" for index, plain, _ in chapters
            ) + "\n"
            content_type = "text/plain; charset=utf-8"
        elif fmt == "html":
            fragments = [
                '<section data-chapter-index="{index}"><h2>Глава {index}</h2>{content}</section>'.format(
                    index=index, content=safe_html
                )
                for index, _, safe_html in chapters
            ]
            body = (
                "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
                "<title>" + html.escape(preview["title"]) + "</title>"
                "</head><body><main>" + "\n".join(fragments)
                + "</main></body></html>"
            )
            content_type = "text/html; charset=utf-8"
        else:
            raise ValueError("Unsupported export format")
        output = body.encode("utf-8")
        if len(output) > MAX_EXPORT_BYTES:
            raise ExportNotReady({
                **preview, "ready": False, "manifest_sha256": None,
                "issues": [{"code": "export_output_too_large"}], "issue_count": 1,
            })
        return ExportFile(
            data=output, content_type=content_type,
            extension=fmt, manifest_sha256=preview["manifest_sha256"],
            content_sha256=hashlib.sha256(output).hexdigest(),
        )
