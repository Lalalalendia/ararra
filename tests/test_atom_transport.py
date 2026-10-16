from __future__ import annotations

import asyncio
import json

import pytest

from chaptera_translation_core import ChapterInput, TranslationCore, TranslationRejected
from chaptera_translation_core.atom_transport import MAX_BATCH_SOURCE_CHARS


def respond(packet: str, *, translation=None, reverse=False, omit_last=False):
    """Mock a provider that follows the source-owned render-domain contract."""
    body = json.loads(packet)
    index = {col: pos for pos, col in enumerate(body['columns'])}
    rows = [{col: row[pos] for col, pos in index.items()} for row in body['rows']]
    by_domain = []
    for row in rows:
        if by_domain and by_domain[-1][0] == row['render_domain_id']:
            by_domain[-1][1].append(row)
        else:
            by_domain.append((row['render_domain_id'], [row]))
    if omit_last:
        by_domain = by_domain[:-1]
    segments = [
        {'covers': [a['atom_id'] for a in group],
         'translation': ''.join(a['source_text'] for a in group) if translation is None
             else translation(''.join(a['source_text'] for a in group))}
        for _, group in by_domain
    ]
    if reverse:
        segments.reverse()
    return json.dumps({'segments': segments}, ensure_ascii=False)


def russian(value):
    return value.replace('世界', 'Мир').replace('李明', 'Ли Мин').replace('兄弟', 'братья').replace('こんにちは', 'Привет')


class AtomProvider:
    def __init__(self, *, first_missing=False, never_repair=False, reorder=False):
        self.calls = []
        self.first_missing = first_missing
        self.never_repair = never_repair
        self.reorder = reorder

    async def generate(self, request):
        assert request.transport == 'atoms_v1'
        assert 'SOURCE ATOMS JSON:' in request.prompt
        assert 'Output ONLY one JSON' not in request.prompt
        self.calls.append(request)
        is_repair = 'TARGETED REPAIR' in request.prompt
        return respond(request.source_html_with_markers, translation=russian,
                       reverse=self.reorder,
                       omit_last=(self.first_missing and len(self.calls) == 1) or
                                 (self.never_repair and is_repair))


def translate(provider, source):
    return asyncio.run(TranslationCore(provider).translate(
        ChapterInput('chapter-12', source, transport='atoms_v1')))


def test_atom_json_preserves_dom_order_and_exact_provenance():
    provider = AtomProvider(reorder=True)
    result = translate(provider, '<body><p>李明看到了世界。</p><p>こんにちは，兄弟。</p></body>')
    assert result.translated_html == '<body><p>Ли Мин看到了Мир。</p><p>Привет，братья。</p></body>'
    assert result.unit_count == 2
    assert len(result.atom_provenance) == 2
    assert result.atom_provenance[0]['source_unit_id'] == 'chapter-12:000000'
    assert result.atom_provenance[1]['source_unit_id'] == 'chapter-12:000001'
    assert all(len(p['source_hash']) == 64 and len(p['target_hash']) == 64 for p in result.atom_provenance)
    assert 'source_text' not in result.atom_provenance[0]
    assert 'translation' not in provider.calls[0].source_html_with_markers


def test_repair_only_missing_domain_and_preserves_accepted_text():
    provider = AtomProvider(first_missing=True)
    output = translate(provider, '<body><p>世界。</p><p>兄弟。</p><p>こんにちは。</p></body>')
    assert output.translated_html == '<body><p>Мир。</p><p>братья。</p><p>Привет。</p></body>'
    assert len(provider.calls) == 2
    assert provider.calls[0].unit_count == 3
    assert provider.calls[1].unit_count == 1
    assert 'TARGETED REPAIR' in provider.calls[1].prompt


def test_failed_repair_never_publishes_partial_html():
    provider = AtomProvider(first_missing=True, never_repair=True)
    with pytest.raises(TranslationRejected, match='atom_coverage'):
        translate(provider, '<body><p>世界。</p><p>兄弟。</p></body>')
    assert len(provider.calls) == 3  # initial + two bounded repair rounds


def test_unknown_identity_cannot_be_silently_accepted():
    class BadProvider:
        async def generate(self, request):
            segments = json.loads(respond(request.source_html_with_markers))['segments']
            segments.append({'covers': ['999999'], 'translation': 'Потерянный текст'})
            return json.dumps({'segments': segments}, ensure_ascii=False)

    with pytest.raises(TranslationRejected, match='atom_identity'):
        translate(BadProvider(), '<p>世界。</p>')


def test_source_literal_markup_and_html_attributes_do_not_become_tags():
    class LiteralProvider(AtomProvider):
        async def generate(self, request):
            self.calls.append(request)
            return respond(request.source_html_with_markers, translation=lambda s: russian(s))

    source = '<p data-source="<strong>世界</strong>">世界 &lt;custom&gt; строго буквально。</p>'
    result = translate(LiteralProvider(), source)
    assert 'data-source="&lt;strong&gt;世界&lt;/strong&gt;"' in result.translated_html
    assert '&lt;custom&gt;' in result.translated_html
    assert '<custom>' not in result.translated_html


def test_reserved_marker_in_original_fails_closed_before_provider():
    provider = AtomProvider()
    with pytest.raises(TranslationRejected, match='reserved marker'):
        translate(provider, '<p>Точное слово TSEG_000001 встречается в оригинале.</p>')
    assert not provider.calls


def test_large_single_dom_node_splits_into_bounded_batches():
    source = '<p>' + ('世界。' * 17000) + '</p>'  # more than 50k source characters
    provider = AtomProvider()
    result = translate(provider, source)
    assert result.unit_count > 50
    assert len(provider.calls) > 5
    assert all(req.unit_count <= 24 for req in provider.calls)
    assert all(len(json.loads(req.source_html_with_markers)['rows']) <= 24 for req in provider.calls)
    assert 'Мир。' in result.translated_html
    assert '世界。' not in result.translated_html
    assert len(result.atom_provenance) == result.unit_count
