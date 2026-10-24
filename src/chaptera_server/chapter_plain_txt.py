"""Fail-closed HTML fragment -> readable plain TXT for new user-consented Drive exports.

Unlike legacy RE-09A raw-fragment handoffs, these are intended to be edited as
ordinary text files. Unknown elements are rejected rather than dropping content.
There is no publication/accepted-text transition and no external I/O.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, Tag


class PlainTxtError(ValueError):
    """A source/machine result cannot be exported losslessly as readable TXT."""


_ALLOWED = frozenset({
    "html", "body", "p", "div", "span", "br", "strong", "b", "em",
    "i", "u", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "hr",
})
_BLOCKS = frozenset({"p", "div", "blockquote", "ul", "ol", "li", "h1", "h2", "h3"})
_SERVICE = re.compile(r"\b(?:TSEG_\d{1,12}|TSRC_[a-f0-9]{6,64})\b", re.I)
_SERVICE_CLASS = re.compile(
    r"(?:^|[-_])(?:qa|review|editor-note|comment|service|internal)(?:$|[-_])", re.I
)


def readable_txt(fragment: str) -> bytes:
    """Preserve visible source text and paragraph boundaries, never trust markup.

    This function does NOT try to repair missing segments, accept a translation,
    or hide nontext blocks. If any element is unsupported, refuse export.
    """
    if not isinstance(fragment, str) or not fragment.strip():
        raise PlainTxtError("Empty source or translation cannot be exported as TXT")
    if _SERVICE.search(fragment):
        raise PlainTxtError("Unresolved internal marker in chapter content")
    soup = BeautifulSoup(fragment, "html.parser")
    for element in soup.descendants:
        if isinstance(element, (Comment, Doctype)):
            raise PlainTxtError("HTML comments and declarations require review")
        if isinstance(element, Tag):
            if element.name not in _ALLOWED:
                raise PlainTxtError("Unsupported HTML element in chapter content")
            classes = element.get("class", [])
            if any(_SERVICE_CLASS.search(str(value)) for value in classes):
                raise PlainTxtError("Editorial/service markup requires review")
            if element.has_attr("hidden") or element.get("aria-hidden") == "true":
                raise PlainTxtError("Hidden content requires review")
            if any(k.startswith("data-qa") or k.startswith("data-review")
                   for k in element.attrs):
                raise PlainTxtError("Editorial/review annotations require review")
    parts: list[str] = []

    def append(node) -> None:
        if isinstance(node, NavigableString):
            parts.append(str(node))
        elif isinstance(node, Tag):
            if node.name == "br":
                parts.append("\n")
                return
            if node.name == "hr":
                parts.append("\n\n---\n\n")
                return
            if node.name in _BLOCKS:
                parts.append("\n\n" if node.name not in ("li",) else "\n")
            for child in node.children:
                append(child)
            if node.name in _BLOCKS:
                parts.append("\n\n" if node.name not in ("li",) else "\n")

    root = soup.body if soup.body is not None else soup
    for child in root.children:
        append(child)
    plain = "".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    plain = plain.replace("\xa0", " ")
    plain = re.sub(r"[ \t]+\n", "\n", plain)
    plain = re.sub(r"\n[ \t]+", "\n", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    if not plain:
        raise PlainTxtError("No readable text in HTML fragment")
    return plain.encode("utf-8")
