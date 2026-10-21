"""Pure, evidence-bound Timotxt glyph normalization: no storage or network.

Adapted from the user's FullCycle R15.1.27 glossary parallel evidence model.
This module produces a *candidate derived layer*. It NEVER changes original
RAW, auto-accepts damaged chapters, stores a glossary, or queues translation.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

_HANGUL_SYLLABLE = re.compile(r"[\uac00-\ud7a3]")
_SUSPECT_GLYPH = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af\ue000-\uf8ff]")
_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF),
               (0xF900, 0xFAFF), (0x20000, 0x323AF))


def _hangul(char: str) -> bool:
    return len(char) == 1 and _HANGUL_SYLLABLE.fullmatch(char) is not None


def _cjk(char: str) -> bool:
    return len(char) == 1 and any(a <= ord(char) <= b for a, b in _CJK_RANGES)


def _fingerprint(mapping: dict[str, str]) -> str:
    pairs = json.dumps(sorted(mapping.items()), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(pairs.encode("utf-8")).hexdigest()


def _pair(corrupt: str, clean: str) -> tuple[list[tuple[str, str]], int] | None:
    """Only equal-length exact-position Hangul -> CJK with unchanged anchors."""
    if not corrupt or len(corrupt) != len(clean) or corrupt == clean:
        return None
    if _HANGUL_SYLLABLE.search(clean):
        return None
    substitutions: list[tuple[str, str]] = []
    anchors = 0
    for a, b in zip(corrupt, clean):
        if a == b:
            if _cjk(a):
                anchors += 1
        elif _hangul(a) and _cjk(b):
            substitutions.append((a, b))
        else:
            return None
    return (substitutions, anchors) if substitutions else None


def learn_timotxt_mapping(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build the same narrow FullCycle R15.1.27 proof classes.

    Strong proof: same Russian canonical target + at least one retained CJK
    anchor, or two independent exact-position pairs.
    Secondary proof: at least two CJK anchors + exactly one clean source
    spelling with matching unaffected character positions.
    Any contradictory replacement for one Hangul glyph excludes that glyph.
    """
    corpus: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        original = str(item.get("original") or "").strip()
        if not original:
            continue
        try:
            project = int(item.get("project_id") or 0)
        except (TypeError, ValueError):
            project = 0
        corpus.append({
            "original": original,
            "rus": str(item.get("rus") or item.get("translation") or "").strip(),
            "project_id": project,
        })

    clean_by_len: dict[int, list[dict[str, Any]]] = defaultdict(list)
    clean_by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    corrupt_rows: list[dict[str, Any]] = []
    for row in corpus:
        if _HANGUL_SYLLABLE.search(row["original"]):
            corrupt_rows.append(row)
        else:
            clean_by_len[len(row["original"])].append(row)
            if row["rus"]:
                clean_by_target[row["rus"].casefold()].append(row)

    claims: dict[str, list[tuple[str, str, str, str, int, int, int]]] = defaultdict(list)
    seen: set[tuple[str, str, str, int, int]] = set()

    def record(corrupt: dict[str, Any], clean: dict[str, Any], kind: str,
               subs: list[tuple[str, str]], anchors: int) -> None:
        key = (corrupt["original"], clean["original"], kind,
               corrupt["project_id"], clean["project_id"])
        if key in seen:
            return
        seen.add(key)
        for source, target in subs:
            claims[source].append((
                target, kind, corrupt["original"], clean["original"], anchors,
                corrupt["project_id"], clean["project_id"],
            ))

    for corrupt in corrupt_rows:
        rus = corrupt["rus"].casefold()
        if rus:
            for clean in clean_by_target.get(rus, []):
                proof = _pair(corrupt["original"], clean["original"])
                if proof is not None:
                    subs, anchors = proof
                    record(corrupt, clean, "same_target", subs, anchors)

        candidates: list[tuple[dict[str, Any], list[tuple[str, str]], int]] = []
        for clean in clean_by_len.get(len(corrupt["original"]), []):
            proof = _pair(corrupt["original"], clean["original"])
            if proof is not None and proof[1] >= 2:
                subs, anchors = proof
                candidates.append((clean, subs, anchors))
        if len({row["original"] for row, _, _ in candidates}) == 1:
            for clean, subs, anchors in candidates:
                record(corrupt, clean, "structural_unique", subs, anchors)

    mapping: dict[str, str] = {}
    conflicts: dict[str, list[str]] = {}
    audit: dict[str, dict[str, Any]] = {}
    for source, entries in sorted(claims.items()):
        targets = {entry[0] for entry in entries}
        if len(targets) != 1:
            conflicts[source] = sorted(targets)
            continue
        target = next(iter(targets))
        strong = [e for e in entries if e[1] == "same_target"]
        independent = {(e[2], e[3], e[5], e[6]) for e in strong}
        anchors = max(e[4] for e in entries)
        if strong and (anchors >= 1 or len(independent) >= 2):
            reason = "same_target_exact_position"
        elif anchors >= 2:
            reason = "unique_structural_exact_position"
        else:
            continue
        mapping[source] = target
        audit[source] = {
            "target": target,
            "reason": reason,
            "evidence_pairs": len({(e[2], e[3], e[5], e[6]) for e in entries}),
            "max_anchors": anchors,
        }

    return {
        "schema": 1,
        "source": "fullcycle_timotxt_glossary_parallel_evidence",
        "mapping": mapping,
        "fingerprint": _fingerprint(mapping),
        "conflicts": conflicts,
        "audit": audit,
        "corpus_rows": len(corpus),
        "corrupt_rows": len(corrupt_rows),
        "mapping_size": len(mapping),
    }


def derive_timotxt_candidate(raw: str, learned: dict[str, Any]) -> dict[str, Any]:
    """Produce a separate candidate with SHA/provenance, never rewrite RAW.

    Unresolved Hangul, Jamo and PUA remain unchanged and marked needs_review.
    A verified mapping does NOT imply permission to bypass stage/confirm.
    """
    if not isinstance(raw, str) or not isinstance(learned, dict):
        raise ValueError("Raw and learned mapping report are required")
    mapping = learned.get("mapping")
    conflicts = learned.get("conflicts") or {}
    if not isinstance(mapping, dict) or not isinstance(conflicts, dict):
        raise ValueError("Invalid mapping or conflicts")
    if any(not isinstance(a, str) or not isinstance(b, str)
           or not _hangul(a) or not _cjk(b) or a in conflicts
           for a, b in mapping.items()):
        raise ValueError("Unproven or conflicted glyph mapping")
    fingerprint = _fingerprint(mapping)
    if learned.get("fingerprint") != fingerprint:
        raise ValueError("Stale or tampered mapping fingerprint")

    counts: Counter[str] = Counter()
    out: list[str] = []
    for char in raw:
        target = mapping.get(char)
        if target:
            out.append(target)
            counts[char] += 1
        else:
            out.append(char)
    candidate = "".join(out)
    remaining = len(_SUSPECT_GLYPH.findall(candidate))
    replacements = sum(counts.values())
    return {
        "source": "timotxt",
        "schema": 1,
        "raw_text": raw,
        "normalized_text": candidate,
        "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "normalized_sha256": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
        "mapping_fingerprint": fingerprint,
        "mapping_used": {a: mapping[a] for a in sorted(counts)},
        "raw_suspect_glyphs": len(_SUSPECT_GLYPH.findall(raw)),
        "replacements": replacements,
        "unresolved_glyphs": remaining,
        "status": "needs_review" if remaining else (
            "normalized_candidate" if replacements else "clean"
        ),
        "candidate_only": True,
    }
