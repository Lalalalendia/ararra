import copy
import hashlib
import re
from collections import Counter
from statistics import median
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from chaptera_translation_core.utils.source_units import MAX_PROVIDER_TRANSLATION_UNITS, MAX_SOURCE_UNIT_CHARS, TRANSLATABLE_SOURCE_UNIT_KINDS, classify_source_text_nodes, get_source_document_title, is_source_boilerplate_text, split_source_text_node, strip_nontranslatable_source_units
TRANSLATION_UNIT_PREFIX = 'TSEG_'
_PUBLICATION_PARAGRAPH_RE = re.compile(r'(<p\b[^>]*>)(.*?)(</p>)', re.IGNORECASE | re.DOTALL)
_PUBLICATION_LOWERCASE_START_RE = re.compile(r'^[\s«„"“‹‘(\[]*[а-яёa-z]')
_PUBLICATION_QUOTED_PERIOD_RE = re.compile(
    r'\.(?=\s*»(?:(?:\s*)|(?:</(?:span|em|strong|b|i|a|u|s|code)>))*\s*$)',
    re.IGNORECASE,
)
_PUBLICATION_TAG_RE = re.compile(r'<[^>]+>')
def _publication_visible_text(fragment: str) -> str:
    return re.sub(r'\s+', ' ', _PUBLICATION_TAG_RE.sub('', fragment or '')).strip()
def _repair_impossible_quoted_period_boundaries(html_content: str) -> str:
    # R15.1.9: финальный fail-safe для синтаксически невозможной русской границы вида
    # «термин.» </p><p>продолжение... . Этот паттерн не требует source alignment:
    # точка перед закрывающей русской кавычкой + новый абзац со строчной буквы не может
    # быть корректной самостоятельной sentence boundary. Работает на publication gate,
    # поэтому закрывает также пути, где optional literary prettify отключён/обойдён.
    if not html_content:
        return html_content
    matches = list(_PUBLICATION_PARAGRAPH_RE.finditer(html_content))
    if len(matches) < 2:
        return html_content
    merge_after = set()
    repaired_inner = {}
    for index in range(len(matches) - 1):
        current = matches[index]
        nxt = matches[index + 1]
        if html_content[current.end():nxt.start()].strip():
            continue
        current_visible = _publication_visible_text(current.group(2))
        next_visible = _publication_visible_text(nxt.group(2))
        if not current_visible or not next_visible:
            continue
        if not re.search(r'«[^»\n]{1,200}\.»\s*$', current_visible):
            continue
        if not _PUBLICATION_LOWERCASE_START_RE.search(next_visible):
            continue
        inner = current.group(2)
        fixed = _PUBLICATION_QUOTED_PERIOD_RE.sub('', inner, count=1)
        if fixed == inner:
            continue
        merge_after.add(index)
        repaired_inner[index] = fixed
    if not merge_after:
        return html_content
    out = []
    cursor = 0
    index = 0
    while index < len(matches):
        first = matches[index]
        out.append(html_content[cursor:first.start()])
        open_tag = first.group(1)
        inner = repaired_inner.get(index, first.group(2))
        last = first
        while index in merge_after and index + 1 < len(matches):
            nxt = matches[index + 1]
            if html_content[last.end():nxt.start()].strip():
                break
            inner = inner.rstrip() + ' ' + nxt.group(2).lstrip()
            index += 1
            last = nxt
        out.append(open_tag + inner + last.group(3))
        cursor = last.end()
        index += 1
    out.append(html_content[cursor:])
    return ''.join(out)
TRANSLATION_UNIT_RE = re.compile(r'<!--\s*TSEG_(\d{6})\s*-->')
SOURCE_HASH_RE = re.compile(r'<!--\s*TSRC_([0-9a-fA-F]{12})\s*-->')
_LENIENT_TRANSLATION_UNIT_RE = re.compile(r'(?:<!--\s*)?TSEG_(\d{1,12})(?:\s*-->)?', re.IGNORECASE)
_LENIENT_SOURCE_HASH_RE = re.compile(r'(?:<!--\s*)?TSRC_([0-9a-fA-F]{12})(?:\s*-->)?', re.IGNORECASE)
_ADJACENT_SOURCE_HASH_RE = re.compile(r'^\s*(?:<!--\s*)?TSRC_([0-9a-fA-F]{12})(?:\s*-->)?', re.IGNORECASE)
_ANY_TRANSLATION_UNIT_RE = re.compile(r'(?<![\w-])(?:<!--\s*)?(?:TSEG_\d{1,12}|TSRC_[0-9a-fA-F]{6,64})(?:\s*-->)?', re.IGNORECASE)
_SOURCE_MEANINGFUL_RE = re.compile(r'[0-9A-Za-zА-Яа-яЁё\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af\U00020000-\U0002FA1F]')
_TARGET_MEANINGFUL_RE = re.compile(r'[0-9A-Za-zА-Яа-яЁё]')
MAX_TRANSLATION_UNIT_CHARS = MAX_SOURCE_UNIT_CHARS
MAX_CONTINUATION_TRANSLATION_UNITS = 16
MAX_LOCAL_REPAIR_SOURCE_CHARS = 3600
MAX_LOCAL_REPAIR_WINDOWS = 3
MAX_LOCAL_REPAIR_SOURCE_RATIO = 0.30
LOCAL_REPAIR_RATIO_MIN_UNITS = 20
NEIGHBOR_COVERAGE_RATIO_MULTIPLIER = 1.75
MIN_TOTAL_RATIO_SOURCE_CHARS = 500
MIN_TOTAL_TRANSLATION_RATIO = 0.35
MIN_UNIT_RATIO_SOURCE_CHARS = 160
MIN_UNIT_TRANSLATION_RATIO = 0.15
MIN_CHUNK_SLOT_RATIO_SOURCE_CHARS = 40
MIN_CHUNK_SLOT_TRANSLATION_RATIO = 0.25
def _split_text_node(text: str, max_chars: int = MAX_TRANSLATION_UNIT_CHARS) -> list[str]:
    # Делегирует runtime TSEG-разбиение canonical source-unit splitter-у.
    return split_source_text_node(text, max_chars=max_chars)
def strip_source_boilerplate(html_content: str, current_chapter_path: str | None = None, current_chapter_title: str | None = None, next_chapter_title: str | None = None) -> str:
    # Применяет единую source policy до TSEG-разметки, сохраняя raw source snapshot отдельно.
    return strip_nontranslatable_source_units(
        html_content,
        current_chapter_path=current_chapter_path,
        current_chapter_title=current_chapter_title,
        next_chapter_title=next_chapter_title,
    )
def _short_source_hash(text: str) -> str:
    # Возвращает короткий collision-resistant hash exact source piece для Segment Envelope v2.
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]
def build_translation_unit_source_bundle(html_content: str) -> tuple[str, list[dict]]:
    # Marks one source chunk and builds its deterministic provider atom payload from the same BeautifulSoup tree.
    if not html_content:
        return html_content, []
    from chaptera_translation_core.utils.preparation_metrics import record_preparation_event, timed_preparation_event
    with timed_preparation_event('provider_marker_dom_build'):
        soup = BeautifulSoup(html_content, 'html.parser')
    record_preparation_event('beautifulsoup_parse_provider')
    unit_index = 0
    domain_index = 0
    provider_units = []
    text_nodes = list(soup.find_all(string=True))
    for node in text_nodes:
        if isinstance(node, Comment) or not str(node).strip():
            continue
        if node.find_parent(['script', 'style']):
            continue
        pieces = _split_text_node(str(node))
        if any(piece.strip() for piece in pieces):
            domain_index += 1
        render_domain_id = f'{getattr(node.parent, "name", None) or "root"}:{domain_index:06d}'
        replacement = []
        for piece in pieces:
            if piece.strip():
                unit_id = f'{unit_index:06d}'
                replacement.append(Comment(f' {TRANSLATION_UNIT_PREFIX}{unit_id} '))
                replacement.append(Comment(f' TSRC_{_short_source_hash(piece)} '))
                provider_units.append({
                    'unit_id': unit_id,
                    'source_text': piece,
                    'render_domain_id': render_domain_id,
                })
                unit_index += 1
            replacement.append(NavigableString(piece))
        node.replace_with(*replacement)
    return str(soup), provider_units
def add_translation_unit_markers(html_content: str) -> str:
    # Preserves the legacy marker API while delegating to the unified one-pass provider source preparation.
    marked_source, _ = build_translation_unit_source_bundle(html_content)
    return marked_source
def count_translation_units(html_content: str) -> int:
    # Считает стабильные TSEG-единицы, которые потребуются этому HTML в одном API-запросе.
    if not html_content:
        return 0
    return len(TRANSLATION_UNIT_RE.findall(add_translation_unit_markers(html_content)))
def build_translation_unit_payload(marked_source: str) -> list[dict]:
    # Возвращает ordered source atoms и группирует render domain только внутри одного исходного text-node split run.
    expected_order, _ = _marker_segments(marked_source)
    source_segments = _exact_marked_source_segments(marked_source)
    if not expected_order or source_segments is None or set(source_segments) != set(expected_order):
        return []
    soup = BeautifulSoup(marked_source or '', 'html.parser')
    render_domains = {}
    domain_index = 0
    last_parent = None
    last_source_node = None
    for node in soup.find_all(string=True):
        if not isinstance(node, Comment):
            continue
        match = re.fullmatch(r'\s*TSEG_(\d{6})\s*', str(node))
        if not match:
            continue
        target = node.next_sibling
        if isinstance(target, Comment) and re.fullmatch(r'\s*TSRC_[0-9a-fA-F]{12}\s*', str(target)):
            target = target.next_sibling
        if not isinstance(target, NavigableString) or isinstance(target, Comment):
            return []
        same_split_run = node.parent is last_parent and node.previous_sibling is last_source_node
        if not same_split_run:
            domain_index += 1
        render_domains[match.group(1)] = f'{getattr(node.parent, "name", None) or "root"}:{domain_index:06d}'
        last_parent = node.parent
        last_source_node = target
    return [
        {'unit_id': unit_id, 'source_text': source_segments[unit_id], 'render_domain_id': render_domains.get(unit_id, '')}
        for unit_id in expected_order
    ]
def strip_translation_unit_markers(html_content: str) -> str:
    # Удаляет все strict и malformed TSEG service anchors перед audit/publication, включая укороченные marker IDs.
    if not html_content:
        return html_content
    return _ANY_TRANSLATION_UNIT_RE.sub('', html_content)
def validate_publication_policy(source_html: str, translated_html: str, current_chapter_path: str | None = None) -> tuple[bool, str]:
    # Возвращает legacy-compatible список доказанных service/source-policy утечек без изменения candidate HTML.
    reasons = []
    if _ANY_TRANSLATION_UNIT_RE.search(translated_html or ''):
        reasons.append('SERVICE_MARKER_LEAK')
    current_title = get_source_document_title(source_html)
    decisions = classify_source_text_nodes(
        translated_html or '',
        current_chapter_path=current_chapter_path,
        current_chapter_title=current_title,
    )
    if any(item['unit_kind'] == 'SOURCE_BOILERPLATE' for item in decisions):
        reasons.append('SOURCE_NOISE_LEAK')
    if any(item['unit_kind'] == 'NEXT_CHAPTER_BOUNDARY' for item in decisions):
        reasons.append('NEXT_CHAPTER_BOUNDARY_LEAK')
    return (False, '; '.join(reasons)) if reasons else (True, 'OK')
def finalize_publication_html(source_html: str, translated_html: str, current_chapter_path: str | None = None) -> str:
    # Удаляет service/policy artifacts и fail-closed запрещает их попадание в опубликованный HTML.
    current_title = get_source_document_title(source_html)
    cleaned = strip_translation_unit_markers(translated_html or '')
    cleaned = strip_nontranslatable_source_units(cleaned, current_chapter_path=current_chapter_path, current_chapter_title=current_title)
    cleaned = _repair_impossible_quoted_period_boundaries(cleaned)
    if _ANY_TRANSLATION_UNIT_RE.search(cleaned):
        raise ValueError('Publication gate: в финальном HTML остались TSEG service anchors.')
    residual = [
        decision for decision in classify_source_text_nodes(cleaned, current_chapter_path=current_chapter_path, current_chapter_title=current_title)
        if decision['unit_kind'] not in TRANSLATABLE_SOURCE_UNIT_KINDS
    ]
    if residual:
        sample = ', '.join(f"{item['unit_kind']}:{item['text'][:40]!r}" for item in residual[:3])
        raise ValueError(f'Publication gate: остались non-translatable source-policy units: {sample}.')
    return cleaned
def _exact_marked_source_segments(marked_source: str) -> dict[str, str] | None:
    # Извлекает exact source text sibling каждого TSEG, пропуская обязательный/legacy-optional TSRC companion.
    soup = BeautifulSoup(marked_source or '', 'html.parser')
    segments = {}
    for node in soup.find_all(string=True):
        if not isinstance(node, Comment):
            continue
        match = re.fullmatch(r'\s*TSEG_(\d{6})\s*', str(node))
        if not match:
            continue
        sibling = node.next_sibling
        if isinstance(sibling, Comment) and re.fullmatch(r'\s*TSRC_[0-9a-fA-F]{12}\s*', str(sibling)):
            sibling = sibling.next_sibling
        if not isinstance(sibling, NavigableString) or isinstance(sibling, Comment):
            return None
        segments[match.group(1)] = str(sibling)
    return segments
def _normalize_marker_id(value: str) -> str:
    # Нормализует укороченный numeric marker ID к canonical six-digit form без обрезки больших значений.
    number = int(value)
    return f'{number:06d}' if number <= 999999 else str(number)
def _marker_hashes(html_content: str, matches: list[re.Match]) -> list[str | None]:
    # Извлекает только непосредственно следующий TSRC companion, не доверяя hash внутри translated content.
    hashes = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html_content or '')
        companion = _ADJACENT_SOURCE_HASH_RE.match((html_content or '')[match.end():end])
        hashes.append(companion.group(1).lower() if companion else None)
    return hashes
def _hashes_support_repaired_order(expected_order: list[str], repaired_order: list[str], expected_hashes: list[str | None], actual_hashes: list[str | None]) -> bool:
    # Проверяет каждый реально сохраненный TSRC против source hash восстановленного canonical unit.
    expected_positions = {unit_id: index for index, unit_id in enumerate(expected_order)}
    for unit_id, actual_hash in zip(repaired_order, actual_hashes):
        if actual_hash is None:
            continue
        position = expected_positions.get(unit_id)
        if position is None or expected_hashes[position] != actual_hash:
            return False
    return True
def _repair_marker_sequence(expected_order: list[str], actual_order: list[str], expected_hashes: list[str | None], actual_hashes: list[str | None]) -> tuple[list[str], dict]:
    # Восстанавливает marker IDs по source hash, одиночной опечатке или длинному large-offset run без угадывания small gaps.
    expected_positions = {unit_id: index for index, unit_id in enumerate(expected_order)}
    if actual_order == expected_order:
        for expected_hash, actual_hash in zip(expected_hashes, actual_hashes):
            if actual_hash is None or actual_hash == expected_hash:
                continue
            if actual_hash in expected_hashes:
                return list(actual_order), {'kind': 'unrepaired', 'changed': False, 'runs': [], 'reason': 'source_hash_conflict'}
        return list(actual_order), {'kind': 'exact', 'changed': False, 'runs': []}
    expected_hash_counts = Counter(value for value in expected_hashes if value)
    expected_by_hash = {
        value: expected_order[index]
        for index, value in enumerate(expected_hashes)
        if value and expected_hash_counts[value] == 1
    }
    if actual_order and len(actual_hashes) == len(actual_order) and all(actual_hashes) and all(value in expected_by_hash for value in actual_hashes):
        corrected = [expected_by_hash[value] for value in actual_hashes]
        positions = [expected_positions[unit_id] for unit_id in corrected]
        if len(corrected) == len(set(corrected)) and positions == sorted(positions):
            return corrected, {'kind': 'source_hash', 'changed': corrected != actual_order, 'runs': []}
    single, single_changed = _repair_single_marker_id_typo(expected_order, actual_order)
    if single_changed:
        if not _hashes_support_repaired_order(expected_order, single, expected_hashes, actual_hashes):
            return list(actual_order), {'kind': 'unrepaired', 'changed': False, 'runs': [], 'reason': 'source_hash_conflict'}
        return single, {'kind': 'single_typo', 'changed': True, 'runs': []}
    if not actual_order or len(actual_order) > len(expected_order) or actual_order[0] != expected_order[0]:
        return list(actual_order), {'kind': 'unrepaired', 'changed': False, 'runs': [], 'reason': 'unsupported_sequence'}
    corrected = list(actual_order)
    runs = []
    index = 0
    changed = False
    while index < len(actual_order):
        try:
            offset = int(actual_order[index]) - int(expected_order[index])
        except (ValueError, IndexError):
            return list(actual_order), {'kind': 'unrepaired', 'changed': False, 'runs': [], 'reason': 'non_numeric_sequence'}
        end = index + 1
        while end < len(actual_order) and int(actual_order[end]) - int(expected_order[end]) == offset:
            end += 1
        run_length = end - index
        if offset == 0:
            runs.append({'start': index, 'end': end, 'offset': 0})
        elif abs(offset) >= 10 and run_length >= 3:
            corrected[index:end] = expected_order[index:end]
            runs.append({'start': index, 'end': end, 'offset': offset})
            changed = True
        else:
            return list(actual_order), {'kind': 'unrepaired', 'changed': False, 'runs': [], 'reason': 'unsafe_offset_run'}
        index = end
    positions = [expected_positions[unit_id] for unit_id in corrected if unit_id in expected_positions]
    if len(corrected) != len(set(corrected)) or len(positions) != len(corrected) or positions != sorted(positions):
        return list(actual_order), {'kind': 'unrepaired', 'changed': False, 'runs': [], 'reason': 'non_monotonic_repair'}
    if not _hashes_support_repaired_order(expected_order, corrected, expected_hashes, actual_hashes):
        return list(actual_order), {'kind': 'unrepaired', 'changed': False, 'runs': [], 'reason': 'source_hash_conflict'}
    return corrected, {'kind': 'affine_offset' if changed else 'exact', 'changed': changed, 'runs': runs}
def _parse_translation_segments(marked_source: str, translated_html: str) -> tuple[list[str], dict[str, str], dict]:
    # Парсит strict/lenient envelopes и применяет только доказанный marker-sequence repair.
    expected_order, _ = _marker_segments(marked_source)
    expected_matches = list(TRANSLATION_UNIT_RE.finditer(marked_source or ''))
    expected_hashes = _marker_hashes(marked_source or '', expected_matches)
    matches = list(_LENIENT_TRANSLATION_UNIT_RE.finditer(translated_html or ''))
    raw_order = [_normalize_marker_id(match.group(1)) for match in matches]
    actual_hashes = _marker_hashes(translated_html or '', matches)
    order, repair = _repair_marker_sequence(expected_order, raw_order, expected_hashes, actual_hashes)
    segments = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(translated_html or '')
        unit_id = order[index]
        if unit_id in segments:
            return order, {}, {**repair, 'kind': 'duplicate_after_repair', 'changed': False}
        segments[unit_id] = _trim_lenient_segment((translated_html or '')[match.end():end])
    return order, segments, repair
def _parse_translation_segments_for_repair(marked_source: str, translated_html: str) -> tuple[list[str], dict[str, str], dict]:
    # Разрешает planner-у видеть доказанный monotonic gap без переименования markers, сохраняя общий parser fail-closed.
    order, segments, repair = _parse_translation_segments(marked_source, translated_html)
    if repair.get('kind') != 'unrepaired':
        return order, segments, repair
    expected_order, _ = _marker_segments(marked_source)
    expected_matches = list(TRANSLATION_UNIT_RE.finditer(marked_source or ''))
    expected_hashes = _marker_hashes(marked_source or '', expected_matches)
    matches = list(_LENIENT_TRANSLATION_UNIT_RE.finditer(translated_html or ''))
    raw_order = [_normalize_marker_id(match.group(1)) for match in matches]
    expected_positions = {unit_id: index for index, unit_id in enumerate(expected_order)}
    positions = [expected_positions.get(unit_id) for unit_id in raw_order]
    if not raw_order or len(raw_order) != len(set(raw_order)) or any(position is None for position in positions) or positions != sorted(positions):
        return order, segments, repair
    actual_hashes = _marker_hashes(translated_html or '', matches)
    if repair.get('reason') == 'source_hash_conflict' and raw_order == expected_order:
        expected_units_by_hash = {}
        for hash_index, candidate_hash in enumerate(expected_hashes):
            if candidate_hash:
                expected_units_by_hash.setdefault(candidate_hash, []).append(expected_order[hash_index])
        suspicious_unit_ids = set()
        for index, (expected_hash, actual_hash) in enumerate(zip(expected_hashes, actual_hashes)):
            if actual_hash is None or actual_hash == expected_hash:
                continue
            suspicious_unit_ids.add(expected_order[index])
            suspicious_unit_ids.update(expected_units_by_hash.get(actual_hash, ()))
        if suspicious_unit_ids:
            raw_segments = {}
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(translated_html or '')
                raw_segments[raw_order[index]] = _trim_lenient_segment((translated_html or '')[match.end():end])
            return raw_order, raw_segments, {
                'kind': 'source_hash_conflict_local',
                'changed': False,
                'runs': [],
                'reason': 'source_hash_conflict',
                'suspicious_unit_ids': sorted(suspicious_unit_ids, key=expected_positions.get),
            }
    if not _hashes_support_repaired_order(expected_order, raw_order, expected_hashes, actual_hashes):
        return order, segments, repair
    raw_segments = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(translated_html or '')
        raw_segments[raw_order[index]] = _trim_lenient_segment((translated_html or '')[match.end():end])
    return raw_order, raw_segments, {'kind': 'gapped_exact', 'changed': False, 'runs': [], 'reason': 'monotonic_expected_subset'}
def _target_fragment_dom_path(root, node) -> str:
    # Кодирует DOM-путь одного target text fragment внутри финального Segment Envelope.
    parent = getattr(node, 'parent', None)
    if parent is None:
        return ''
    parts = []
    current = parent
    while current is not None:
        name = getattr(current, 'name', None) or '[document]'
        ancestor = getattr(current, 'parent', None)
        if current is root or ancestor is None:
            parts.append(name)
            break
        siblings = [child for child in ancestor.children if isinstance(child, Tag)]
        parts.append(f'{name}[{siblings.index(current) if current in siblings else 0}]')
        current = ancestor
    text_nodes = [child for child in parent.children if isinstance(child, NavigableString) and not isinstance(child, Comment) and str(child).strip()]
    return '/' + '/'.join(reversed(parts)) + f'/text()[{text_nodes.index(node) if node in text_nodes else 0}]'
def _canonicalize_translation_marker_ids(translated_html: str, repaired_order: list[str]) -> str:
    # Переписывает только marker tokens к canonical IDs после доказанного sequence repair.
    matches = list(_LENIENT_TRANSLATION_UNIT_RE.finditer(translated_html or ''))
    if len(matches) != len(repaired_order):
        return ''
    result = translated_html or ''
    for match, unit_id in reversed(list(zip(matches, repaired_order))):
        result = result[:match.start()] + f'<!-- TSEG_{unit_id} -->' + result[match.end():]
    return result
def _target_fragments_by_marker(marked_translation: str, expected_order: list[str]) -> dict[str, list[dict]]:
    # Группирует visible target fragments по canonical TSEG в полном final DOM, сохраняя global DOM paths.
    soup = BeautifulSoup(marked_translation or '', 'html.parser')
    root = soup.body or soup
    fragments = {unit_id: [] for unit_id in expected_order}
    current_unit_id = None
    for node in root.descendants:
        if isinstance(node, Comment):
            match = re.fullmatch(r'\s*TSEG_(\d{6})\s*', str(node))
            if match:
                current_unit_id = match.group(1) if match.group(1) in fragments else None
            continue
        if not isinstance(node, NavigableString) or not current_unit_id or not str(node).strip():
            continue
        if node.find_parent(['script', 'style']):
            continue
        fragments[current_unit_id].append({
            'translated_text': str(node).strip(),
            'target_dom_path': _target_fragment_dom_path(root, node),
            'part_index': len(fragments[current_unit_id]),
        })
    return fragments
def analyze_translation_unit_alignment(marked_source: str, translated_html: str) -> dict:
    # Возвращает machine-readable marker alignment, включая affine-offset salvage и реальные gaps.
    expected_order, _ = _marker_segments(marked_source)
    observed_matches = list(_LENIENT_TRANSLATION_UNIT_RE.finditer(translated_html or ''))
    observed_order = [_normalize_marker_id(match.group(1)) for match in observed_matches]
    repaired_order, _, marker_repair = _parse_translation_segments(marked_source, translated_html)
    expected_set = set(expected_order)
    repaired_set = set(repaired_order)
    return {
        'expected_order': expected_order,
        'observed_order': observed_order,
        'repaired_order': repaired_order,
        'missing_unit_ids': [unit_id for unit_id in expected_order if unit_id not in repaired_set],
        'unexpected_unit_ids': [unit_id for unit_id in repaired_order if unit_id not in expected_set],
        'marker_repair': marker_repair,
    }
def build_translation_provenance(marked_source: str, marked_translation: str, document_id: str | None, source_unit_offset: int = 0, source_unit_ids: list[str] | None = None, marker_repair_report: dict | None = None, repaired_unit_ids: set[str] | None = None, allow_positional_fallback: bool = False) -> list[dict]:
    # Строит one-to-many alignment edges, разрешая positional fallback только после доказанной DOM-полноты.
    if not document_id:
        return []
    repaired_unit_ids = set(repaired_unit_ids or ())
    expected_order, source_fragments = _marker_segments(marked_source)
    source_segments = _exact_marked_source_segments(marked_source)
    actual_order, translated_segments, parsed_marker_repair = _parse_translation_segments(marked_source, marked_translation)
    marker_repair = marker_repair_report or parsed_marker_repair
    if not expected_order or source_segments is None or set(source_segments) != set(expected_order):
        return []
    if source_unit_ids is not None and len(source_unit_ids) != len(expected_order):
        return []
    if parsed_marker_repair.get('kind') in {'unrepaired', 'duplicate_after_repair'} or actual_order != expected_order:
        if not allow_positional_fallback:
            return []
        matches = list(_LENIENT_TRANSLATION_UNIT_RE.finditer(marked_translation or ''))
        observed_order = [_normalize_marker_id(match.group(1)) for match in matches]
        if len(matches) != len(expected_order) or len(observed_order) != len(set(observed_order)) or set(observed_order) != set(expected_order):
            return []
        expected_hashes = _marker_hashes(marked_source or '', list(TRANSLATION_UNIT_RE.finditer(marked_source or '')))
        actual_hashes = _marker_hashes(marked_translation or '', matches)
        if any(actual_hash is not None and actual_hash != expected_hashes[index] for index, actual_hash in enumerate(actual_hashes)):
            return []
        translated_segments = {}
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(marked_translation or '')
            translated_segments[expected_order[index]] = _trim_lenient_segment((marked_translation or '')[match.end():end])
        actual_order = list(expected_order)
        marker_repair = {'kind': 'dom_order_fallback', 'changed': True, 'runs': []}
    canonical_translation = _canonicalize_translation_marker_ids(marked_translation, actual_order)
    target_fragments_by_unit = _target_fragments_by_marker(canonical_translation, expected_order)
    provenance = []
    for local_index, unit_id in enumerate(expected_order):
        fragment = translated_segments.get(unit_id, '')
        if not _translation_segment_is_valid(source_fragments[unit_id], fragment):
            return []
        target_fragments = target_fragments_by_unit.get(unit_id) or []
        if not target_fragments:
            return []
        unit_index = source_unit_offset + local_index
        source_unit_id = source_unit_ids[local_index] if source_unit_ids is not None else f'{document_id}:{unit_index:06d}'
        for target_fragment in target_fragments:
            part_index = target_fragment['part_index']
            split = len(target_fragments) > 1
            target_slot_id = f'TGT_{unit_index:06d}_{part_index:03d}' if split else f'TGT_{unit_index:06d}'
            provenance.append({
                'source_unit_id': source_unit_id,
                'target_slot_id': target_slot_id,
                'target_fragment_id': f'TFR_{unit_index:06d}_{part_index:03d}',
                'source_text': source_segments[unit_id],
                'translated_text': target_fragment['translated_text'],
                'alignment_kind': 'SPLIT' if split else ('MARKER_RENUMBERED' if marker_repair.get('changed') else 'EXACT'),
                'origin': 'local_repair' if unit_id in repaired_unit_ids else 'translation',
                'source_hash_short': _short_source_hash(source_segments[unit_id]),
                'target_dom_path': target_fragment['target_dom_path'],
                'part_index': part_index,
                'part_count': len(target_fragments),
                'marker_repair': marker_repair,
            })
    return provenance
def _visible_text_length(fragment: str) -> int:
    # Считает длину видимого текста HTML-фрагмента без служебных комментариев.
    if not fragment:
        return 0
    return len(BeautifulSoup(fragment, 'html.parser').get_text('', strip=True))
def _segment_visible_text(fragment: str) -> str:
    # Извлекает видимый текст одного TSEG-сегмента без model-owned HTML-структуры.
    return BeautifulSoup(fragment or '', 'html.parser').get_text('', strip=True)
def is_translation_unit_meaningful(source_text: str, translated_text: str) -> bool:
    # Отвергает punctuation-only заглушку вроде «— …», когда source содержит смысловые символы.
    if not source_text.strip():
        return True
    if _SOURCE_MEANINGFUL_RE.search(source_text) and not _TARGET_MEANINGFUL_RE.search(translated_text or ''):
        return False
    return bool((translated_text or '').strip())
def _translation_segment_is_valid(source_fragment: str, translated_fragment: str) -> bool:
    # Проверяет локальный TSEG по непустоте, смысловому payload и консервативному отношению длины.
    source_text = _segment_visible_text(source_fragment)
    translated_text = _segment_visible_text(translated_fragment)
    if source_text and not is_translation_unit_meaningful(source_text, translated_text):
        return False
    if len(source_text) >= MIN_CHUNK_SLOT_RATIO_SOURCE_CHARS and len(translated_text) < len(source_text) * MIN_CHUNK_SLOT_TRANSLATION_RATIO:
        return False
    return True
def _translation_expansion_baseline(expected_order: list[str], source_segments: dict[str, str], translated_segments: dict[str, str]) -> float:
    # Оценивает типичное отношение target/source по доказанно полным units, устойчиво к единичному merged-outlier.
    ratios = []
    for unit_id in expected_order:
        fragment = translated_segments.get(unit_id)
        if fragment is None or not _translation_segment_is_valid(source_segments[unit_id], fragment):
            continue
        source_len = len(_segment_visible_text(source_segments[unit_id]).strip())
        target_len = len(_segment_visible_text(fragment).strip())
        if source_len >= 4 and target_len:
            ratios.append(target_len / source_len)
    return median(ratios) if ratios else 1.0
def _neighbor_coverage_suspected(unit_id: str, baseline_ratio: float, source_segments: dict[str, str], translated_segments: dict[str, str]) -> bool:
    # Помечает только аномально расширенный сосед как риск merged coverage, не принимая это за доказательство перевода.
    fragment = translated_segments.get(unit_id)
    if fragment is None or not _translation_segment_is_valid(source_segments[unit_id], fragment):
        return False
    source_len = len(_segment_visible_text(source_segments[unit_id]).strip())
    target_len = len(_segment_visible_text(fragment).strip())
    if source_len < 4 or target_len < 24:
        return False
    return target_len / source_len >= max(2.0, baseline_ratio * NEIGHBOR_COVERAGE_RATIO_MULTIPLIER)
def _merge_repair_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    # Объединяет пересекающиеся/соприкасающиеся локальные окна, чтобы один source unit не переводился дважды.
    if not windows:
        return []
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
def _marker_segments(html_content: str) -> tuple[list[str], dict[str, str]]:
    # Возвращает порядок строгих TSEG-маркеров и сырой HTML между соседними маркерами.
    matches = list(TRANSLATION_UNIT_RE.finditer(html_content or ''))
    order = [match.group(1) for match in matches]
    segments = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html_content)
        segments[match.group(1)] = html_content[match.end():end]
    return order, segments
def _trim_lenient_segment(fragment: str) -> str:
    # Убирает остатки поврежденных delimiters и TSRC companion из raw TSEG segment.
    text = re.sub(r'^\s*(?:--\s*>|-->|>)\s*', '', fragment or '', count=1)
    text = _LENIENT_SOURCE_HASH_RE.sub('', text, count=1)
    return re.sub(r'\s*(?:<!--|<!-+)\s*$', '', text, count=1)
def _repair_single_marker_id_typo(expected_order: list[str], actual_order: list[str]) -> tuple[list[str], bool]:
    # Исправляет ровно один marker-ID только когда позиция однозначно доказывает ожидаемый ID.
    if len(actual_order) != len(expected_order):
        return actual_order, False
    mismatches = [index for index, (expected, actual) in enumerate(zip(expected_order, actual_order)) if expected != actual]
    if len(mismatches) != 1:
        return actual_order, False
    index = mismatches[0]
    expected_id = expected_order[index]
    wrong_id = actual_order[index]
    if expected_id in actual_order:
        return actual_order, False
    if wrong_id in expected_order and actual_order.count(wrong_id) < 2:
        return actual_order, False
    corrected = list(actual_order)
    corrected[index] = expected_id
    return corrected, True
def _lenient_marker_segments(html_content: str, expected_order: list[str], marked_source: str | None = None) -> tuple[list[str], dict[str, str], bool]:
    # Сохраняет legacy API и делегирует доказанный sequence repair полному Segment Envelope parser-у.
    if marked_source is not None:
        order, segments, repair = _parse_translation_segments(marked_source, html_content)
        return order, segments, bool(repair.get('changed'))
    matches = list(_LENIENT_TRANSLATION_UNIT_RE.finditer(html_content or ''))
    raw_order = [_normalize_marker_id(match.group(1)) for match in matches]
    order, typo_repaired = _repair_single_marker_id_typo(expected_order, raw_order)
    segments = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html_content or '')
        unit_id = order[index]
        if unit_id in segments:
            return order, {}, typo_repaired
        segments[unit_id] = _trim_lenient_segment((html_content or '')[match.end():end])
    return order, segments, typo_repaired
def _complete_translation_segments(marked_source: str, translated_html: str) -> dict[str, str] | None:
    # Возвращает полный доказанный map после hash/sequence salvage, не доверяя raw marker ID модели.
    expected_order, source_segments = _marker_segments(marked_source)
    if not expected_order:
        return {}
    actual_order, translated_segments, repair = _parse_translation_segments(marked_source, translated_html)
    if repair.get('kind') in {'unrepaired', 'duplicate_after_repair'}:
        return None
    if actual_order != expected_order or len(actual_order) != len(set(actual_order)):
        return None
    translations = {}
    for unit_id in expected_order:
        fragment = translated_segments.get(unit_id, '')
        if not _translation_segment_is_valid(source_segments[unit_id], fragment):
            return None
        translations[unit_id] = _segment_visible_text(fragment)
    return translations
def build_translation_unit_repair_plan(marked_source: str, translated_html: str, max_units: int | None = None, allow_trailing_partial_salvage: bool = False, max_source_chars: int = MAX_LOCAL_REPAIR_SOURCE_CHARS, max_windows: int = MAX_LOCAL_REPAIR_WINDOWS, max_source_ratio: float = MAX_LOCAL_REPAIR_SOURCE_RATIO) -> dict | None:
    # Планирует policy-skip, точечный tail fill или локальные retranslation windows по source-budget вместо лимита «1–3 TSEG».
    expected_order, source_segments = _marker_segments(marked_source)
    if not expected_order or max_source_chars <= 0 or max_windows <= 0 or not 0 < max_source_ratio <= 1:
        return None
    actual_order, translated_segments, marker_repair = _parse_translation_segments_for_repair(marked_source, translated_html)
    if marker_repair.get('kind') in {'unrepaired', 'duplicate_after_repair'}:
        return None
    if not actual_order or not translated_segments or len(actual_order) != len(set(actual_order)):
        return None
    expected_index = {unit_id: index for index, unit_id in enumerate(expected_order)}
    if any(unit_id not in expected_index for unit_id in actual_order):
        return None
    positions = [expected_index[unit_id] for unit_id in actual_order]
    if positions != sorted(positions):
        return None
    present_ids = set(translated_segments)
    source_noise_indexes = {
        index for index, unit_id in enumerate(expected_order)
        if is_source_boilerplate_text(_segment_visible_text(source_segments[unit_id]))
    }
    source_noise_id_set = {expected_order[index] for index in source_noise_indexes}
    suspicious_unit_ids = set(marker_repair.get('suspicious_unit_ids') or ())
    problem_indexes = set()
    classifications = []
    for index, unit_id in enumerate(expected_order):
        if index in source_noise_indexes:
            classifications.append({'unit_id': unit_id, 'classification': 'source_noise', 'reason': 'source_policy'})
            continue
        if unit_id in suspicious_unit_ids:
            problem_indexes.add(index)
            classifications.append({'unit_id': unit_id, 'classification': 'suspected_alignment', 'reason': 'source_hash_conflict'})
            continue
        fragment = translated_segments.get(unit_id)
        if fragment is None or not _translation_segment_is_valid(source_segments[unit_id], fragment):
            problem_indexes.add(index)
    if not problem_indexes and not source_noise_indexes:
        return None
    baseline_ratio = _translation_expansion_baseline(expected_order, source_segments, translated_segments)
    windows = []
    sorted_problems = sorted(problem_indexes)
    groups = []
    for index in sorted_problems:
        if groups and index == groups[-1][1] + 1:
            groups[-1] = (groups[-1][0], index)
        else:
            groups.append((index, index))
    trailing_problem_suffix = bool(groups and groups[-1][1] == len(expected_order) - 1 and groups[-1][0] == sorted_problems[0] and len(groups) == 1)
    for start, end in groups:
        missing_marker = any(expected_order[index] not in present_ids for index in range(start, end + 1))
        left = start - 1 if start > 0 and start - 1 not in source_noise_indexes else None
        right = end + 1 if end + 1 < len(expected_order) and end + 1 not in source_noise_indexes else None
        left_suspected = left is not None and _neighbor_coverage_suspected(expected_order[left], baseline_ratio, source_segments, translated_segments)
        right_suspected = right is not None and _neighbor_coverage_suspected(expected_order[right], baseline_ratio, source_segments, translated_segments)
        tail_fill = allow_trailing_partial_salvage and trailing_problem_suffix and start > positions[-1]
        window_start, window_end = start, end
        if not tail_fill and (missing_marker or left_suspected or right_suspected or end == len(expected_order) - 1):
            if left is not None:
                window_start = left
            if right is not None and end < len(expected_order) - 1:
                window_end = right
        windows.append((window_start, window_end))
        classification = 'already_covered' if left_suspected or right_suspected else 'missing'
        for index in range(start, end + 1):
            if expected_order[index] in suspicious_unit_ids:
                continue
            classifications.append({
                'unit_id': expected_order[index],
                'classification': classification,
                'reason': 'neighbor_expansion_requires_window_retranslation' if classification == 'already_covered' else ('marker_missing' if expected_order[index] not in present_ids else 'invalid_translation'),
            })
    windows = _merge_repair_windows(windows)
    if len(windows) > max_windows:
        return None
    repair_indexes = {index for start, end in windows for index in range(start, end + 1) if index not in source_noise_indexes}
    if max_units is not None and (max_units <= 0 or len(repair_indexes) > max_units):
        return None
    source_char_count = sum(len(_segment_visible_text(source_segments[expected_order[index]])) for index in repair_indexes)
    total_source_chars = sum(len(_segment_visible_text(source_segments[unit_id])) for unit_id in expected_order if unit_id not in source_noise_id_set)
    if source_char_count > max_source_chars or (len(expected_order) >= LOCAL_REPAIR_RATIO_MIN_UNITS and total_source_chars and source_char_count / total_source_chars > max_source_ratio):
        return None
    safe_translations = {}
    for index, unit_id in enumerate(expected_order):
        if index in repair_indexes or index in source_noise_indexes:
            continue
        fragment = translated_segments.get(unit_id)
        if fragment is None or not _translation_segment_is_valid(source_segments[unit_id], fragment):
            return None
        safe_translations[unit_id] = _segment_visible_text(fragment)
    if len(safe_translations) + len(repair_indexes) + len(source_noise_indexes) != len(expected_order):
        return None
    classification_by_id = {item['unit_id']: item for item in classifications}
    repair_units = []
    for index in sorted(repair_indexes):
        unit_id = expected_order[index]
        repair_units.append({
            'unit_id': unit_id,
            'source_text': _segment_visible_text(source_segments[unit_id]),
            'current_translation': _segment_visible_text(translated_segments.get(unit_id, '')),
            'repair_reason': classification_by_id.get(unit_id, {}).get('classification', 'neighbor_context'),
            'left_source': _segment_visible_text(source_segments[expected_order[index - 1]]) if index > 0 else '',
            'right_source': _segment_visible_text(source_segments[expected_order[index + 1]]) if index + 1 < len(expected_order) else '',
        })
    repair_windows = [
        {'unit_ids': [expected_order[index] for index in range(start, end + 1) if index not in source_noise_indexes]}
        for start, end in windows
    ]
    return {
        'repair_units': repair_units,
        'repair_windows': repair_windows,
        'safe_translations': safe_translations,
        'source_texts': {unit_id: _segment_visible_text(source_segments[unit_id]) for unit_id in expected_order},
        'source_noise_ids': [expected_order[index] for index in sorted(source_noise_indexes)],
        'classifications': sorted(classifications, key=lambda item: expected_index[item['unit_id']]),
        'source_char_count': source_char_count,
        'source_ratio': source_char_count / total_source_chars if total_source_chars else 0.0,
        'marker_typo_repaired': bool(marker_repair.get('changed')),
        'marker_repair': marker_repair,
    }
def rebuild_source_dom_from_unit_translations(marked_source: str, translations: dict[str, str], omitted_unit_ids: set[str] | None = None) -> str | None:
    # Накладывает полный доказанный map переводов и policy-omissions на source-owned DOM без model-owned структуры.
    expected_order, _ = _marker_segments(marked_source)
    omitted_unit_ids = set(omitted_unit_ids or ())
    if omitted_unit_ids & set(translations) or set(translations) | omitted_unit_ids != set(expected_order):
        return None
    soup = BeautifulSoup(marked_source, 'html.parser')
    markers = []
    for node in soup.find_all(string=True):
        if not isinstance(node, Comment):
            continue
        match = re.fullmatch(r'\s*TSEG_(\d{6})\s*', str(node))
        if match:
            markers.append((node, match.group(1)))
    if len(markers) != len(expected_order):
        return None
    for marker, unit_id in markers:
        target = marker.next_sibling
        hash_marker = None
        if isinstance(target, Comment) and re.fullmatch(r'\s*TSRC_[0-9a-fA-F]{12}\s*', str(target)):
            hash_marker = target
            target = target.next_sibling
        if not isinstance(target, NavigableString) or isinstance(target, Comment):
            return None
        if unit_id in omitted_unit_ids:
            target.extract()
            if hash_marker is not None:
                hash_marker.extract()
            marker.extract()
            continue
        translated_text = translations.get(unit_id, '')
        if not translated_text.strip():
            return None
        source_piece = str(target)
        leading = source_piece[:len(source_piece) - len(source_piece.lstrip())]
        trailing = source_piece[len(source_piece.rstrip()):]
        target.replace_with(NavigableString(leading + translated_text + trailing))
    return str(soup)
def build_translation_tail_recovery(marked_source: str, partial_translation: str) -> tuple[str, str, int, int] | None:
    # Отделяет доказанный Segment Envelope prefix от source-tail после deterministic marker salvage.
    source_matches = list(TRANSLATION_UNIT_RE.finditer(marked_source or ''))
    partial_matches = list(_LENIENT_TRANSLATION_UNIT_RE.finditer(partial_translation or ''))
    if len(source_matches) < 2 or len(partial_matches) < 2:
        return None
    expected_order, source_segments = _marker_segments(marked_source)
    actual_order, translated_segments, marker_repair = _parse_translation_segments(marked_source, partial_translation)
    if marker_repair.get('kind') in {'unrepaired', 'duplicate_after_repair'}:
        return None
    consecutive_count = 0
    for index, unit_id in enumerate(actual_order):
        if index >= len(expected_order) or unit_id != expected_order[index]:
            break
        consecutive_count += 1
    safe_units = min(consecutive_count - 1, len(expected_order) - 1)
    while safe_units > 0:
        unit_id = expected_order[safe_units - 1]
        source_len = _visible_text_length(source_segments[unit_id])
        translated_len = _visible_text_length(translated_segments.get(unit_id, ''))
        if translated_len > 0 and (source_len < MIN_UNIT_RATIO_SOURCE_CHARS or translated_len >= source_len * MIN_UNIT_TRANSLATION_RATIO):
            break
        safe_units -= 1
    if safe_units <= 0:
        return None
    prefix_cut = partial_matches[safe_units].start()
    tail_cut = source_matches[safe_units].start()
    translated_prefix = partial_translation[:prefix_cut]
    marked_source_tail = marked_source[tail_cut:]
    if not translated_prefix.strip() or not marked_source_tail.strip():
        return None
    return translated_prefix, marked_source_tail, safe_units, len(expected_order)
def validate_translation_completeness(marked_source: str, translated_html: str) -> tuple[bool, str]:
    # Доказывает полноту после deterministic marker salvage и отвергает неоднозначную sequence corruption.
    expected_order, source_segments = _marker_segments(marked_source)
    if not expected_order:
        return True, 'OK'
    actual_order, translated_segments, marker_repair = _parse_translation_segments(marked_source, translated_html)
    if not actual_order:
        return False, f'утрачены все TSEG-маркеры ({len(expected_order)} ожидается)'
    if marker_repair.get('kind') == 'duplicate_after_repair' or len(actual_order) != len(set(actual_order)):
        return False, 'обнаружены повторяющиеся TSEG-маркеры'
    if marker_repair.get('kind') == 'unrepaired':
        return False, 'неоднозначное повреждение порядка TSEG-сегментов'
    if actual_order != expected_order:
        expected_set = set(expected_order)
        actual_set = set(actual_order)
        missing = [unit_id for unit_id in expected_order if unit_id not in actual_set]
        unexpected = [unit_id for unit_id in actual_order if unit_id not in expected_set]
        if missing:
            return False, f'отсутствуют TSEG-сегменты: {", ".join(missing[:8])}'
        if unexpected:
            return False, f'обнаружены неожиданные TSEG-сегменты: {", ".join(unexpected[:8])}'
        return False, 'нарушен порядок TSEG-сегментов'
    source_total = 0
    translated_total = 0
    for unit_id in expected_order:
        source_text = _segment_visible_text(source_segments[unit_id])
        translated_text = _segment_visible_text(translated_segments.get(unit_id, ''))
        source_total += len(source_text)
        translated_total += len(translated_text)
        if source_text and not is_translation_unit_meaningful(source_text, translated_text):
            return False, f'TSEG_{unit_id} не содержит смыслового переведенного текста'
        if len(source_text) >= MIN_UNIT_RATIO_SOURCE_CHARS and len(translated_text) < len(source_text) * MIN_UNIT_TRANSLATION_RATIO:
            return False, f'TSEG_{unit_id} аномально короткий ({len(translated_text)}/{len(source_text)})'
    if source_total >= MIN_TOTAL_RATIO_SOURCE_CHARS and translated_total < source_total * MIN_TOTAL_TRANSLATION_RATIO:
        return False, f'перевод аномально короткий ({translated_total}/{source_total}, меньше {MIN_TOTAL_TRANSLATION_RATIO:.0%})'
    return True, 'OK'
def _text_slot_signature(html_content: str) -> list[tuple[tuple[int, ...], str, int, int, str]]:
    # Reuses the prepared target/source DOM signature so repeated completeness checks do not rebuild the same BeautifulSoup tree.
    from chaptera_translation_core.core.prepared_chapter import prepare_target_snapshot
    return list(prepare_target_snapshot(strip_translation_unit_markers(html_content or '')).slot_signature)
def validate_chunk_completeness(source_html: str, translated_html: str) -> tuple[bool, str]:
    # Доказывает полноту chunk по DOM slots и не принимает punctuation-only заглушки как перевод смыслового source.
    if not translated_html or not translated_html.strip():
        return False, 'API вернуло пустой chunk'
    source_slots = _text_slot_signature(source_html)
    translated_slots = _text_slot_signature(translated_html)
    if not source_slots:
        return True, 'OK'
    if len(translated_slots) != len(source_slots):
        return False, f'изменено число текстовых DOM-slots: {len(source_slots)} → {len(translated_slots)}'
    source_shape = [(path, tag_name, text_ordinal) for path, tag_name, text_ordinal, _, _ in source_slots]
    translated_shape = [(path, tag_name, text_ordinal) for path, tag_name, text_ordinal, _, _ in translated_slots]
    if translated_shape != source_shape:
        return False, 'изменены порядок, тип или DOM-путь текстовых slots'
    source_total = 0
    translated_total = 0
    for index, ((_, tag_name, _, source_len, source_text), (_, _, _, translated_len, translated_text)) in enumerate(zip(source_slots, translated_slots)):
        source_total += source_len
        translated_total += translated_len
        if not is_translation_unit_meaningful(source_text, translated_text):
            return False, f'text slot #{index + 1} <{tag_name}> не содержит смыслового переведенного текста'
        if source_len >= MIN_CHUNK_SLOT_RATIO_SOURCE_CHARS and translated_len < source_len * MIN_CHUNK_SLOT_TRANSLATION_RATIO:
            return False, f'text slot #{index + 1} <{tag_name}> аномально короткий ({translated_len}/{source_len})'
    if source_total >= MIN_TOTAL_RATIO_SOURCE_CHARS and translated_total < source_total * MIN_TOTAL_TRANSLATION_RATIO:
        return False, f'перевод аномально короткий ({translated_total}/{source_total}, меньше {MIN_TOTAL_TRANSLATION_RATIO:.0%})'
    return True, 'OK'
def rebuild_source_dom_from_complete_tsegs(marked_source: str, translated_html: str) -> str | None:
    # Восстанавливает source-owned DOM из полного strict/lenient TSEG map без дополнительного API-запроса.
    translations = _complete_translation_segments(marked_source, translated_html)
    if translations is None:
        return None
    return rebuild_source_dom_from_unit_translations(marked_source, translations)
def _content_visible_length(content) -> int:
    # Считает видимый текст одного непосредственного DOM-узла для выбора безопасной границы split.
    if isinstance(content, Comment):
        return 0
    if isinstance(content, NavigableString):
        return len(str(content).strip())
    if isinstance(content, Tag):
        return len(content.get_text('', strip=True))
    return 0
def _find_retry_split(container: Tag, target_ratio: float = 0.5) -> tuple[Tag, int | tuple[int, int]] | None:
    # Находит DOM-safe boundary, ближайшую к заданной доле visible source вместо обязательного деления пополам.
    target_ratio = min(0.95, max(0.05, float(target_ratio or 0.5)))
    contents = list(container.contents)
    lengths = [_content_visible_length(content) for content in contents]
    total = sum(lengths)
    if total > 0 and len(contents) > 1:
        best = None
        left_len = 0
        target_length = total * target_ratio
        for boundary in range(1, len(contents)):
            left_len += lengths[boundary - 1]
            right_len = total - left_len
            if left_len > 0 and right_len > 0:
                score = abs(left_len - target_length)
                if best is None or score < best[0]:
                    best = (score, boundary)
        if best is not None:
            return container, best[1]
    for index, content in enumerate(contents):
        if isinstance(content, NavigableString) and not isinstance(content, Comment) and len(str(content).strip()) > MAX_TRANSLATION_UNIT_CHARS:
            text = str(content)
            target_offset = min(len(text) - 1, max(1, int(len(text) * target_ratio)))
            search_radius = max(32, min(1200, len(text) // 10))
            search_start = max(1, target_offset - search_radius)
            search_end = min(len(text) - 1, target_offset + search_radius)
            candidates = []
            for separator in ('\n\n', '。', '！', '？', '. ', '! ', '? ', '; ', ', ', ' '):
                left = text.rfind(separator, search_start, target_offset + 1)
                if left >= 0:
                    candidates.append(left + len(separator))
                right = text.find(separator, target_offset, search_end + 1)
                if right >= 0:
                    candidates.append(right + len(separator))
            offset = min((value for value in candidates if 0 < value < len(text)), key=lambda value: abs(value - target_offset), default=target_offset)
            if 0 < offset < len(text):
                return container, (index, offset)
    for child in (child for child in container.children if isinstance(child, Tag) and child.name not in ('script', 'style')):
        nested = _find_retry_split(child, target_ratio=target_ratio)
        if nested is not None:
            return nested
    return None
def _tag_path(root: Tag, target: Tag) -> tuple[int, ...]:
    # Кодирует путь к контейнеру индексами дочерних тегов для последующего deterministic merge.
    if root is target:
        return ()
    path = []
    current = target
    while current is not root:
        parent = current.parent
        if parent is None:
            raise ValueError('Не удалось построить путь к split-контейнеру.')
        tag_children = [child for child in parent.children if isinstance(child, Tag)]
        path.append(next(index for index, child in enumerate(tag_children) if child is current))
        current = parent
    return tuple(reversed(path))
def _locate_tag_path(root: Tag, path: tuple[int, ...]) -> Tag | None:
    # Находит контейнер в клонированном или переведенном DOM по стабильному tag-path.
    current = root
    for index in path:
        tag_children = [child for child in current.children if isinstance(child, Tag)]
        if index >= len(tag_children):
            return None
        current = tag_children[index]
    return current
def _content_translation_unit_count(content) -> int:
    # Считает canonical translation units непосредственного DOM content без изменения исходного дерева.
    if isinstance(content, Comment):
        return 0
    if isinstance(content, NavigableString):
        return sum(1 for piece in _split_text_node(str(content)) if piece.strip())
    if isinstance(content, Tag):
        return count_translation_units(str(content))
    return 0
def _find_retry_split_by_units(container: Tag, target_left_units: int) -> tuple[Tag, int | tuple[int, int]] | None:
    # Ищет DOM-safe boundary, ближайшую к requested числу source units, а не к длине видимого текста.
    contents = list(container.contents)
    counts = [_content_translation_unit_count(content) for content in contents]
    total = sum(counts)
    target_left_units = max(1, min(max(1, total - 1), int(target_left_units or 1)))
    if total > 1 and len(contents) > 1:
        best = None
        left_units = 0
        for boundary in range(1, len(contents)):
            left_units += counts[boundary - 1]
            right_units = total - left_units
            if left_units > 0 and right_units > 0:
                score = abs(left_units - target_left_units)
                if best is None or score < best[0]:
                    best = (score, boundary)
        if best is not None:
            return container, best[1]
    for index, content in enumerate(contents):
        if isinstance(content, NavigableString) and not isinstance(content, Comment):
            pieces = _split_text_node(str(content))
            meaningful_indexes = [piece_index for piece_index, piece in enumerate(pieces) if piece.strip()]
            if len(meaningful_indexes) > 1:
                split_piece_count = max(1, min(len(meaningful_indexes) - 1, target_left_units))
                split_piece_index = meaningful_indexes[split_piece_count]
                offset = sum(len(piece) for piece in pieces[:split_piece_index])
                if 0 < offset < len(str(content)):
                    return container, (index, offset)
    for child in (child for child in container.children if isinstance(child, Tag) and child.name not in ('script', 'style')):
        child_units = count_translation_units(str(child))
        if child_units > 1:
            nested = _find_retry_split_by_units(child, min(target_left_units, child_units - 1))
            if nested is not None:
                return nested
    return None
def _clone_dom_split(html_content: str, split: tuple[Tag, int | tuple[int, int]], original_root: Tag) -> tuple[str, str, tuple[int, ...]] | None:
    # Применяет boundary из исходного parsed tree к двум DOM-клонам и возвращает merge path.
    container, boundary = split
    path = _tag_path(original_root, container)
    soup = BeautifulSoup(html_content, 'html.parser')
    left_soup = BeautifulSoup(str(soup), 'html.parser')
    right_soup = BeautifulSoup(str(soup), 'html.parser')
    left_root = left_soup.body or left_soup
    right_root = right_soup.body or right_soup
    left_container = _locate_tag_path(left_root, path)
    right_container = _locate_tag_path(right_root, path)
    if left_container is None or right_container is None:
        return None
    if isinstance(boundary, tuple):
        content_index, offset = boundary
        left_contents = list(left_container.contents)
        right_contents = list(right_container.contents)
        if content_index >= len(left_contents) or content_index >= len(right_contents):
            return None
        left_text = str(left_contents[content_index])
        right_text = str(right_contents[content_index])
        left_contents[content_index].replace_with(NavigableString(left_text[:offset]))
        right_contents[content_index].replace_with(NavigableString(right_text[offset:]))
        for content in list(left_container.contents)[content_index + 1:]:
            content.extract()
        for content in list(right_container.contents)[:content_index]:
            content.extract()
    else:
        for content in list(left_container.contents)[boundary:]:
            content.extract()
        for content in list(right_container.contents)[:boundary]:
            content.extract()
    if _visible_text_length(str(left_soup)) == 0 or _visible_text_length(str(right_soup)) == 0:
        return None
    return str(left_soup), str(right_soup), path
def split_html_for_unit_target(html_content: str, target_left_units: int) -> tuple[str, str, tuple[int, ...]] | None:
    # Делит source около exact unit target и тем самым избегает recursive tiny-tail partition.
    if not html_content:
        return None
    soup = BeautifulSoup(html_content, 'html.parser')
    root = soup.body or soup
    split = _find_retry_split_by_units(root, target_left_units)
    if split is None:
        return None
    return _clone_dom_split(html_content, split, root)
def split_html_for_retry(html_content: str, target_ratio: float = 0.5) -> tuple[str, str, tuple[int, ...]] | None:
    # Делит HTML на две валидные части у DOM-safe границы, ближайшей к requested visible-text ratio.
    if not html_content:
        return None
    soup = BeautifulSoup(html_content, 'html.parser')
    root = soup.body or soup
    split = _find_retry_split(root, target_ratio=target_ratio)
    if split is None:
        return None
    container, boundary = split
    path = _tag_path(root, container)
    left_soup = BeautifulSoup(str(soup), 'html.parser')
    right_soup = BeautifulSoup(str(soup), 'html.parser')
    left_root = left_soup.body or left_soup
    right_root = right_soup.body or right_soup
    left_container = _locate_tag_path(left_root, path)
    right_container = _locate_tag_path(right_root, path)
    if left_container is None or right_container is None:
        return None
    if isinstance(boundary, tuple):
        content_index, offset = boundary
        left_contents = list(left_container.contents)
        right_contents = list(right_container.contents)
        if content_index >= len(left_contents) or content_index >= len(right_contents):
            return None
        left_text = str(left_contents[content_index])
        right_text = str(right_contents[content_index])
        left_contents[content_index].replace_with(NavigableString(left_text[:offset]))
        right_contents[content_index].replace_with(NavigableString(right_text[offset:]))
        for content in list(left_container.contents)[content_index + 1:]:
            content.extract()
        for content in list(right_container.contents)[:content_index]:
            content.extract()
    else:
        for content in list(left_container.contents)[boundary:]:
            content.extract()
        for content in list(right_container.contents)[:boundary]:
            content.extract()
    if _visible_text_length(str(left_soup)) == 0 or _visible_text_length(str(right_soup)) == 0:
        return None
    return str(left_soup), str(right_soup), path
def estimate_translation_request_count(html_content: str, max_units: int = MAX_PROVIDER_TRANSLATION_UNITS) -> int:
    # Считает runtime-equivalent минимальное число balanced provider leaves.
    unit_count = count_translation_units(html_content)
    if unit_count <= 0:
        return 0
    if max_units <= 0 or unit_count <= max_units:
        return 1
    minimum_leaves = max(2, (unit_count + max_units - 1) // max_units)
    target_left_units = max(1, min(max_units, round(unit_count / minimum_leaves)))
    split_plan = split_html_for_unit_target(html_content, target_left_units)
    if split_plan is None:
        split_plan = split_html_for_retry(html_content, target_ratio=target_left_units / unit_count)
    if split_plan is None:
        return 1
    left_source, right_source, _ = split_plan
    left_units = count_translation_units(left_source)
    right_units = count_translation_units(right_source)
    if not left_units or not right_units or max(left_units, right_units) >= unit_count:
        return 1
    return estimate_translation_request_count(left_source, max_units) + estimate_translation_request_count(right_source, max_units)
def merge_retry_translations(left_html: str, right_html: str, path: tuple[int, ...]) -> str:
    # Сливает две независимо переведенные половины обратно в исходный container graph.
    left_soup = BeautifulSoup(left_html, 'html.parser')
    right_soup = BeautifulSoup(right_html, 'html.parser')
    left_root = left_soup.body or left_soup
    right_root = right_soup.body or right_soup
    left_container = _locate_tag_path(left_root, path)
    right_container = _locate_tag_path(right_root, path)
    if left_container is None or right_container is None:
        raise ValueError('Перевод изменил DOM-путь split-контейнера; безопасная сборка невозможна.')
    for content in list(right_container.contents):
        left_container.append(copy.copy(content))
    return str(left_soup)
