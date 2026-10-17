"""Source-owned atom JSON transport, bounded batching, and deterministic QA.

The desktop's coverage/reconciliation/render algorithms are reused verbatim.
This module only adapts those algorithms to a headless, chapter-scoped provider.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable

from .core.translation_units import (
    CoverageMap, SourceAtom, TargetSegment, apply_debt_segments, build_coverage_provenance,
    build_missing_debts, build_render_plan, build_semantic_groups,
    build_source_atoms, expected_render_domain_rows, reconcile_coverage_response,
    serialize_source_atoms_for_prompt,
)

def atom_checkpoint_key(prompt: str, payload: str, role: str) -> str:
    """Bind a result to exact model instructions, source atom scope and role."""
    digest = hashlib.sha256()
    for part in ("atoms-v1-checkpoint-1", role, prompt, payload):
        raw = part.encode("utf-8")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


MAX_BATCH_ATOMS = 24
MAX_BATCH_SOURCE_CHARS = 7000
MAX_REPAIR_ROUNDS = 2
MAX_REPAIR_DOMAINS_PER_ROUND = 12


@dataclass(frozen=True)
class AtomPacket:
    atoms: tuple[SourceAtom, ...]
    payload: str


def prepare_source_atoms(source_units: list[dict], *, chapter_id: str) -> list[SourceAtom]:
    """Preserve exact IDs/text while partitioning oversized DOM render domains.

    Partitioning is deterministic and source-owned, never provider-assigned. Each
    partition is independently renderable back into the original text node.
    """
    copied: list[dict] = []
    previous_domain = None
    part = 0
    count = 0
    size = 0
    for item in source_units:
        original_domain = str(item.get('render_domain_id') or '')
        value = str(item.get('source_text') or '')
        if previous_domain != original_domain:
            previous_domain, part, count, size = original_domain, 0, 0, 0
        if count and (count >= MAX_BATCH_ATOMS or size + len(value) > MAX_BATCH_SOURCE_CHARS):
            part, count, size = part + 1, 0, 0
        copied.append({
            **item,
            'render_domain_id': f'{original_domain}:part{part:04d}' if original_domain else f'atom:{len(copied):06d}',
        })
        count += 1
        size += len(value)
    atoms = build_source_atoms(copied, job_id='j0', chapter_id=chapter_id)
    if len({atom.atom_id for atom in atoms}) != len(atoms):
        raise ValueError('Source atom IDs are not unique')
    return atoms


def packets_for_atoms(atoms: Iterable[SourceAtom]) -> list[AtomPacket]:
    source_atoms = list(atoms)
    lookup = {atom.atom_id: atom for atom in source_atoms}
    if len(lookup) != len(source_atoms):
        raise ValueError('Duplicate source atoms in batch')
    groups = expected_render_domain_rows(source_atoms)
    result: list[AtomPacket] = []
    current: list[SourceAtom] = []
    size = 0
    for _, covers in groups:
        rows = [lookup[atom_id] for atom_id in covers]
        row_chars = sum(len(atom.source_text) for atom in rows)
        if current and (len(current) + len(rows) > MAX_BATCH_ATOMS or size + row_chars > MAX_BATCH_SOURCE_CHARS):
            result.append(AtomPacket(tuple(current), serialize_source_atoms_for_prompt(current, build_semantic_groups(current))))
            current, size = [], 0
        current.extend(rows)
        size += row_chars
    if current:
        result.append(AtomPacket(tuple(current), serialize_source_atoms_for_prompt(current, build_semantic_groups(current))))
    return result


def parse_packet_response(raw: str, atoms: tuple[SourceAtom, ...]) -> CoverageMap:
    coverage = reconcile_coverage_response(raw, list(atoms), default_job_id='j0', enforce_render_domain_contract=True)
    # Desktop regression test_later_overlapping_cover_does_not_erase_first_proven_segment:
    # source-owned reconciler keeps the FIRST valid claim and rejects only a later
    # overlap. The missing atom remains a repair debt; rejecting this WHOLE
    # response here discards proven paid work and diverges from the desktop.
    # Unknown IDs and noncontiguous covers still fail closed before checkpoint.
    dangerous = {'unknown_atom_id', 'non_contiguous_coverage'}
    if any(item.get('reason') in dangerous for item in coverage.invalid_segments):
        raise ValueError('Atom response contains unknown, duplicate or non-contiguous identity')
    if not coverage.missing_atom_ids and any(item.get('reason') == 'malformed_lines' for item in coverage.invalid_segments):
        raise ValueError('Atom response contains unexplained malformed records')
    return coverage


def salvage_partial_atom_response(partial_text: str, atoms: tuple[SourceAtom, ...]) -> str | None:
    """Extract complete JSON segment objects, then require desktop reconciliation.

    A MAX_TOKENS/transport interruption may truncate a JSON object halfway
    through a later segment. We reuse ONLY fully decoded prefix objects and
    pass their source identity to the existing CoverageMap validator. This
    is a transport syntax adapter, not a second translation/recovery algorithm.

    Never salvage unknown IDs, duplicate/overlapping coverage, noncontiguous
    segments, arbitrary prose, thoughts, or oversized untrusted response data.
    """
    if not isinstance(partial_text, str) or not 0 < len(partial_text) <= 2_000_000:
        return None
    # A provider can signal MAX_TOKENS even if the JSON answer was complete.
    try:
        complete = parse_packet_response(partial_text, atoms)
        if complete.segments:
            return partial_text
    except (ValueError, TypeError):
        pass
    match = re.match(r'\s*\{\s*"segments"\s*:\s*\[', partial_text)
    if match is None:
        return None
    pos = match.end()
    decoder = json.JSONDecoder()
    records: list[dict] = []
    while pos < len(partial_text):
        while pos < len(partial_text) and partial_text[pos].isspace():
            pos += 1
        if pos >= len(partial_text) or partial_text[pos] == ']':
            break
        if partial_text[pos] != '{':
            return None
        try:
            value, pos = decoder.raw_decode(partial_text, pos)
        except json.JSONDecodeError:
            # The next segment is physically incomplete; do not guess its
            # translation or extend a truncated string.
            break
        if not isinstance(value, dict):
            return None
        records.append(value)
        if len(records) > len(atoms):
            return None
        while pos < len(partial_text) and partial_text[pos].isspace():
            pos += 1
        if pos >= len(partial_text) or partial_text[pos] == ']':
            break
        if partial_text[pos] != ',':
            return None
        pos += 1
    if not records:
        return None
    candidate = json.dumps({'segments': records}, ensure_ascii=False)
    # Preserve only source-proven segments; reject forged and overlapping IDs.
    proof = parse_packet_response(candidate, atoms)
    return candidate if proof.segments else None


def merge_proven_segments(atoms: list[SourceAtom], parts: Iterable[CoverageMap]) -> CoverageMap:
    """Re-run reconciliation over all proven segments in the caller-owned scope."""
    payload = {'segments': [
        {'covers': list(segment.covers), 'translation': segment.translation}
        for part in parts for segment in part.segments
    ]}
    return parse_packet_response(json.dumps(payload, ensure_ascii=False), tuple(atoms))


def repair_domains(coverage: CoverageMap, marked_source: str) -> list[tuple[SourceAtom, ...]]:
    """Source-owned repair scopes, joining only proven adjacent contamination.

    The original desktop detector reports an adjacent-target contamination
    *pair* as one completeness debt. Repairing those source domains
    independently discards the paired context and can reintroduce semantic
    spillover. Keep normal missing/alignment debts domain-local; join only
    the explicit neighboring pair, never cross the atom/character packet cap.
    """
    plan = build_render_plan(marked_source, coverage)
    affected = {atom_id for debt in (*build_missing_debts(coverage), *plan.debts)
                for atom_id in debt.atom_ids}
    if not affected:
        return []
    lookup = {atom.atom_id: atom for atom in coverage.atoms}
    rows = [tuple(lookup[atom_id] for atom_id in covers)
            for _, covers in expected_render_domain_rows(coverage.atoms)]
    selected = {index for index, row in enumerate(rows)
                if affected.intersection(atom.atom_id for atom in row)}
    # Bounded disjoint sets preserve the source order and coalesce overlapping
    # contamination pairs. No grouping based on arbitrary model descriptions.
    parent = {index: index for index in selected}

    def find(index: int) -> int:
        while parent[index] != index:
            index = parent[index]
        return index

    by_id = {atom.atom_id: index for index, row in enumerate(rows) for atom in row}
    for debt in plan.debts:
        if debt.reason != 'adjacent_target_contamination_suspected':
            continue
        indexes = sorted({by_id[atom_id] for atom_id in debt.atom_ids if atom_id in by_id})
        if len(indexes) < 2 or indexes != list(range(indexes[0], indexes[-1] + 1)):
            continue
        if not set(indexes).issubset(selected):
            continue
        participating = {find(index) for index in indexes}
        merged_indices = [index for index in selected if find(index) in participating]
        merged_atoms = [atom for index in sorted(merged_indices) for atom in rows[index]]
        if (len(merged_atoms) > MAX_BATCH_ATOMS or
                sum(len(atom.source_text) for atom in merged_atoms) > MAX_BATCH_SOURCE_CHARS):
            # Oversized contamination scopes keep their original bounded
            # domain-local repairs and must still pass final desktop QA.
            continue
        root = min(participating)
        for index in participating:
            parent[index] = root

    groups: dict[int, list[SourceAtom]] = {}
    for index in sorted(selected):
        groups.setdefault(find(index), []).extend(rows[index])
    return [tuple(group) for group in groups.values()]


def apply_proven_repair(coverage: CoverageMap, repair: CoverageMap) -> CoverageMap:
    """Replace complete source-owned scopes, including bounded repair pairs.

    Gemini must supply one separate complete segment per original DOM render
    domain. A cross-domain many-to-one merge, foreign source or partial repair
    never gains authority to replace existing proven translation.
    """
    if repair.missing_atom_ids or not repair.segments:
        return coverage
    source_by_id = {atom.atom_id: atom for atom in coverage.atoms}
    if any(
        atom.atom_id not in source_by_id or
        source_by_id[atom.atom_id] != atom
        for atom in repair.atoms
    ):
        return coverage
    expected = {tuple(covers) for _, covers in expected_render_domain_rows(repair.atoms)}
    delivered = [tuple(segment.covers) for segment in repair.segments]
    if (len(delivered) != len(expected) or set(delivered) != expected or
            any(segment.job_id != coverage.job_id for segment in repair.segments)):
        return coverage
    scope = {atom.atom_id for atom in repair.atoms}
    if {atom_id for segment in repair.segments for atom_id in segment.covers} != scope:
        return coverage
    return apply_debt_segments(coverage, [{
        'atom_ids': sorted(scope),
        'segments': [
            {'covers': list(segment.covers), 'translation': segment.translation}
            for segment in repair.segments
        ],
    }])


def atom_provenance(coverage: CoverageMap) -> list[dict]:
    """Durable, privacy-aware audit with source/target hashes, not duplicated text."""
    import hashlib
    document_id = coverage.atoms[0].chapter_id if coverage.atoms else 'chapter'
    proof = build_coverage_provenance(coverage, document_id=document_id)
    return [{
        'source_unit_id': row['source_unit_id'],
        'target_slot_id': row['target_slot_id'],
        'target_fragment_id': row['target_fragment_id'],
        'target_dom_path': row['target_dom_path'],
        'source_hash': hashlib.sha256(row['source_text'].encode('utf-8')).hexdigest(),
        'target_hash': hashlib.sha256(row['translated_text'].encode('utf-8')).hexdigest(),
        'alignment_kind': row['alignment_kind'],
        'origin': row['origin'],
        'part_index': row['part_index'],
        'part_count': row['part_count'],
    } for row in proof]
