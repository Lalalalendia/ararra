"""Proposed v2 semantic TOC fingerprint; deliberately not enabled in live jobs.

The existing v1 catalog SHA/selection and job manifests remain authoritative
until an explicit versioned SQLite/CAS migration has been accepted.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

VERSION = 2
DOMAIN = b"RE02-CATALOG-SEMANTICS\x00v2\x00"
MAX_ENTRIES = 2000
MAX_ORDINAL = 10_000_000
MAX_CANONICAL_BYTES = 2 * 1024 * 1024
ENTRY_CODES = {"unknown": 0, "chapter": 1, "extra": 2, "service": 3}
SOURCE_CODES = {"none": 0, "structured": 1, "title_pattern": 2}
CONFIDENCE_CODES = {"unverified": 0, "high": 1, "medium": 2}


@dataclass(frozen=True, slots=True)
class CatalogSemanticEntry:
    """Site identity and independently justified, possibly unknown semantics."""

    source_ordinal: int
    source_id: str
    url: str
    title: str
    entry_type: str
    author_number: int | None = None
    classification_source: str = "none"
    classification_confidence: str = "unverified"


def _utf8(value: str, label: str, limit: int) -> bytes:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    try:
        raw = unicodedata.normalize("NFC", value).encode("utf-8", "strict")
    except UnicodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8") from exc
    if len(raw) > limit:
        raise ValueError(f"{label} exceeds {limit} UTF-8 bytes")
    return raw


def _append(buf: bytearray, data: bytes) -> None:
    if len(buf) + len(data) > MAX_CANONICAL_BYTES:
        raise ValueError("Semantic TOC exceeds 2 MiB canonical byte budget")
    buf.extend(data)


def _text(buf: bytearray, value: str, label: str, limit: int) -> bytes:
    raw = _utf8(value, label, limit)
    _append(buf, len(raw).to_bytes(4, "big") + raw)
    return raw


def _positive(n: int, label: str) -> None:
    if type(n) is not int or not 1 <= n <= MAX_ORDINAL:
        raise ValueError(f"{label} must be an integer in 1..{MAX_ORDINAL}")


def canonical_semantic_bytes(
    source: str, canonical_book_id: str, entries: list[CatalogSemanticEntry] | tuple[CatalogSemanticEntry, ...]
) -> bytes:
    """Unambiguous v2 bytes: domain, length-prefixed NFC UTF-8, then records.

    Record = ordinal:u64be, source_id/url/title:bytes(u32be length + UTF-8),
    author number: presence:u8 [+ u64be], type:u8, source:u8, confidence:u8.
    The count is u32be. Records must be in increasing source-ordinal order.
    """
    if type(entries) not in (list, tuple) or not 1 <= len(entries) <= MAX_ENTRIES:
        raise ValueError("Expected 1..2000 typed TOC entries")
    buf = bytearray(DOMAIN)
    source_raw = _text(buf, source, "source", 64)
    if re.fullmatch(rb"[a-z][a-z0-9_-]*", source_raw) is None:
        raise ValueError("source must be a lowercase canonical adapter ID")
    _text(buf, canonical_book_id, "canonical_book_id", 512)
    _append(buf, len(entries).to_bytes(4, "big"))
    seen_ids: set[bytes] = set()
    last = 0
    for entry in entries:
        if not isinstance(entry, CatalogSemanticEntry):
            raise ValueError("Every semantic TOC entry must be typed")
        _positive(entry.source_ordinal, "source_ordinal")
        if entry.source_ordinal <= last:
            raise ValueError("TOC source ordinals must be strictly increasing")
        last = entry.source_ordinal
        if entry.author_number is not None:
            _positive(entry.author_number, "author_number")
        if (type(entry.entry_type) is not str or entry.entry_type not in ENTRY_CODES or
                type(entry.classification_source) is not str or
                entry.classification_source not in SOURCE_CODES or
                type(entry.classification_confidence) is not str or
                entry.classification_confidence not in CONFIDENCE_CODES):
            raise ValueError("Invalid semantic classification code")
        expected = {"none": ("unknown", "unverified"),
                    "structured": (None, "high"),
                    "title_pattern": (None, "medium")}
        entry_type, confidence = expected[entry.classification_source]
        if (entry_type is not None and entry.entry_type != entry_type or
                entry_type is None and entry.entry_type == "unknown" or
                entry.classification_confidence != confidence):
            raise ValueError("Entry classification lacks matching evidence")
        _append(buf, entry.source_ordinal.to_bytes(8, "big"))
        raw_id = _text(buf, entry.source_id, "source_id", 512)
        if raw_id in seen_ids:
            raise ValueError("Duplicate normalized source ID in TOC")
        seen_ids.add(raw_id)
        _text(buf, entry.url, "url", 4096)
        _text(buf, entry.title, "title", 4096)
        _append(buf, b"\x00" if entry.author_number is None else
                b"\x01" + entry.author_number.to_bytes(8, "big"))
        _append(buf, bytes((ENTRY_CODES[entry.entry_type],
                            SOURCE_CODES[entry.classification_source],
                            CONFIDENCE_CODES[entry.classification_confidence])))
    return bytes(buf)


def semantic_catalog_sha256(
    source: str, canonical_book_id: str, entries: list[CatalogSemanticEntry] | tuple[CatalogSemanticEntry, ...]
) -> str:
    """Versioned semantic digest; never substitute this for legacy v1 SHA."""
    return hashlib.sha256(canonical_semantic_bytes(source, canonical_book_id, entries)).hexdigest()
