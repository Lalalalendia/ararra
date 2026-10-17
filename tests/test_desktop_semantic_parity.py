"""Desktop test-suite parity: source-owned coverage, recovery and contamination.

Source: full_cycle_app31.zip tests/test_translation_contamination_guards.py,
tests/test_translation_response_identity_normalization.py and
tests/test_translation_recovery_false_positive_regression.py.
Verified against the newer desktop translation_units.py source. Only
deterministic pure behavior is carried to the one-key, one-chapter server.
No desktop PyQt/GUI/task manager or cross-key routing.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from chaptera_translation_core import ChapterInput, TranslationCore, TranslationRejected
from chaptera_translation_core.atom_transport import parse_packet_response
from chaptera_translation_core.core.translation_units import (
    CoverageMap, SourceAtom, TargetSegment, _recovery_duplicate_debts,
    target_contamination_suspected,
)


def atom(atom_id: str, text: str, index: int) -> SourceAtom:
    return SourceAtom('j0', atom_id, text, index, render_domain_id=f'p:{index}')


def items_from_packet(payload: str) -> list[dict]:
    packet = json.loads(payload)
    columns = {name: index for index, name in enumerate(packet['columns'])}
    return [
        {name: row[index] for name, index in columns.items()}
        for row in packet['rows']
    ]


def grouped_segments(payload: str) -> list[dict]:
    groups: list[list[dict]] = []
    for item in items_from_packet(payload):
        if groups and groups[-1][0]['render_domain_id'] == item['render_domain_id']:
            groups[-1].append(item)
        else:
            groups.append([item])
    return [
        {
            'covers': [atom['atom_id'] for atom in group],
            'translation': ''.join(item['source_text'] for item in group)
            .replace('世界', 'Мир').replace('兄弟', 'братья'),
        }
        for group in groups
    ]


def test_desktop_later_duplicate_cover_preserves_first_proof_and_owes_second():
    """Regression: later overlap must not erase the first source-owned proof."""
    atoms = (
        atom('000058', '我只是想加入兄弟會。', 0),
        atom('000059', '紅魔鬼帶著他瞬移離開。', 1),
    )
    raw = '\n'.join([
        json.dumps({'covers': ['000058'], 'translation': 'Я лишь хочу вступить в ваше Братство.'},
                   ensure_ascii=False),
        json.dumps({'covers': ['000058'], 'translation': 'Ошибочный повтор с чужим смыслом.'},
                   ensure_ascii=False),
    ])
    proof = parse_packet_response(raw, atoms)
    assert len(proof.segments) == 1
    assert proof.segments[0].covers == ('000058',)
    assert 'Братство' in proof.segments[0].translation
    assert proof.missing_atom_ids == ['000059']
    assert proof.invalid_segments[-1]['reason'] == 'overlapping_coverage_later_segment_rejected'


def test_overlapping_model_claim_recovers_only_missing_source_domain():
    class Provider:
        def __init__(self):
            self.calls = []

        async def generate(self, request):
            self.calls.append(request)
            valid = grouped_segments(request.source_html_with_markers)
            if request.request_role == 'translation':
                assert len(valid) == 2
                return json.dumps({
                    'segments': [
                        valid[0],
                        {'covers': valid[0]['covers'], 'translation': 'Чужой перевод второго абзаца.'},
                    ]
                }, ensure_ascii=False)
            assert request.request_role == 'targeted_repair'
            assert request.unit_count == 1
            assert len(valid) == 1
            return json.dumps({'segments': valid}, ensure_ascii=False)

    provider = Provider()
    result = asyncio.run(TranslationCore(provider).translate(ChapterInput(
        'chapter', '<body><p>世界。</p><p>兄弟。</p></body>', transport='atoms_v1',
    )))
    assert result.translated_html == '<body><p>Мир。</p><p>братья。</p></body>'
    assert len(result.atom_provenance) == 2
    assert len(provider.calls) == 2
    assert provider.calls[0].unit_count == 2
    assert provider.calls[1].unit_count == 1
    assert '世界' not in provider.calls[1].source_html_with_markers
    assert '兄弟' in provider.calls[1].source_html_with_markers


def test_unknown_atom_after_valid_segment_remains_fatal_no_guessing():
    class Foreign:
        calls = 0
        async def generate(self, request):
            self.calls += 1
            valid = grouped_segments(request.source_html_with_markers)[0]
            return json.dumps({'segments': [
                valid, {'covers': ['not-from-original'], 'translation': 'Подмена'},
            ]}, ensure_ascii=False)

    provider = Foreign()
    with pytest.raises(TranslationRejected, match='atom_identity'):
        asyncio.run(TranslationCore(provider).translate(ChapterInput(
            'chapter', '<p>世界。</p>', transport='atoms_v1',
        )))
    assert provider.calls == 1


def test_noncontiguous_atom_identity_still_fails_closed():
    atoms = (
        SourceAtom('j0', '000000', '文本甲', 0, render_domain_id='p:0'),
        SourceAtom('j0', '000001', '文本乙', 1, render_domain_id='p:0'),
        SourceAtom('j0', '000002', '文本丙', 2, render_domain_id='p:0'),
    )
    raw = json.dumps({'segments': [{
        'covers': ['000000', '000002'], 'translation': 'Нельзя пропустить середину',
    }]}, ensure_ascii=False)
    with pytest.raises(ValueError, match='unknown, duplicate or non-contiguous'):
        parse_packet_response(raw, atoms)


def test_adjacent_json_objects_and_wrong_provider_job_id_keep_server_owned_scope():
    atoms = (atom('000000', '文本甲', 0), atom('000001', '文本乙', 1))
    raw = (
        json.dumps({'job_id': 'wrong-id', 'segment_id': 'same',
                    'covers': ['000000'], 'translation': 'Первый.'}, ensure_ascii=False)
        + json.dumps({'job_id': 'wrong-id', 'segment_id': 'same',
                      'covers': ['000001'], 'translation': 'Второй.'}, ensure_ascii=False)
    )
    proof = parse_packet_response(raw, atoms)
    assert proof.complete
    assert [x.translation for x in proof.segments] == ['Первый.', 'Второй.']
    assert [x.segment_id for x in proof.segments] == ['s000000', 's000001']
    assert {x.job_id for x in proof.segments} == {'j0'}


def test_punctuation_only_original_can_have_punctuation_only_translation():
    proof = parse_packet_response(
        json.dumps({'covers': ['000000'], 'translation': '…'}, ensure_ascii=False),
        (atom('000000', '……', 0),),
    )
    assert proof.complete
    assert proof.segments[0].translation == '…'


def test_desktop_contamination_guard_distinguishes_drift_from_author_repetition():
    left = (
        'Заметив, что Фьюри остаётся безучастным, Эндрю многозначительно '
        'посмотрел на Наташу. Мне трудно объяснить вам, что информация '
        'является самой драгоценной вещью во Вселенной.'
    )
    right = (
        'Мне на самом деле трудно объяснить вам, что информация — это '
        'самое ценное во Вселенной, — сказал Эндрю Наташе, заметив, '
        'что Фьюри остаётся безучастным.'
    )
    assert target_contamination_suspected(
        left, right, '独立源文本甲', '完全不同的源文本乙',
    )
    assert not target_contamination_suspected(left, left, '相同的源文本', '相同的源文本')


def test_repeated_original_is_not_labeled_missing_due_to_identical_russian_target():
    atoms = (
        atom('000000', '同一句重复文本。', 0),
        atom('000001', '同一句重复文本。', 1),
    )
    coverage = CoverageMap('j0', atoms, [
        TargetSegment('j0', 's0', ('000000',), 'Это авторский повтор с одинаковым смыслом.'),
        TargetSegment('j0', 's1', ('000001',), 'Это авторский повтор с одинаковым смыслом.'),
    ], [])
    assert _recovery_duplicate_debts(coverage) == []
