"""Desktop-origin Russian lexical and entity quality gates.

All inference, morphology and finding rules follow the verified source. Only
dependencies on context compilation and prepared chapter were adapted.
These findings are diagnostic-only in the server; a historical desktop
'blocking=True' label never automatically rejects an accepted chapter.
"""
# -*- coding: utf-8 -*-
import re
import unicodedata
from bs4 import BeautifulSoup, Comment
from .post_translation_context import compile_translation_context
from .glossary_enforcement import (
    ENFORCEMENT_FLEXIBLE,
    ENFORCEMENT_LEXICAL_FAMILY,
    ENFORCEMENT_STRICT,
    infer_enforcement,
)
_CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af\U00020000-\U0002EE5F\U0002F800-\U0002FA1F\U00030000-\U000323AF]')
_TOKEN_RE = re.compile(r'[0-9A-Za-zА-Яа-яЁё]+', re.UNICODE)
_CYRILLIC_TOKEN_RE = re.compile(r'^[а-яё]+$')
_AMBIGUOUS_CANON_RE = re.compile(r'[/|]')
_ENTITY_NOTE_MARKERS = ('персонаж', 'псевдоним', 'имя;', 'божество', 'type=person', 'type=alias')
_LOCATION_NOTE_MARKERS = ('локация', 'город', 'страна', 'регион', 'континент')
_IRREGULAR_RUSSIAN_FORMS = {
    'люди': {'люди', 'людей', 'людям', 'людьми', 'людях'},
    # Frequent fleeting-vowel noun in fantasy location names. Generic -ок handling would be unsafe
    # because урок->урока and исток->истока do not drop the vowel.
    'замок': {'замок', 'замка', 'замку', 'замком', 'замке', 'замки', 'замков', 'замкам', 'замками', 'замках'},
}
_LOCATION_COREFERENCE_TOKENS = {'там', 'туда', 'оттуда', 'здесь', 'сюда', 'отсюда'}
def _visible_text_nodes(html_content: str, *, source: bool = False) -> list[str]:
    # Reuses prepared source/target snapshots while preserving the legacy visible-node ordering exactly.
    if not html_content:
        return []
    if source:
        from .core.prepared_chapter import prepare_source_chapter
        return list(prepare_source_chapter(html_content).visible_text_nodes)
    from .core.prepared_chapter import prepare_target_snapshot
    return list(prepare_target_snapshot(html_content).visible_text_nodes)
def _normalize_token(token: str) -> str:
    # Нормализует регистр и ё/е для устойчивого сравнения русского lexical anchor.
    return unicodedata.normalize('NFKC', token).casefold().replace('ё', 'е')
def _russian_inflection_forms(expected: str, allow_plural: bool) -> set[str]:
    # Генерирует консервативный набор русских падежных/числовых форм без принятия произвольных lexical substitutions.
    expected = _normalize_token(expected)
    forms = {expected}
    forms.update(_IRREGULAR_RUSSIAN_FORMS.get(expected, ()))
    if len(expected) <= 2 or not _CYRILLIC_TOKEN_RE.fullmatch(expected):
        return forms
    if expected.endswith('ая'):
        stem = expected[:-2]
        forms.update(stem + suffix for suffix in ('ая', 'ой', 'ую', 'ою', 'ые', 'ых', 'ым', 'ыми'))
        return forms
    if expected.endswith('яя'):
        stem = expected[:-2]
        forms.update(stem + suffix for suffix in ('яя', 'ей', 'юю', 'ею', 'ие', 'их', 'им', 'ими'))
        return forms
    if expected.endswith(('ый', 'ой', 'ое')):
        stem = expected[:-2]
        forms.update(stem + suffix for suffix in ('ый', 'ой', 'ая', 'ое', 'ые', 'ого', 'ому', 'ым', 'ом', 'ую', 'ою', 'ых', 'ыми'))
        return forms
    if expected.endswith('ее'):
        stem = expected[:-2]
        forms.update(stem + suffix for suffix in ('ий', 'яя', 'ее', 'ие', 'его', 'ему', 'им', 'ем', 'юю', 'ею', 'их', 'ими'))
        return forms
    if expected.endswith('ий'):
        stem = expected[:-2]
        hard = expected.endswith(('кий', 'гий', 'хий'))
        forms.update(stem + suffix for suffix in (
            ('ий', 'ая', 'ое', 'ие', 'ого', 'ому', 'им', 'ом', 'ую', 'ою', 'ых', 'ыми')
            if hard else ('ий', 'яя', 'ее', 'ие', 'его', 'ему', 'им', 'ем', 'юю', 'ею', 'их', 'ими')
        ))
        # R15.1.8: -ий бывает не только окончанием прилагательного, но и существительного
        # (адамантий -> адамантия/адамантием). Добавляем только тот же длинный lexical stem.
        forms.update(stem + suffix for suffix in ('ий', 'ия', 'ию', 'ием', 'ии'))
        if allow_plural:
            forms.update(stem + suffix for suffix in ('ии', 'иев', 'иям', 'иями', 'иях'))
        return forms
    if expected.endswith('о'):
        stem = expected[:-1]
        forms.update(stem + suffix for suffix in ('о', 'а', 'у', 'ом', 'е'))
        if allow_plural:
            forms.update({stem, stem + 'а', stem + 'ам', stem + 'ами', stem + 'ах'})
        return forms
    if expected.endswith('е'):
        stem = expected[:-1]
        forms.update(stem + suffix for suffix in ('е', 'я', 'ю', 'ем', 'и'))
        if allow_plural:
            forms.update(stem + suffix for suffix in ('я', 'й', 'ям', 'ями', 'ях'))
        return forms
    if expected.endswith('ы'):
        stem = expected[:-1]
        forms.update(stem + suffix for suffix in ('ы', 'ов', 'ам', 'ами', 'ах'))
        return forms
    if expected.endswith('и'):
        stem = expected[:-1]
        forms.update(stem + suffix for suffix in ('и', 'ов', 'ев', 'ам', 'ями', 'ах'))
        return forms
    if expected.endswith(('й', 'ь')):
        stem = expected[:-1]
        forms.update(stem + suffix for suffix in ('я', 'ю', 'ем', 'е', 'и', 'ью'))
        if allow_plural:
            forms.update(stem + suffix for suffix in ('и', 'ев', 'ям', 'ями', 'ях'))
        return forms
    if expected.endswith('ец'):
        # Частая русская беглая гласная: дворец→дворца, конец→конца. Без этого
        # canonical multiword anchors вроде «Божественный Дворец» дают false TERM_REQUIRED_MISSING.
        stem = expected[:-2]
        forms.update(stem + suffix for suffix in ('ец', 'ца', 'цу', 'цом', 'це'))
        if allow_plural:
            forms.update(stem + suffix for suffix in ('цы', 'цов', 'цам', 'цами', 'цах'))
        return forms
    if expected.endswith('а'):
        stem = expected[:-1]
        forms.update(stem + suffix for suffix in ('ы', 'и', 'е', 'у', 'ой', 'ою'))
        if allow_plural:
            forms.update({stem, stem + 'ам', stem + 'ами', stem + 'ах'})
        return forms
    if expected.endswith('я'):
        stem = expected[:-1]
        forms.update(stem + suffix for suffix in ('и', 'е', 'ю', 'ей', 'ею'))
        if allow_plural:
            forms.update(stem + suffix for suffix in ('и', 'ь', 'ям', 'ями', 'ях'))
        return forms
    forms.update(expected + suffix for suffix in ('а', 'я', 'у', 'ю', 'ом', 'ем', 'е'))
    if allow_plural:
        forms.update(expected + suffix for suffix in ('ы', 'и', 'ов', 'ев', 'ам', 'ям', 'ами', 'ями', 'ах', 'ях'))
    return forms
def _token_matches_canonical(actual: str, expected: str, *, allow_plural: bool = True) -> bool:
    # Сравнивает lexical token с canonical base через ограниченный набор русских грамматических форм.
    return _normalize_token(actual) in _russian_inflection_forms(expected, allow_plural)
def _find_canonical_target_surface(translated_text: str, canonical_target: str, *, allow_plural: bool = True) -> str:
    # Возвращает фактическую русскую словоформу canonical lexical anchor, если она грамматически совместима с каноном.
    expected_tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(canonical_target)]
    actual_matches = list(_TOKEN_RE.finditer(translated_text))
    if not expected_tokens or len(actual_matches) < len(expected_tokens):
        return ''
    width = len(expected_tokens)
    for start in range(len(actual_matches) - width + 1):
        window = actual_matches[start:start + width]
        if all(
            _token_matches_canonical(_normalize_token(match.group(0)), expected, allow_plural=allow_plural)
            for match, expected in zip(window, expected_tokens)
        ):
            return ' '.join(match.group(0) for match in window)
    return ''
def _contains_canonical_target(translated_text: str, canonical_target: str, *, allow_plural: bool = True) -> bool:
    # Проверяет canonical lexical anchor через общий morphology-aware поиск фактической словоформы.
    return bool(_find_canonical_target_surface(translated_text, canonical_target, allow_plural=allow_plural))
def _canonical_core_tokens(canonical_target: str) -> list[str]:
    # Выбирает информативную часть multiword canonical anchor для доказуемого литературного short form.
    # Короткие родовые слова вроде «тело/мир/поле» сами по себе не должны скрывать реальную подмену термина.
    tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(canonical_target)]
    if len(tokens) <= 1:
        return tokens
    max_len = max((len(token) for token in tokens), default=0)
    cores = [token for token in tokens if len(token) >= 5 and len(token) >= max_len - 2]
    if cores:
        return cores
    return [max(tokens, key=len)] if tokens else []
def _find_canonical_short_surface(translated_text: str, canonical_target: str) -> str:
    # Возвращает грамматически совместимый core token canonical термина.
    # Используется только после того, как полный canonical уже установлен где-то в этой главе.
    core_tokens = _canonical_core_tokens(canonical_target)
    if not core_tokens:
        return ''
    for match in _TOKEN_RE.finditer(translated_text or ''):
        actual = _normalize_token(match.group(0))
        if any(_token_matches_canonical(actual, expected, allow_plural=True) for expected in core_tokens):
            return match.group(0)
    return ''
def _canonical_family_bases(expected: str) -> set[str]:
    # Builds a deliberately small derivational family used only by lexical_family bindings.
    expected = _normalize_token(expected)
    bases = {expected}
    if len(expected) < 5 or not _CYRILLIC_TOKEN_RE.fullmatch(expected):
        return bases
    if re.search(r'[бвгджзклмнпрстфхцчшщ]$', expected):
        bases.update({expected + 'ка', expected + 'ский', 'анти' + expected + 'ский', expected + 'овый'})
    if expected.endswith('имость'):
        stem = expected[:-6]
        if len(stem) >= 3:
            bases.add(stem + 'имый')
    if expected.endswith('ность'):
        stem = expected[:-5]
        if len(stem) >= 3:
            bases.add(stem + 'ный')
    if expected.endswith('ий') and len(expected) > 4:
        bases.add(expected[:-2] + 'иевый')
    return bases
def _find_canonical_family_surface(translated_text: str, canonical_target: str) -> str:
    # Accepts a narrow Russian derivational family for lexical-family contracts:
    # мутант→мутантка/антимутантский, вибраниум→вибраниумовый, невидимость→невидимый.
    expected_tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(canonical_target)]
    if len(expected_tokens) != 1:
        return ''
    expected = expected_tokens[0]
    family_forms: set[str] = set()
    for base in _canonical_family_bases(expected):
        family_forms.update(_russian_inflection_forms(base, True))
    for match in _TOKEN_RE.finditer(translated_text or ''):
        if _normalize_token(match.group(0)) in family_forms:
            return match.group(0)
    return ''
def _binding_enforcement(binding: dict) -> str:
    return infer_enforcement(
        str(binding.get('s') or ''),
        str(binding.get('t') or ''),
        str(binding.get('i') or ''),
        binding.get('provenance') if isinstance(binding.get('provenance'), dict) else {},
        explicit=str(binding.get('enforcement') or ''),
    )
def _find_binding_surface(translated_text: str, canonical_target: str, enforcement: str) -> str:
    surface = _find_canonical_target_surface(translated_text, canonical_target, allow_plural=True)
    if surface:
        return surface
    if enforcement == ENFORCEMENT_LEXICAL_FAMILY:
        return _find_canonical_family_surface(translated_text, canonical_target)
    return ''
def _contains_binding_surface(translated_text: str, canonical_target: str, enforcement: str) -> bool:
    return bool(_find_binding_surface(translated_text, canonical_target, enforcement))
def _find_adjacent_canonical_coreference_surface(
    target_slots: list[str], index: int, canonical_target: str, binding: dict,
) -> str:
    # Разрешает только непосредственную анафору после полного canonical в предыдущем target slot.
    # Это закрывает реальные случаи «очки страха» -> «очки» и «Лос-Анджелес» -> «там»,
    # не превращая глобальное наличие термина в индульгенцию для произвольных замен по всей главе.
    if index <= 0 or not _contains_canonical_target(target_slots[index - 1], canonical_target, allow_plural=True):
        return ''
    current = str(target_slots[index] or '')
    canonical_tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(canonical_target)]
    if len(canonical_tokens) > 1:
        head = canonical_tokens[0]
        for match in _TOKEN_RE.finditer(current):
            if _token_matches_canonical(match.group(0), head, allow_plural=True):
                return match.group(0)
    note = str(binding.get('i') or '').casefold()
    if any(marker in note for marker in _LOCATION_NOTE_MARKERS):
        for match in _TOKEN_RE.finditer(current):
            if _normalize_token(match.group(0)) in _LOCATION_COREFERENCE_TOKENS:
                return match.group(0)
    return ''
def _term_slot_evidence(
    target_slots: list[str], slot_indexes: list[int], canonical_target: str,
    observed_overrides: dict[int, str] | None = None,
) -> list[dict]:
    # Сохраняет компактное morphology/coreference evidence без полного текста главы.
    evidence = []
    core_tokens = _canonical_core_tokens(canonical_target)
    observed_overrides = observed_overrides or {}
    for index in slot_indexes[:8]:
        if not 0 <= index < len(target_slots):
            continue
        target_text = str(target_slots[index] or '')
        tokens = [match.group(0) for match in _TOKEN_RE.finditer(target_text)]
        evidence.append({
            'target_slot_id': f'TGT_{index:06d}',
            'target_length': len(target_text),
            'target_tokens_sample': tokens[:12],
            'canonical_core_tokens': core_tokens,
            'observed_core_surface': observed_overrides.get(index) or _find_canonical_short_surface(target_text, canonical_target) or None,
        })
    return evidence
def _is_entity_binding(binding: dict) -> bool:
    # Отличает person/name glossary entries от терминов, которые должны быть lexical anchors в каждом occurrence.
    note = str(binding.get('i') or '').casefold()
    return any(marker in note for marker in _ENTITY_NOTE_MARKERS)
def _is_single_cjk_entity_source(value: str) -> bool:
    # Односимвольное CJK-имя лексически неоднозначно: тот же иероглиф может быть обычной морфемой внутри слова/фразы.
    normalized = unicodedata.normalize('NFKC', str(value or '').strip())
    return len(normalized) == 1 and bool(_CJK_RE.fullmatch(normalized))
def _short_cjk_entity_is_lexically_ambiguous(source_text: str, original: str) -> bool:
    # Считает односимвольную entity недоказанной, если каждое её occurrence примыкает к CJK-тексту.
    # Изолированное `王`, `「王」`, `王:` остаётся строгим; `间谍之王`/`王说道` — только contextual evidence.
    normalized_text = unicodedata.normalize('NFKC', str(source_text or ''))
    normalized_original = unicodedata.normalize('NFKC', str(original or '').strip())
    if not _is_single_cjk_entity_source(normalized_original):
        return False
    found = False
    for match in re.finditer(re.escape(normalized_original), normalized_text):
        found = True
        left_cjk = match.start() > 0 and bool(_CJK_RE.fullmatch(normalized_text[match.start() - 1]))
        right_cjk = match.end() < len(normalized_text) and bool(_CJK_RE.fullmatch(normalized_text[match.end()]))
        if not left_cjk and not right_cjk:
            return False
    return found
def _binding_has_unambiguous_source_evidence(source_text: str, binding: dict) -> bool:
    # Фильтрует substring-only evidence коротких CJK entity, чтобы обычная морфема не считалась вторым персонажем в slot.
    original = str(binding.get('s') or '')
    return bool(original and original in source_text and not _short_cjk_entity_is_lexically_ambiguous(source_text, original))
def _find_entity_note_alias_surface(target_text: str, canonical_target: str, binding: dict) -> str:
    # Semantic-preflight person notes sometimes contain an explicit alternate name/epithet
    # (e.g. `type=person; Красный Дьявол`). If that alternate lexical name is emitted instead
    # of the project canonical, it is not pronoun/coreference and must be repaired.
    note = str(binding.get('i') or '')
    for raw_part in note.split(';'):
        candidate = raw_part.strip()
        if not candidate or candidate.casefold().startswith('type='):
            continue
        if candidate.casefold() == str(canonical_target or '').strip().casefold():
            continue
        tokens = _TOKEN_RE.findall(candidate)
        if not 1 <= len(tokens) <= 4:
            continue
        if not all(re.fullmatch(r'[А-ЯЁ][А-Яа-яЁё-]*', token) for token in tokens):
            continue
        surface = _find_canonical_target_surface(target_text, candidate, allow_plural=False)
        if surface:
            return surface
    return ''
def _find_entity_substitution(source_text: str, target_text: str, expected_binding: dict, entity_catalog: list[dict]) -> dict | None:
    # Ищет canonical имя другого project-local персонажа только когда source slot содержит одну доказанную ожидаемую entity.
    source_entities = [binding for binding in entity_catalog if _binding_has_unambiguous_source_evidence(source_text, binding)]
    if len(source_entities) != 1 or source_entities[0].get('s') != expected_binding.get('s'):
        return None
    candidates = sorted(
        (binding for binding in entity_catalog if binding.get('s') != expected_binding.get('s')),
        key=lambda binding: len(str(binding.get('t') or '')),
        reverse=True,
    )
    for binding in candidates:
        target = str(binding.get('t') or '')
        if target and _contains_canonical_target(target_text, target, allow_plural=False):
            return binding
    return None
def run_translation_gates(
    source_html: str,
    translated_html: str,
    context_manager=None,
    current_chapters_list=None,
    *,
    alignment_proven: bool | None = None,
) -> list[dict]:
    # Проверяет project-local terminology, morphology и entity substitutions; cross-project advisory glossary не блокирует перевод.
    source_slots = _visible_text_nodes(source_html, source=True)
    from .core.prepared_chapter import prepare_target_snapshot
    target_snapshot = prepare_target_snapshot(translated_html)
    target_slots = list(target_snapshot.visible_text_nodes)
    translated_text = target_snapshot.combined_text
    # Legacy callers without coverage provenance retain the previous DOM-count heuristic.
    # Coverage-v2 callers can now explicitly require machine-proven source-owned segmentation.
    slots_aligned = bool(source_slots) and len(source_slots) == len(target_slots) and alignment_proven is not False
    findings = []
    if context_manager is not None:
        compiled = compile_translation_context(context_manager, source_html, current_chapters_list)
        entity_catalog = compiled.get('entity_catalog', [])
        if alignment_proven is False and compiled.get('required'):
            findings.append({
                'code': 'ALIGNMENT_UNPROVEN_TERM_QA', 'severity': 'warning', 'blocking': False,
                'message': 'Позиционный terminology QA не блокирует перевод: source→target alignment не доказан machine-owned coverage contract.',
                'evidence': {'required_terms': len(compiled.get('required') or ())},
            })
        exact_source_slots_by_term = {}
        regex_service = getattr(context_manager, 'regex_service', None)
        span_matcher = getattr(regex_service, 'find_exact_matches_with_spans_in_nodes', None)
        if slots_aligned and callable(span_matcher):
            for node_index, _start, _end, original in span_matcher(source_slots):
                if 0 <= int(node_index) < len(source_slots):
                    exact_source_slots_by_term.setdefault(str(original), []).append(int(node_index))
        for binding in compiled['required']:
            original = binding['s']
            target = binding['t']
            enforcement = _binding_enforcement(binding)
            if _AMBIGUOUS_CANON_RE.search(target):
                findings.append({
                    'code': 'TERM_AMBIGUOUS_CANON', 'severity': 'warning', 'blocking': False,
                    'message': f'Нельзя строго проверить неоднозначный canonical target для {original!r}: {target!r}.',
                    'evidence': {'source': original, 'target': target, 'enforcement': enforcement},
                })
                continue
            if slots_aligned and exact_source_slots_by_term:
                source_slot_indexes = list(dict.fromkeys(exact_source_slots_by_term.get(original, [])))
            else:
                source_slot_indexes = [index for index, source_slot in enumerate(source_slots) if original in source_slot] if slots_aligned else []
            if _is_entity_binding(binding):
                canonical_entity_established = _contains_canonical_target(translated_text, target, allow_plural=False)
                ambiguous_short_slots = [
                    index for index in source_slot_indexes
                    if _short_cjk_entity_is_lexically_ambiguous(source_slots[index], original)
                ]
                substitutions = []
                alias_drifts = []
                missing_entity_slots = []
                for index in source_slot_indexes:
                    if index in ambiguous_short_slots:
                        continue
                    if _contains_canonical_target(target_slots[index], target, allow_plural=False):
                        continue
                    substitution = _find_entity_substitution(source_slots[index], target_slots[index], binding, entity_catalog)
                    if substitution:
                        substitutions.append((index, substitution))
                        continue
                    observed_alias = _find_entity_note_alias_surface(target_slots[index], target, binding)
                    if observed_alias:
                        alias_drifts.append((index, observed_alias))
                        continue
                    missing_entity_slots.append(index)
                if substitutions:
                    evidence = {
                        'source': original, 'target': target, 'enforcement': enforcement,
                        'target_slot_ids': [f'TGT_{index:06d}' for index, _ in substitutions],
                        'substitutions': [
                            {'source': replacement['s'], 'target': replacement['t']}
                            for _, replacement in substitutions
                        ],
                    }
                    findings.append({
                        'code': 'TERM_ENTITY_SUBSTITUTION', 'severity': 'high', 'blocking': True,
                        'message': f'Вместо entity {original!r} → {target!r} найден canonical target другого персонажа.',
                        'evidence': evidence,
                    })
                if alias_drifts:
                    findings.append({
                        'code': 'TERM_ENTITY_ALIAS_DRIFT', 'severity': 'high', 'blocking': True,
                        'message': f'Для entity {original!r} использовано явное альтернативное имя вместо canonical {target!r}.',
                        'evidence': {
                            'source': original, 'target': target, 'enforcement': enforcement,
                            'target_slot_ids': [f'TGT_{index:06d}' for index, _ in alias_drifts],
                            'observed_aliases': [surface for _, surface in alias_drifts],
                        },
                    })
                if (missing_entity_slots and not canonical_entity_established) or ambiguous_short_slots:
                    ambiguous_slot_ids = [f'TGT_{index:06d}' for index in ambiguous_short_slots]
                    lexical_ambiguity = bool(ambiguous_slot_ids)
                    findings.append({
                        'code': 'TERM_ENTITY_UNVERIFIED', 'severity': 'warning', 'blocking': False,
                        'message': (
                            f'Односимвольная CJK entity {original!r} → {target!r} встретилась в лексически неоднозначном контексте; '
                            'строгая подмена персонажа не доказана.'
                            if lexical_ambiguity else
                            f'Для entity {original!r} не найдено имя {target!r}; возможна корректная местоименная/coreference замена.'
                        ),
                        'evidence': {
                            'source': original, 'target': target, 'enforcement': enforcement,
                            'target_slot_ids': [f'TGT_{index:06d}' for index in missing_entity_slots],
                            **({'reason': 'short_cjk_entity_ambiguous_context', 'ambiguous_target_slot_ids': ambiguous_slot_ids} if lexical_ambiguity else {}),
                        },
                    })
                continue
            if enforcement == ENFORCEMENT_FLEXIBLE:
                # Flexible bindings are consistency hints, not per-occurrence hard contracts.
                continue
            missing_slot_indexes = [
                index for index in source_slot_indexes
                if not _contains_binding_surface(target_slots[index], target, enforcement)
            ]
            canonical_established = _contains_binding_surface(translated_text, target, enforcement)
            short_form_slots = []
            short_form_surfaces: dict[int, str] = {}
            if canonical_established:
                target_token_count = len(_TOKEN_RE.findall(target))
                for index in missing_slot_indexes:
                    observed = ''
                    if target_token_count > 1:
                        observed = _find_canonical_short_surface(target_slots[index], target)
                    # Legacy family/coreference relaxation remains allowed after the canonical was established.
                    if not observed and target_token_count == 1:
                        observed = _find_canonical_family_surface(target_slots[index], target)
                    if not observed:
                        observed = _find_adjacent_canonical_coreference_surface(target_slots, index, target, binding)
                    if observed:
                        short_form_slots.append(index)
                        short_form_surfaces[index] = observed
                if short_form_slots:
                    missing_slot_indexes = [index for index in missing_slot_indexes if index not in set(short_form_slots)]
                    findings.append({
                        'code': 'TERM_CANONICAL_SHORT_FORM', 'severity': 'info', 'blocking': False,
                        'message': (
                            f'Для source term {original!r} использована доказуемая литературная словоформа/анафора после того, '
                            f'как canonical anchor {target!r} уже установлен в главе.'
                        ),
                        'evidence': {
                            'source': original, 'target': target, 'enforcement': enforcement,
                            'target_slot_ids': [f'TGT_{index:06d}' for index in short_form_slots],
                            'slot_evidence': _term_slot_evidence(target_slots, short_form_slots, target, short_form_surfaces),
                        },
                    })
            missing = bool(missing_slot_indexes) if source_slot_indexes else not _contains_binding_surface(translated_text, target, enforcement)
            if missing:
                evidence = {'source': original, 'target': target, 'enforcement': enforcement}
                if missing_slot_indexes:
                    evidence['target_slot_ids'] = [f'TGT_{index:06d}' for index in missing_slot_indexes]
                    evidence['slot_evidence'] = _term_slot_evidence(target_slots, missing_slot_indexes, target)
                # Only exact slot-addressable strict/family violations trigger automatic deterministic repair.
                blocking = bool(missing_slot_indexes) and enforcement in {ENFORCEMENT_STRICT, ENFORCEMENT_LEXICAL_FAMILY}
                findings.append({
                    'code': 'TERM_REQUIRED_MISSING', 'severity': 'high', 'blocking': blocking,
                    'message': f'Для source term {original!r} не найден canonical lexical anchor {target!r}.',
                    'evidence': evidence,
                })
    residue = list(target_snapshot.cjk_residue)
    if residue:
        findings.append({
            'code': 'CJK_RESIDUE', 'severity': 'high', 'blocking': False,
            'message': f'В русском visible text остались CJK-символы: {"".join(residue[:12])!r}.',
            'evidence': {'sample': ''.join(residue[:24]), 'count': len(residue)},
        })
    return findings
def localize_blocking_gate_findings(source_html: str, translated_html: str, findings: list[dict]) -> list[dict]:
    # Локализует только доказуемые CJK/terminology blockers до exact target slots при one-to-one DOM alignment.
    blockers = [finding for finding in findings if finding.get('blocking')]
    if not blockers:
        return []
    source_slots = _visible_text_nodes(source_html, source=True)
    target_slots = _visible_text_nodes(translated_html)
    if not source_slots or len(source_slots) != len(target_slots):
        return []
    localized = []
    blocker_codes = {finding.get('code') for finding in blockers}
    if 'CJK_RESIDUE' in blocker_codes:
        for index, (source_text, target_text) in enumerate(zip(source_slots, target_slots)):
            residue = _CJK_RE.findall(target_text)
            if residue:
                localized.append({
                    'code': 'CJK_RESIDUE', 'severity': 'high', 'blocking': True,
                    'message': f'В target slot остались CJK-символы: {"".join(residue[:12])!r}.',
                    'evidence': {
                        'target_slot_id': f'TGT_{index:06d}', 'source_excerpt': source_text,
                        'translation_excerpt': target_text, 'sample': ''.join(residue[:24]),
                    },
                })
    for finding in blockers:
        if finding.get('code') in {'TERM_ENTITY_SUBSTITUTION', 'TERM_ENTITY_ALIAS_DRIFT'}:
            evidence = finding.get('evidence') or {}
            code = str(finding.get('code') or '')
            for slot_id in evidence.get('target_slot_ids') or []:
                try:
                    index = int(str(slot_id).removeprefix('TGT_'))
                except (TypeError, ValueError):
                    continue
                if not 0 <= index < len(source_slots):
                    continue
                localized.append({
                    'code': code, 'severity': 'high', 'blocking': True,
                    'message': finding.get('message') or 'Обнаружена подмена canonical entity.',
                    'evidence': {
                        **evidence, 'target_slot_id': f'TGT_{index:06d}',
                        'source_excerpt': source_slots[index], 'translation_excerpt': target_slots[index],
                    },
                })
            continue
        if finding.get('code') != 'TERM_REQUIRED_MISSING':
            continue
        evidence = finding.get('evidence') or {}
        original = evidence.get('source')
        target = evidence.get('target')
        enforcement = str(evidence.get('enforcement') or ENFORCEMENT_STRICT)
        if not original or not target:
            continue
        for index, (source_text, target_text) in enumerate(zip(source_slots, target_slots)):
            if original not in source_text or _contains_binding_surface(target_text, target, enforcement):
                continue
            localized.append({
                'code': 'TERM_REQUIRED_MISSING', 'severity': 'high', 'blocking': True,
                'message': f'В target slot отсутствует canonical lexical anchor {target!r} для {original!r}.',
                'evidence': {
                    'target_slot_id': f'TGT_{index:06d}', 'source_excerpt': source_text,
                    'translation_excerpt': target_text, 'source': original, 'target': target, 'enforcement': enforcement,
                },
            })
    return localized
def blocking_gate_message(findings: list[dict]) -> str | None:
    # Собирает компактное диагностичное сообщение только из blocking findings.
    blocking = [finding for finding in findings if finding.get('blocking')]
    if not blocking:
        return None
    return '; '.join(f"{finding['code']}: {finding['message']}" for finding in blocking[:6])
