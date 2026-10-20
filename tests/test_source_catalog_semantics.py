"""Offline v2 semantic-fingerprint contract; no DB/API/worker integration."""
from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from chaptera_server.source_catalog_semantics import (
    DOMAIN, VERSION, CatalogSemanticEntry, canonical_semantic_bytes,
    semantic_catalog_sha256,
)

SOURCE = "royalroad"
BOOK_ID = "fiction/142007"
URL = "https://www.royalroad.com/fiction/142007/aura/chapter/60001/prologue"


def prologue(**changes):
    return replace(CatalogSemanticEntry(1, "60001", URL, "Пролог", "unknown"), **changes)


def chapter(**changes):
    return replace(CatalogSemanticEntry(
        2, "60002", URL.replace("60001/prologue", "60002/chapter-1"),
        "第1章 Первый", "chapter", author_number=1,
        classification_source="structured", classification_confidence="high",
    ), **changes)


def digest(entries=None, source=SOURCE, book_id=BOOK_ID):
    return semantic_catalog_sha256(source, book_id,
                                   [prologue(), chapter()] if entries is None else entries)


def test_versioned_domain_bytes_and_reference_vector():
    raw = canonical_semantic_bytes(SOURCE, BOOK_ID, [prologue(), chapter()])
    assert VERSION == 2
    assert raw.startswith(DOMAIN)
    assert raw[len(DOMAIN):len(DOMAIN) + 4] == bytes((0, 0, 0, len(SOURCE)))
    assert hashlib.sha256(raw).hexdigest() == digest()
    assert digest() == "5eed13e0e77429aed5f2af2806353cc7237b8d61da6cbc579767780f5aac8545"


def test_title_type_author_number_and_confidence_change_sha_but_not_v1_identity():
    original = [prologue(), chapter()]
    v1 = lambda entries: hashlib.sha256("\n".join(
        f"{x.source_ordinal}\t{x.source_id}\t{x.url}" for x in entries
    ).encode()).hexdigest()
    variants = [
        [prologue(title="Пролог исправлен"), chapter()],
        [prologue(entry_type="extra", classification_source="structured",
                  classification_confidence="high"), chapter()],
        [prologue(), chapter(author_number=2)],
        [prologue(), chapter(classification_source="title_pattern",
                             classification_confidence="medium")],
    ]
    assert v1(original) == "eee2d4cb0cad5ca410cc25b87090a745ee6e1ad99ebf87678801224b33f46e6b"
    assert all(v1(v) == v1(original) for v in variants)
    assert all(digest(v) != digest(original) for v in variants)
    assert len({digest(v) for v in variants}) == 4


def test_source_and_book_identity_are_bound_into_semantic_digest():
    assert digest(book_id="fiction/142008") != digest()
    assert digest(source="webnovel") != digest()
    assert digest([prologue(source_id="different"), chapter()]) != digest()
    assert digest([prologue(url=URL + "/changed"), chapter()]) != digest()


def test_unicode_nfc_and_byte_length_framing_preserve_identity():
    composed = prologue(title="Café\tЧасть\n2")
    decomposed = prologue(title="Cafe\u0301\tЧасть\n2")
    assert digest([composed]) == digest([decomposed])
    assert digest([prologue(title="A\tB", source_id="C")]) != digest(
        [prologue(title="A", source_id="B\tC")]
    )
    assert digest([prologue(source_id="caf\u00e9")]) == digest(
        [prologue(source_id="cafe\u0301")]
    )
    assert digest([prologue(title="a\nX")]) != digest([prologue(title="aX\n")])
    assert digest(book_id="Café") == digest(book_id="Cafe\u0301")


def test_duplicate_author_numbers_are_legal_but_duplicate_source_identity_is_not():
    duplicate_author = [
        chapter(source_ordinal=1, author_number=1),
        chapter(source_id="other", source_ordinal=2, author_number=1),
    ]
    assert len(digest(duplicate_author)) == 64
    with pytest.raises(ValueError, match="Duplicate normalized source ID"):
        digest([chapter(source_ordinal=1), chapter(source_ordinal=2)])
    with pytest.raises(ValueError, match="strictly increasing"):
        digest([chapter(), prologue()])
    with pytest.raises(ValueError, match="strictly increasing"):
        digest([prologue(), prologue(source_id="next")])


def test_unknown_semantics_are_explicit_never_inferred_as_chapter():
    assert prologue().entry_type == "unknown"
    for invalid in [
        prologue(entry_type="chapter"),
        prologue(entry_type="service", classification_confidence="high"),
        chapter(classification_source="none", classification_confidence="unverified"),
        chapter(classification_source="structured", classification_confidence="medium"),
        chapter(entry_type="unknown"),
        chapter(entry_type="ambiguous"),
        chapter(entry_type=[]),
        chapter(classification_source=[]),
        chapter(classification_confidence=[]),
    ]:
        with pytest.raises(ValueError, match="classification"):
            digest([invalid])
    assert len(digest([prologue(entry_type="service", classification_source="title_pattern",
                                classification_confidence="medium")])) == 64


@pytest.mark.parametrize("invalid", [None, 0, -2, True, 10_000_001, "1", 1.0])
def test_source_ordinal_rejects_noninteger_out_of_bounds(invalid):
    with pytest.raises(ValueError, match="source_ordinal"):
        digest([prologue(source_ordinal=invalid)])


@pytest.mark.parametrize("invalid", [0, -1, True, "1", 10_000_001, 1.5])
def test_author_number_rejects_noninteger_out_of_bounds(invalid):
    with pytest.raises(ValueError, match="author_number"):
        digest([chapter(author_number=invalid)])


def test_bounds_invalid_unicode_and_record_types():
    for entries in ([], [prologue()] * 2001, ["untyped"], (x for x in (prologue(),))):
        with pytest.raises(ValueError):
            digest(entries)
    for invalid in ["", "\t\n", "a" * 4097, "\ud800"]:
        with pytest.raises(ValueError):
            digest([prologue(title=invalid)])
    for invalid in ["", "RoyalRoad", "royalroad/other", "r" * 65]:
        with pytest.raises(ValueError):
            digest(source=invalid)
    with pytest.raises(ValueError):
        digest(book_id="")
    with pytest.raises(ValueError, match="2 MiB"):
        digest([prologue(source_id=f"a{i}", source_ordinal=i + 1,
                         title="好" * 1200, url=URL + str(i)) for i in range(2000)])


def test_bounded_full_catalog_order_deterministic():
    all_rows = [prologue(source_ordinal=i + 1, source_id=f"ID{i}") for i in range(2000)]
    assert digest(all_rows) == digest(tuple(all_rows))
    assert digest(all_rows) != digest(all_rows[:-1])
    with pytest.raises(ValueError, match="strictly increasing"):
        digest(all_rows[::-1])
