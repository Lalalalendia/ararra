from __future__ import annotations
import html
import re
from collections import Counter
from dataclasses import dataclass
from bs4 import BeautifulSoup, Comment, NavigableString
# These patterns intentionally mirror the battle-tested Builder/QC leftovers
# detector, are part of the reusable Chaptera core, which has no UI dependencies.
_RE_HTML_TAG = re.compile(r"(?is)<\s*/?\s*[a-z][a-z0-9:_-]*[^>]*>")
_RE_HTML_TAG_PARTS = re.compile(r"(?is)^<\s*(/?)\s*([a-z][a-z0-9:_-]*)[^>]*>$")
_RE_ENTITY = re.compile(r"(?is)&(?:nbsp|amp|lt|gt|quot|apos|#\d+|#[xX][0-9a-fA-F]+);")
_RE_INTERNAL_MARKER = re.compile(r"(?i)\b(?:TSEG|TSRC)_\d{3,}\b|⟦[^⟧]{1,80}⟧")
_RE_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^\)]+\)")
_RE_MD_LINK = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_RE_BOLD_ASTERISK = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_RE_BOLD_UNDERSCORE = re.compile(r"__(.+?)__", re.DOTALL)
_RE_ITALIC_ASTERISK = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_RE_ITALIC_UNDERSCORE = re.compile(r"(?<!_)_([^_\n]+?)_(?!_)")
_RE_HEADING = re.compile(r"(?m)^\s*#{1,6}\s+")
_RE_CODE_FENCE = re.compile(r"(?m)^\s*(?:```|~~~)(?:[^\n]*)?$")
_RE_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_RE_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_RE_SPACE_AROUND_NL = re.compile(r"[ \t]*\n[ \t]*")
_RE_MANY_NL = re.compile(r"\n{3,}")
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr", "ul",
}
@dataclass(frozen=True)
class TextSalvageResult:
    text: str
    changed: bool
    safe: bool
    reasons: tuple[str, ...] = ()
    removed_markup_count: int = 0
    decoded_entity_count: int = 0
def _tag_signature(token: str) -> str:
    match = _RE_HTML_TAG_PARTS.match(str(token or ""))
    if not match:
        return ""
    closing = "/" if match.group(1) else ""
    name = str(match.group(2) or "").lower()
    return f"<{closing}{name}>" if name else ""
def _source_tag_budget(source_text: str) -> Counter[str]:
    budget: Counter[str] = Counter()
    for token in _RE_HTML_TAG.findall(source_text or ""):
        signature = _tag_signature(token)
        if signature:
            budget[signature] += 1
    return budget
def _source_entity_budget(source_text: str) -> Counter[str]:
    return Counter(token.lower() for token in _RE_ENTITY.findall(source_text or ""))
def _unexpected_markup_tokens(text: str, source_text: str) -> list[str]:
    budget = _source_tag_budget(source_text)
    used: Counter[str] = Counter()
    unexpected: list[str] = []
    for token in _RE_HTML_TAG.findall(text or ""):
        signature = _tag_signature(token)
        if signature and used[signature] < budget[signature]:
            used[signature] += 1
            continue
        unexpected.append(token)
    return unexpected
def detect_markup_leak(text: str, source_text: str = "") -> list[str]:
    """Return model-owned structural leftovers that are absent from source text.
    Literal tag-looking text already present in the immutable source atom is
    protected by a small occurrence budget. This keeps mathematical/literal
    constructs from being destroyed while still rejecting model-added HTML.
    """
    leaks = _unexpected_markup_tokens(text or "", source_text or "")
    source_markers = Counter(m.group(0) for m in _RE_INTERNAL_MARKER.finditer(source_text or ""))
    used_markers: Counter[str] = Counter()
    for match in _RE_INTERNAL_MARKER.finditer(text or ""):
        token = match.group(0)
        if used_markers[token] < source_markers[token]:
            used_markers[token] += 1
        else:
            leaks.append(token)
    return leaks
def _strip_unexpected_html(text: str, source_text: str) -> tuple[str, int]:
    budget = _source_tag_budget(source_text)
    used: Counter[str] = Counter()
    removed = 0
    def repl(match: re.Match) -> str:
        nonlocal removed
        token = match.group(0)
        signature = _tag_signature(token)
        if signature and used[signature] < budget[signature]:
            used[signature] += 1
            return token
        removed += 1
        parts = _RE_HTML_TAG_PARTS.match(token)
        tag_name = str(parts.group(2) or "").lower() if parts else ""
        return "\n" if tag_name in _BLOCK_TAGS else ""
    return _RE_HTML_TAG.sub(repl, text or ""), removed
def _decode_unexpected_entities(text: str, source_text: str) -> tuple[str, int]:
    budget = _source_entity_budget(source_text)
    used: Counter[str] = Counter()
    decoded = 0
    def repl(match: re.Match) -> str:
        nonlocal decoded
        token = match.group(0)
        key = token.lower()
        if used[key] < budget[key]:
            used[key] += 1
            return token
        value = html.unescape(token)
        if value != token:
            decoded += 1
        return value
    return _RE_ENTITY.sub(repl, text or ""), decoded
def _strip_model_markdown(text: str, source_text: str) -> tuple[str, bool]:
    original = text
    source = source_text or ""
    if "![" not in source:
        text = _RE_MD_IMAGE.sub("", text)
    if "](" not in source:
        text = _RE_MD_LINK.sub(r"\1", text)
    if "```" not in source and "~~~" not in source:
        text = _RE_CODE_FENCE.sub("", text)
    if "`" not in source:
        text = _RE_INLINE_CODE.sub(r"\1", text)
    if "**" not in source:
        text = _RE_BOLD_ASTERISK.sub(r"\1", text)
    if "__" not in source:
        text = _RE_BOLD_UNDERSCORE.sub(r"\1", text)
    if "~~" not in source:
        text = _RE_STRIKE.sub(r"\1", text)
    # Single emphasis markers are deliberately last and only stripped when the
    # same marker family is absent from source. They are common literary dust,
    # but can also be meaningful punctuation in source text.
    if "*" not in source:
        text = _RE_ITALIC_ASTERISK.sub(r"\1", text)
    if "_" not in source:
        text = _RE_ITALIC_UNDERSCORE.sub(r"\1", text)
    if not re.search(r"(?m)^\s*#{1,6}\s+", source):
        text = _RE_HEADING.sub("", text)
    return text, text != original
def _normalize_dust_whitespace(text: str) -> str:
    text = (text or "").replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = _RE_SPACE_AROUND_NL.sub("\n", text)
    text = _RE_MANY_NL.sub("\n\n", text)
    text = _RE_MULTI_SPACE.sub(" ", text)
    return text.strip()
def salvage_translation_text(text: str, source_text: str = "") -> TextSalvageResult:
    """Deterministically remove presentation dust from one translated text atom.
    This function never invents words and never rebuilds model-owned DOM. It
    only removes/decodes markup already handled by the Builder in production.
    If structural/internal leftovers remain after cleanup, ``safe`` is False so
    the existing debt/retry mechanism may take over.
    """
    original = str(text or "")
    source = str(source_text or "")
    current = original
    reasons: list[str] = []
    current, removed_markup = _strip_unexpected_html(current, source)
    if removed_markup:
        reasons.append("html_markup")
    current, decoded_entities = _decode_unexpected_entities(current, source)
    if decoded_entities:
        reasons.append("html_entity")
    current, markdown_changed = _strip_model_markdown(current, source)
    if markdown_changed:
        reasons.append("markdown")
    normalized = _normalize_dust_whitespace(current)
    if normalized != current:
        reasons.append("whitespace")
    current = normalized
    leaks = detect_markup_leak(current, source)
    if leaks:
        reasons.append("markup_leak")
    safe = not leaks
    if source.strip() and not current.strip():
        safe = False
        reasons.append("empty_after_salvage")
    return TextSalvageResult(
        text=current,
        changed=current != original,
        safe=safe,
        reasons=tuple(dict.fromkeys(reasons)),
        removed_markup_count=removed_markup,
        decoded_entity_count=decoded_entities,
    )
def sanitize_rendered_html_text_nodes(rendered_html: str, source_text: str = "") -> tuple[str, dict]:
    """Repair escaped/literal model markup inside text nodes of an existing HTML result.
    Real DOM elements are untouched. Only NavigableString nodes are processed,
    so source-owned ``<p>/<h1>/...`` structure remains intact. This is intended
    for crash-recovered or already persisted translations produced before the
    atom-level sanitizer existed.
    """
    raw = str(rendered_html or "")
    if not raw:
        return raw, {"changed": False, "nodes": 0, "removed_markup": 0, "decoded_entities": 0}
    soup = BeautifulSoup(raw, "html.parser")
    changed_nodes = 0
    removed_markup = 0
    decoded_entities = 0
    for node in list(soup.find_all(string=True)):
        if isinstance(node, Comment):
            continue
        if node.find_parent(["script", "style"]):
            continue
        node_text = str(node)
        if not (
            _RE_HTML_TAG.search(node_text)
            or _RE_ENTITY.search(node_text)
            or "```" in node_text
            or "**" in node_text
            or "__" in node_text
            or "~~" in node_text
            or "\u200b" in node_text
            or "\ufeff" in node_text
        ):
            continue
        salvaged = salvage_translation_text(node_text, source_text)
        if not salvaged.safe or not salvaged.changed:
            continue
        node.replace_with(NavigableString(salvaged.text))
        changed_nodes += 1
        removed_markup += salvaged.removed_markup_count
        decoded_entities += salvaged.decoded_entity_count
    return str(soup), {
        "changed": changed_nodes > 0,
        "nodes": changed_nodes,
        "removed_markup": removed_markup,
        "decoded_entities": decoded_entities,
    }
