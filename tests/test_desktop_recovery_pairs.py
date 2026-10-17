"""RE-06 desktop regression: repair source-incompatible adjacent *pairs*.

Derived from full_cycle_app31.zip:
- test_translation_recovery_false_positive_regression.py
  ::test_debt_repair_pair_is_rechecked_for_source_incompatible_contamination
  ::test_short_exact_duplicate_from_distinct_source_is_detected
  ::test_identical_target_is_allowed_when_source_repetition_is_identical
- test_translation_contamination_guards.py
  ::test_adjacent_translation_duplicate_creates_pair_completeness_debt
  ::test_short_dialogue_and_repeated_source_are_not_false_positives
Server-specific invariant: one source-owned Gemini request repairs two adjacent
domains when the desktop declares a pair debt; provider may never merge the
two DOM render domains into one output segment. All calls are offline.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from chaptera_translation_core import ChapterInput, TranslationCore, TranslationRejected
from chaptera_translation_core.atom_transport import apply_proven_repair
from chaptera_translation_core.core.translation_units import (
    CoverageMap, SourceAtom, TargetSegment,
)

LEFT = (
    "Заметив, что Фьюри остаётся безучастным, Эндрю многозначительно посмотрел "
    "на Наташу. Мне довольно трудно объяснить вам, что именно информация — "
    "самая драгоценная вещь во Вселенной."
)
RIGHT = (
    "Мне на самом деле трудно объяснить вам, что информация — это самое ценное "
    "во Вселенной, — сказал Эндрю Наташе, заметив, что Фьюри остаётся безучастным."
)


def source_rows(payload: str) -> list[list[dict]]:
    packet = json.loads(payload)
    column = {name: n for n, name in enumerate(packet['columns'])}
    rows = [{name: item[i] for name, i in column.items()}
            for item in packet['rows']]
    groups: list[list[dict]] = []
    for row in rows:
        if groups and groups[-1][0]['render_domain_id'] == row['render_domain_id']:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def response(payload: str, translations: list[str] | None = None) -> str:
    groups = source_rows(payload)
    if translations is None:
        translations = [''.join(row['source_text'] for row in group)
                        for group in groups]
    assert len(groups) == len(translations)
    return json.dumps({
        'segments': [
            {'covers': [row['atom_id'] for row in group], 'translation': translated}
            for group, translated in zip(groups, translations)
        ],
    }, ensure_ascii=False)


def test_contaminated_pair_gets_one_joint_targeted_repair_and_independent_dom_nodes():
    source = '<body><p>完全不同的源文本甲。</p><p>另一个无关的源文本乙。</p></body>'
    fixed = [
        'Первый абзац сообщает о событиях в городе.',
        'Второй абзац рассказывает о другой встрече.',
    ]

    class Provider:
        def __init__(self):
            self.calls = []

        async def generate(self, request):
            self.calls.append(request)
            if request.request_role == 'translation':
                assert request.unit_count == 2
                return response(request.source_html_with_markers, [LEFT, RIGHT])
            assert request.request_role == 'targeted_repair'
            assert request.unit_count == 2, 'pair debt must remain editable as a unit'
            assert len(source_rows(request.source_html_with_markers)) == 2
            return response(request.source_html_with_markers, fixed)

    provider = Provider()
    result = asyncio.run(TranslationCore(provider).translate(ChapterInput(
        'chapter', source, transport='atoms_v1',
    )))
    assert len(provider.calls) == 2, 'one translation and ONE bounded pair repair'
    assert result.translated_html == f'<body><p>{fixed[0]}</p><p>{fixed[1]}</p></body>'
    assert len(result.atom_provenance) == 2
    assert len({row['target_fragment_id'] for row in result.atom_provenance}) == 2


def test_after_single_debt_repair_short_spillover_rechecks_both_neighbors():
    source = '<body><p>你相信老师吗？</p><p>我信，当然信！</p></body>'

    class Provider:
        def __init__(self):
            self.calls = []

        async def generate(self, request):
            self.calls.append(request)
            if len(self.calls) == 1:
                assert request.unit_count == 2
                return response(request.source_html_with_markers,
                                ['Верю, конечно верю!', 'Верю, конечно верю!'])
            if len(self.calls) == 2:
                # First global short duplicate is deliberately advisory, but
                # an incomplete/repaired source domain becomes strict.
                assert request.request_role == 'targeted_repair'
                return response(request.source_html_with_markers)
            return response(request.source_html_with_markers,
                            ['Он спросил, верит ли тот учителю.', 'Верю, конечно верю!'])

    # The initial short duplicate alone must not be treated as a pair:
    # it is unsafe to infer contamination without source-owned repair context.
    # The actual repaired-short case is tested below using a missing source atom.
    class AfterMissing(Provider):
        async def generate(self, request):
            self.calls.append(request)
            if len(self.calls) == 1:
                groups = source_rows(request.source_html_with_markers)
                assert len(groups) == 2
                second = response(request.source_html_with_markers,
                                  ['Первый.', 'Верю, конечно верю!'])
                data = json.loads(second)
                data['segments'] = data['segments'][1:]
                return json.dumps(data, ensure_ascii=False)
            if len(self.calls) == 2:
                assert request.unit_count == 1
                return response(request.source_html_with_markers, ['Верю, конечно верю!'])
            assert request.request_role == 'targeted_repair'
            assert request.unit_count == 2
            return response(request.source_html_with_markers,
                            ['Он спросил, верит ли тот учителю.', 'Верю, конечно верю!'])

    provider = AfterMissing()
    result = asyncio.run(TranslationCore(provider).translate(ChapterInput(
        'short', source, transport='atoms_v1',
    )))
    assert [call.unit_count for call in provider.calls] == [2, 1, 2]
    assert result.translated_html == (
        '<body><p>Он спросил, верит ли тот учителю.</p>'
        '<p>Верю, конечно верю!</p></body>'
    )


def test_author_repeated_source_and_short_dialogue_do_not_trigger_paid_repair():
    source = '<body><p>我信，当然信！</p><p>我信，当然信！</p></body>'
    class Provider:
        def __init__(self):
            self.calls = 0
        async def generate(self, request):
            self.calls += 1
            assert self.calls == 1, 'author repetition is not a contamination debt'
            return response(request.source_html_with_markers,
                            ['Верю, конечно верю!', 'Верю, конечно верю!'])
    provider = Provider()
    result = asyncio.run(TranslationCore(provider).translate(ChapterInput(
        'repeated', source, transport='atoms_v1',
    )))
    assert provider.calls == 1
    assert result.translated_html.count('Верю, конечно верю!') == 2


def test_cross_render_domain_many_to_one_response_cannot_close_pair_debt():
    source = '<body><p>完全不同的源文本甲。</p><p>另一个无关的源文本乙。</p></body>'
    class Provider:
        def __init__(self):
            self.calls = []
        async def generate(self, request):
            self.calls.append(request)
            if request.request_role == 'translation':
                return response(request.source_html_with_markers, [LEFT, RIGHT])
            groups = source_rows(request.source_html_with_markers)
            assert len(groups) == 2
            return json.dumps({'segments': [{
                'covers': [a['atom_id'] for group in groups for a in group],
                'translation': 'Один общий сегмент поверх разных абзацев.',
            }]}, ensure_ascii=False)

    provider = Provider()
    with pytest.raises(TranslationRejected, match='atom_coverage'):
        asyncio.run(TranslationCore(provider).translate(ChapterInput(
            'cross-domain', source, transport='atoms_v1',
        )))
    assert len(provider.calls) == 3  # initial and two bounded, unproved pair attempts


def test_forged_source_in_repair_cannot_replace_existing_valid_atom():
    before = SourceAtom('j0', '000001', '独立源文本甲', 0,
                        render_domain_id='p:1')
    foreign = SourceAtom('j0', '000001', '完全不同的原文', 0,
                         render_domain_id='p:1')
    coverage = CoverageMap('j0', (before,), [
        TargetSegment('j0', 's1', ('000001',), 'Исходный доказанный перевод.'),
    ], [])
    replacement = CoverageMap('j0', (foreign,), [
        TargetSegment('j0', 's2', ('000001',), 'Чужой перевод.', 'debt_repair'),
    ], [])
    updated = apply_proven_repair(coverage, replacement)
    assert updated == coverage
    assert updated.segments[0].translation == 'Исходный доказанный перевод.'
