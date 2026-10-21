"""Regress FullCycle's evidence-only Timotxt source normalization boundaries.

These synthetic terms do not derive from copyrighted novel chapters.
No calls to HTTP, SQLite, Gemini, import/translation or live services.
"""
from __future__ import annotations

import copy
import hashlib

import pytest

from chaptera_server.source_timotxt_normalization import (
    derive_timotxt_candidate,
    learn_timotxt_mapping,
)


def parallel_rows():
    return [
        {"project_id": 1, "original": "空間寶녪", "rus": "Камень Пространства"},
        {"project_id": 2, "original": "空間寶石", "rus": "Камень Пространства"},
        # Different Russian targets, but the clean spelling is unique,
        # and two intact CJK characters strongly anchor alignment.
        {"project_id": 3, "original": "綠꾫人", "rus": "Зелёный гигант"},
        {"project_id": 4, "original": "綠巨人", "rus": "Халк"},
    ]


def test_fullcycle_evidence_classes_are_ported_without_guessing():
    evidence = learn_timotxt_mapping(parallel_rows())
    assert evidence["mapping"] == {"꾫": "巨", "녪": "石"}
    assert evidence["conflicts"] == {}
    assert evidence["mapping_size"] == 2
    assert evidence["audit"]["녪"]["reason"] == "same_target_exact_position"
    assert evidence["audit"]["꾫"]["reason"] == "unique_structural_exact_position"
    reordered = learn_timotxt_mapping(list(reversed(parallel_rows())))
    assert evidence["mapping"] == reordered["mapping"]
    assert evidence["fingerprint"] == reordered["fingerprint"]


def test_conflicting_claim_for_same_glyph_must_fail_closed():
    rows = [
        {"project_id": 1, "original": "甲놊乙", "rus": "A"},
        {"project_id": 2, "original": "甲不乙", "rus": "A"},
        {"project_id": 3, "original": "丙놊丁", "rus": "B"},
        {"project_id": 4, "original": "丙天丁", "rus": "B"},
    ]
    learned = learn_timotxt_mapping(rows)
    assert "놊" not in learned["mapping"]
    assert learned["conflicts"]["놊"] == ["不", "天"]
    derived = derive_timotxt_candidate("甲놊乙", learned)
    assert derived["normalized_text"] == "甲놊乙"
    assert derived["unresolved_glyphs"] == 1
    assert derived["status"] == "needs_review"


def test_unanchored_single_pair_must_not_create_mapping():
    rows = [
        {"project_id": 1, "original": "놊", "rus": "Один"},
        {"project_id": 2, "original": "天", "rus": "Один"},
    ]
    learned = learn_timotxt_mapping(rows)
    assert learned["mapping"] == {}
    assert derive_timotxt_candidate("놊", learned)["status"] == "needs_review"


def test_derived_text_never_rewrites_raw_or_guesses_unmapped_glyphs():
    learned = learn_timotxt_mapping(parallel_rows())
    raw = "空間寶녪與놅"
    result = derive_timotxt_candidate(raw, learned)
    assert raw == result["raw_text"]
    assert result["normalized_text"] == "空間寶石與놅"
    assert result["raw_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert result["normalized_sha256"] != result["raw_sha256"]
    assert result["mapping_used"] == {"녪": "石"}
    assert result["raw_suspect_glyphs"] == 2
    assert result["replacements"] == 1
    assert result["unresolved_glyphs"] == 1
    assert result["status"] == "needs_review"
    assert result["candidate_only"] is True
    complete = derive_timotxt_candidate("空間寶녪", learned)
    assert complete["normalized_text"] == "空間寶石"
    assert complete["status"] == "normalized_candidate"
    assert complete["candidate_only"] is True


def test_jamo_and_private_use_glyphs_cannot_be_hidden_by_syllable_map():
    learned = learn_timotxt_mapping(parallel_rows())
    for raw in ("普通\u1100文字", "中文\u3131文字", "中文\ue001文字"):
        result = derive_timotxt_candidate(raw, learned)
        assert result["normalized_text"] == raw
        assert result["status"] == "needs_review"
        assert result["unresolved_glyphs"] == 1


def test_provenance_fingerprint_and_mapping_must_not_be_tampered():
    clean = learn_timotxt_mapping(parallel_rows())
    stale = copy.deepcopy(clean)
    stale["mapping"]["녪"] = "土"
    with pytest.raises(ValueError, match="fingerprint"):
        derive_timotxt_candidate("空間寶녪", stale)
    conflicted = copy.deepcopy(clean)
    conflicted["conflicts"]["녪"] = ["石", "土"]
    with pytest.raises(ValueError, match="conflicted"):
        derive_timotxt_candidate("空間寶녪", conflicted)
    forged = copy.deepcopy(clean)
    forged["mapping"]["土"] = "木"
    with pytest.raises(ValueError, match="Unproven"):
        derive_timotxt_candidate("空間寶녪", forged)


def test_clean_input_keeps_same_source_digest_and_is_still_candidate_only():
    evidence = learn_timotxt_mapping([])
    value = "第一章普通中文文字"
    out = derive_timotxt_candidate(value, evidence)
    assert out["status"] == "clean"
    assert out["raw_text"] == out["normalized_text"] == value
    assert out["raw_sha256"] == out["normalized_sha256"]
    assert out["candidate_only"]
