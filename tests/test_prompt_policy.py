"""Golden cases for the new server prompt policy using frozen desktop semantics.

Provider calls are simulated. No Gemini calls, PyQt imports or desktop DB involved.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from chaptera_translation_core import (
    ChapterInput, TranslationCore, make_snapshot, glossary_fingerprint, sha256_text,
)
from chaptera_translation_core.prompt_policy import (
    compose_prompt, filter_literary_style, scoped_glossary, select_source_bindings,
)
from chaptera_translation_core.core.worker_helpers.translation_completeness import strip_source_boilerplate
from chaptera_translation_core.chapter_context import (
    context_is_current, validate_context_response, bounded_translation_tail,
)


CHAPTER_PATH = 'OEBPS/Text/chapter_0517.xhtml'
SOURCE = '<body><p>李明 пришёл в город. 王雪 ждала его.</p></body>'
GLOSSARY = {
    '李明': {'rus': 'Ли Мин', 'authority': 'required', 'note': 'персонаж, мужчина'},
    '王雪': {'rus': 'Ван Сюэ', 'authority': 'advisory', 'scope': 'global', 'note': 'персонаж'},
    '不存在': {'rus': 'Не существует'},
}


class RecordingProvider:
    def __init__(self):
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return request.source_html_with_markers.replace('李明', 'Ли Мин').replace('王雪', 'Ван Сюэ')


def _snapshot(glossary=GLOSSARY, source=SOURCE, predecessor=None, **overrides):
    glossary_hash = glossary_fingerprint(glossary)
    source_hash = sha256_text(strip_source_boilerplate(source))
    return make_snapshot(
        chapter_path=CHAPTER_PATH, source_hash=source_hash,
        glossary_hash=glossary_hash, predecessor=predecessor,
        context={
            'summary': 'Ли Мин пришёл в город.',
            'active_entities': [{'source_name': '李明', 'gender_or_grammar': 'female'}],
            **overrides,
        },
    )


def test_exact_visible_text_glossary_filter_and_scoped_versions():
    glossary = scoped_glossary(GLOSSARY, {
        '李明': [{'scope': [CHAPTER_PATH], 'override': {'rus': 'Ли Минь'}}],
    }, CHAPTER_PATH)
    assert glossary['李明']['rus'] == 'Ли Минь'
    assert scoped_glossary(GLOSSARY, {
        '李明': [{'scope': [CHAPTER_PATH], 'override': {'rus': 'Ли Минь'}}],
    }, 'OEBPS/Text/chapter_0518.xhtml')['李明']['rus'] == 'Ли Мин'
    source = '<body><p title="不存在">李明 и 王雪</p></body>'
    groups = select_source_bindings(source, glossary)
    assert [x['s'] for x in groups['required']] == ['李明']
    assert [x['s'] for x in groups['advisory']] == ['王雪']
    assert '不存在' not in json.dumps(groups, ensure_ascii=False)
    assert groups['required'][0]['enforcement'] == 'strict'


def test_no_glossary_authority_suppresses_all_lexical_contracts():
    assert scoped_glossary(GLOSSARY, {}, CHAPTER_PATH, 'UNAVAILABLE') == {}
    chapter = ChapterInput('517', SOURCE, glossary=GLOSSARY, glossary_authority='UNAVAILABLE', chapter_path=CHAPTER_PATH)
    prompt, info = compose_prompt(chapter, SOURCE, SOURCE)
    assert 'REQUIRED_BINDINGS' not in prompt
    assert info.matched_required == 0


def test_conflicting_versions_for_same_chapter_fail_closed():
    versions = {'李明': [
        {'scope': [CHAPTER_PATH], 'override': {'rus': 'Ли Минь'}},
        {'scope': [CHAPTER_PATH], 'override': {'rus': 'Минг Ли'}},
    ]}
    with pytest.raises(ValueError, match='Conflicting glossary versions'):
        scoped_glossary(GLOSSARY, versions, CHAPTER_PATH)


def test_current_snapshot_is_used_and_gender_disagreement_is_flagged():
    provider = RecordingProvider()
    snapshot = _snapshot()
    events = []
    chapter = ChapterInput('517', SOURCE, glossary=GLOSSARY, chapter_path=CHAPTER_PATH,
                           chapter_context_snapshot=snapshot)
    result = asyncio.run(TranslationCore(provider, on_event=events.append).translate(chapter))
    assert 'Ли Мин' in result.translated_html
    prompt = provider.requests[0].prompt
    assert 'SOURCE-DERIVED CHAPTER CONTEXT' in prompt
    assert 'UNRESOLVED GLOSSARY / CHAPTER-CONTEXT CONFLICTS' in prompt
    assert '"t": "Ли Мин"' in prompt
    assert any(e.stage == 'context_conflict' for e in events)


def test_stale_source_or_glossary_snapshot_is_silently_excluded_and_reported():
    provider = RecordingProvider()
    snapshot = _snapshot()
    chapter = ChapterInput('517', SOURCE + '<p>Новая строка.</p>', glossary=GLOSSARY,
                           chapter_path=CHAPTER_PATH, chapter_context_snapshot=snapshot)
    events = []
    asyncio.run(TranslationCore(provider, on_event=events.append).translate(chapter))
    assert 'SOURCE-DERIVED CHAPTER CONTEXT' not in provider.requests[0].prompt
    assert [event.stage for event in events].count('context_stale') == 1
    assert not context_is_current(snapshot, source_hash='changed', glossary_hash=glossary_fingerprint(GLOSSARY))

    # Changing a scoped lexical binding invalidates the old snapshot too.
    new_glossary = {**GLOSSARY, '李明': {'rus': 'Минг Ли'}}
    chapter2 = ChapterInput('517', SOURCE, glossary=new_glossary, chapter_path=CHAPTER_PATH,
                            chapter_context_snapshot=snapshot)
    prompt2, info2 = compose_prompt(chapter2, SOURCE, strip_source_boilerplate(SOURCE))
    assert info2.context_status == 'stale_ignored'
    assert 'Герой' not in prompt2


def test_predecessor_dependency_is_immutable_and_bounded():
    predecessor = {'chapter_path': 'OEBPS/Text/chapter_0516.xhtml',
                   'source_hash': 'old', 'translation_tail_hash': 'tail1',
                   'translation_tail': 'Ранее было сказано: «Ли Мин». ' * 180}
    predecessor['translation_tail'] = bounded_translation_tail(predecessor['translation_tail'])
    snapshot = _snapshot(predecessor=predecessor)
    chapter = ChapterInput('517', SOURCE, glossary=GLOSSARY, chapter_path=CHAPTER_PATH,
                           predecessor=predecessor, chapter_context_snapshot=snapshot)
    prompt, info = compose_prompt(chapter, SOURCE, strip_source_boilerplate(SOURCE))
    assert info.context_accepted
    assert 'translation_tail' in prompt
    assert len(predecessor['translation_tail']) <= 2400
    # A changed predecessor translation must not reuse an old approved context.
    changed = {**predecessor, 'translation_tail_hash': 'tail2'}
    prompt2, info2 = compose_prompt(
        ChapterInput('517', SOURCE, glossary=GLOSSARY, chapter_path=CHAPTER_PATH,
                     predecessor=changed, chapter_context_snapshot=snapshot), SOURCE,
        strip_source_boilerplate(SOURCE),
    )
    assert not info2.context_accepted
    assert 'SOURCE-DERIVED CHAPTER CONTEXT' not in prompt2


def test_desktop_style_filter_preserves_literary_instructions_but_not_machine_contracts():
    requested = '\n'.join([
        'Сохраняй сдержанный тон повествования.',
        'OUTPUT CONTRACT: reply as Markdown JSON.',
        'Keep exact TSEG markers.',
        'Use natural conversational Russian.',
        '{glossary}',
    ])
    clean = filter_literary_style(requested)
    assert 'Сохраняй сдержанный тон' in clean
    assert 'natural conversational' in clean
    assert 'OUTPUT CONTRACT' not in clean
    assert 'TSEG' not in clean
    assert '{glossary}' not in clean
    prompt, _ = compose_prompt(ChapterInput('517', SOURCE, style_instructions=requested), SOURCE, SOURCE)
    assert 'LITERARY STYLE GUIDANCE' in prompt
    assert 'Output ONLY translated HTML' in prompt


def test_context_response_parser_preserves_exact_id_and_rejects_invented_terms():
    documents = [{'id': 'c517', 'chapter_path': CHAPTER_PATH, 'source_hash': 'hash',
                  'text': '李明 увидел 王雪.'}]
    response = {'chapter_contexts': {'c517': {
        'summary': 'Ли Мин увидел Ван Сюэ.',
        'semantic_terms': [
            {'source': '李明', 'suggested_ru': 'Ли Мин', 'kind': 'person'},
            {'source': '不存在', 'suggested_ru': 'Вымышленный'},
        ],
    }}}
    accepted, missing = validate_context_response(response, documents)
    assert not missing
    assert [x['source'] for x in accepted[0]['semantic_terms']] == ['李明']


def test_conflicting_glossary_versions_are_permanent_not_retryable(tmp_path):
    from chaptera_server.storage import QueueStore, JobSpec
    from chaptera_server.worker import TranslationWorker

    versions = {'李明': [
        {'scope': [CHAPTER_PATH], 'override': {'rus': 'Ли Мин'}},
        {'scope': [CHAPTER_PATH], 'override': {'rus': 'Ли Минь'}},
    ]}
    store = QueueStore(tmp_path / 'jobs.sqlite3')
    job = store.create_job('alice', JobSpec(
        '517', SOURCE, glossary=GLOSSARY, chapter_path=CHAPTER_PATH,
        glossary_versions=versions,
    ))
    provider = RecordingProvider()
    assert asyncio.run(TranslationWorker(store, provider).run_once())
    saved = store.get_job(job['id'])
    assert saved['status'] == 'failed'
    assert saved['attempts'] == 1
    assert not provider.requests
    assert store.claim_next() is None
