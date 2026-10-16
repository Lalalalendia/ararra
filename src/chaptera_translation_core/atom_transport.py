"""Source-owned atom JSON transport, bounded batching, and deterministic QA.

The desktop's coverage/reconciliation/render algorithms are reused verbatim.
This module only adapts those algorithms to a headless, chapter-scoped provider.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

from .core.translation_units import (
    CoverageMap, SourceAtom, TargetSegment, apply_debt_segments, build_coverage_provenance,
    build_missing_debts, build_render_plan, build_semantic_groups,
    build_source_atoms, expected_render_domain_rows, reconcile_coverage_response,
    serialize_source_atoms_for_prompt,
)

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
    dangerous = {'unknown_atom_id', 'overlapping_coverage_later_segment_rejected', 'non_contiguous_coverage'}
    if any(item.get('reason') in dangerous for item in coverage.invalid_segments):
        raise ValueError('Atom response contains unknown, duplicate or non-contiguous identity')
    if not coverage.missing_atom_ids and any(item.get('reason') == 'malformed_lines' for item in coverage.invalid_segments):
        raise ValueError('Atom response contains unexplained malformed records')
    return coverage


def merge_proven_segments(atoms: list[SourceAtom], parts: Iterable[CoverageMap]) -> CoverageMap:
    """Re-run reconciliation over all proven segments in the caller-owned scope."""
    payload = {'segments': [
        {'covers': list(segment.covers), 'translation': segment.translation}
        for part in parts for segment in part.segments
    ]}
    return parse_packet_response(json.dumps(payload, ensure_ascii=False), tuple(atoms))


def repair_domains(coverage: CoverageMap, marked_source: str) -> list[tuple[SourceAtom, ...]]:
    """Select only source-owned render domains affected by missing/alignment debt."""
    plan = build_render_plan(marked_source, coverage)
    affected = {atom_id for debt in (*build_missing_debts(coverage), *plan.debts)
                for atom_id in debt.atom_ids}
    if not affected:
        return []
    lookup = {atom.atom_id: atom for atom in coverage.atoms}
    return [tuple(lookup[atom_id] for atom_id in covers)
            for _, covers in expected_render_domain_rows(coverage.atoms)
            if affected.intersection(covers)]


def apply_proven_repair(coverage: CoverageMap, repair: CoverageMap) -> CoverageMap:
    """Only a complete one-domain repair may replace its previous atom scope."""
    if repair.missing_atom_ids or not repair.segments:
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
