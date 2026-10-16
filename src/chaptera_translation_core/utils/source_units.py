import hashlib
import html
import re
import unicodedata
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
MAX_SOURCE_UNIT_CHARS = 480
MAX_PROVIDER_TRANSLATION_UNITS = 80
SOURCE_UNIT_CONTENT = 'CONTENT'
SOURCE_UNIT_CURRENT_CHAPTER_TITLE = 'CURRENT_CHAPTER_TITLE'
SOURCE_UNIT_BOILERPLATE = 'SOURCE_BOILERPLATE'
SOURCE_UNIT_NEXT_CHAPTER_BOUNDARY = 'NEXT_CHAPTER_BOUNDARY'
SOURCE_UNIT_NEXT_CHAPTER = SOURCE_UNIT_NEXT_CHAPTER_BOUNDARY
SOURCE_UNIT_METADATA = 'NON_TRANSLATABLE_METADATA'
TRANSLATABLE_SOURCE_UNIT_KINDS = {SOURCE_UNIT_CONTENT, SOURCE_UNIT_CURRENT_CHAPTER_TITLE}
_ZERO_WIDTH_RE = re.compile(r'[\u200b-\u200f\u2060\ufeff]')
_DECORATIVE_SEPARATOR_RE = re.compile(r'[⊥⊤◆◇■□●○·•⋅丨|¦＿_~～*=+]+')
_SOURCE_BOILERPLATE_DOMAIN_RE = re.compile(r'101(?:kanshu|kks|read|看書網|看书网)(?:\.[a-z]{2,})?', re.IGNORECASE)
_SOURCE_BOILERPLATE_CHAPTER_END_RE = re.compile(r'^[（(]?本章完[)）]?$')
_SOURCE_TAIL_HINT_RE = re.compile(r'^(?:溫馨提示|温馨提示)\s*[:：]?$', re.IGNORECASE)
_SOURCE_NEXT_CHAPTER_RE = re.compile(r'^[<\[【（(]?\s*(?:下一章|下章|下一節|下一节|下一回|下一頁|下一页|下頁|下页|next\s+chapter)(?:\s+.*|[:：>»›】\]）)].*)?$', re.IGNORECASE)
_CHAPTER_NUMBER_RES = (
    re.compile(r'第\s*(\d+)\s*[章節节回卷]'),
    re.compile(r'(?:глава|chapter)\s*[-_:：№#]*\s*(\d+)', re.IGNORECASE),
    re.compile(r'(?:^|[/_\-])chapter[_\-]?(\d+)(?:\D|$)', re.IGNORECASE),
)
_PROMOTION_PREFIXES = (
    '最新小说', '最新小說', '本书首发', '本書首發', '记住我们', '記住我們', '记住本站', '記住本站', '记住全网最快', '記住全網最快',
    'самыесвежиеглав', 'самоеновоечтение', 'перваяпубликациякниги', 'запомнитенас', 'свежиеглавы',
    'latestchapters', 'firstpublished',
)
_PROMOTION_FRAGMENTS = (
    '首发', '首發', '最新章节', '最新章節', '全文阅读', '全文閱讀', '无错章节', '無錯章節', '无乱序', '無亂序',
    '更新不易', '记得分享', '記得分享', '小说站', '小説站', '最快更新',
    'толькона нашемсайте', 'тольконашемсайте', 'толькоздесь', 'безпропусковиошибок', 'верныеглавы',
    'onlyonoursite', 'withoutmissingchapters',
)
_DOMAIN_PROMOTION_FRAGMENTS = (
    '希望读者记一下我们域名', '希望讀者記一下我們域名', '看书网藏书全', '看書網藏書全', '全手打无错站', '全手打無錯站',
    '看书网伴你读', '看書網伴你讀', '超靠谱', '超靠譜', '超顺畅', '超順暢', 'авторпроситчитателейзапомнитьнашдомен',
    '追书就上', '追書就上', '观看最快的章节更新', '觀看最快的章節更新', '看书网书库多', '看書網書庫多',
    '看书网藏书广', '看書網藏書廣', '任你选网站', '任你選網站', '任你读网站', '任你讀網站', '超赞网站', '超讚網站',
)
def _normalize_source_noise_text(text: str) -> str:
    # Нормализует scraper/navigation text только для классификации, не меняя immutable source payload.
    decoded = html.unescape(text or '')
    normalized = unicodedata.normalize('NFKC', decoded)
    normalized = _ZERO_WIDTH_RE.sub('', normalized).casefold()
    return ' '.join(normalized.split())
def _source_noise_views(text: str) -> tuple[str, str, str]:
    # Строит normalized/compact/alnum представления для устойчивого распознавания обфусцированного source noise.
    normalized = _normalize_source_noise_text(text)
    compact = _DECORATIVE_SEPARATOR_RE.sub('', normalized)
    compact = re.sub(r'\s+', '', compact)
    alnum = re.sub(r'[^0-9a-zа-яё\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]+', '', compact)
    return normalized, compact, alnum
def _domain_is_standalone(compact: str) -> bool:
    # Проверяет, что кроме site/domain token в standalone строке осталась только декоративная пунктуация.
    without_domain = _SOURCE_BOILERPLATE_DOMAIN_RE.sub('', compact)
    without_domain = re.sub(r'[\[\]【】()（）<>《》:：/\\.!！?？,，;；\'"“”‘’—–−-]+', '', without_domain)
    return not without_domain
def _starts_like_promotion(normalized: str, alnum: str) -> bool:
    # Проверяет сильное начало рекламной standalone-фразы без поиска по произвольной сюжетной середине.
    stripped = normalized.lstrip('[【(（<《"“\'')
    stripped_alnum = re.sub(r'[^0-9a-zа-яё\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]+', '', stripped)
    return any(stripped_alnum.startswith(prefix) for prefix in _PROMOTION_PREFIXES) or any(alnum.startswith(prefix) for prefix in _PROMOTION_PREFIXES)
def is_source_boilerplate_text(text: str) -> bool:
    # Распознает доказанный scraper/site boilerplate после NFKC/deobfuscation, не удаляя сюжетные упоминания сайтов.
    normalized, compact, alnum = _source_noise_views(text)
    if not compact:
        return False
    if _SOURCE_BOILERPLATE_CHAPTER_END_RE.fullmatch(compact):
        return True
    domain_found = bool(_SOURCE_BOILERPLATE_DOMAIN_RE.search(compact))
    promotion_found = _starts_like_promotion(normalized, alnum) or any(fragment.replace(' ', '') in alnum for fragment in _PROMOTION_FRAGMENTS)
    domain_promotion_found = any(fragment.replace(' ', '') in alnum for fragment in _DOMAIN_PROMOTION_FRAGMENTS)
    if domain_found and (_domain_is_standalone(compact) or promotion_found or domain_promotion_found):
        return True
    return promotion_found and len(normalized) <= 260
def is_source_tail_boilerplate_text(text: str) -> bool:
    # diagnostics4: standalone `溫馨提示:` в последнем visible source node является site footer label.
    # Позиционное ограничение применяется в document analyzer; standalone API не удаляет такую фразу из середины сюжета.
    normalized = _normalize_source_noise_text(text).strip()
    return bool(_SOURCE_TAIL_HINT_RE.fullmatch(normalized))
def _extract_chapter_number(text: str) -> int | None:
    # Извлекает арабский номер главы из title/path/standalone heading без анализа сюжетных чисел.
    normalized = _normalize_source_noise_text(text)
    for pattern in _CHAPTER_NUMBER_RES:
        match = pattern.search(normalized)
        if match:
            return int(match.group(1))
    return None
def _normalize_heading_key(text: str) -> str:
    # Нормализует chapter heading для точного сравнения со следующим spine title.
    normalized = _normalize_source_noise_text(text)
    return re.sub(r'[^0-9a-zа-яё\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]+', '', normalized)
def _looks_like_chapter_heading(text: str) -> bool:
    # Проверяет короткую standalone форму chapter heading вместо произвольного упоминания слова «глава» в прозе.
    normalized = _normalize_source_noise_text(text).strip()
    if not normalized or len(normalized) > 220:
        return False
    if normalized.startswith('===') and normalized.endswith('==='):
        return _extract_chapter_number(normalized) is not None
    return bool(re.fullmatch(r'(?:第\s*\d+\s*[章節节回卷].*|(?:глава|chapter)\s*[-_:：№#]*\s*\d+.*)', normalized, re.IGNORECASE))
def is_source_next_chapter_tail_text(text: str, current_chapter_number: int | None = None, next_chapter_title: str | None = None) -> bool:
    # Распознает standalone navigation/next-title boundary только при сильном positional или numeric доказательстве.
    normalized = _normalize_source_noise_text(text).strip()
    if not normalized or len(normalized) > 220:
        return False
    if _SOURCE_NEXT_CHAPTER_RE.fullmatch(normalized):
        return True
    if next_chapter_title and _normalize_heading_key(normalized) == _normalize_heading_key(next_chapter_title):
        return True
    candidate_number = _extract_chapter_number(normalized)
    return bool(_looks_like_chapter_heading(normalized) and current_chapter_number is not None and candidate_number == current_chapter_number + 1)
def find_source_next_chapter_tail_start(text_nodes: list[str], current_chapter_title: str | None = None, current_chapter_path: str | None = None, next_chapter_title: str | None = None) -> int | None:
    # Ищет доказанный boundary и отсекает весь appended tail, сохраняя обычные внутренние упоминания глав.
    if not text_nodes:
        return None
    current_number = _extract_chapter_number(current_chapter_title or '') or _extract_chapter_number(current_chapter_path or '')
    if current_number is None:
        for text in text_nodes[:3]:
            current_number = _extract_chapter_number(text)
            if current_number is not None:
                break
    minimum_position = 1 if len(text_nodes) > 1 else 0
    for index in range(minimum_position, len(text_nodes)):
        if is_source_next_chapter_tail_text(text_nodes[index], current_chapter_number=current_number, next_chapter_title=next_chapter_title):
            return index
    return None
def split_source_text_node(text: str, max_chars: int = MAX_SOURCE_UNIT_CHARS) -> list[str]:
    # Делит immutable source text-node на детерминированные logical units без изменения символов и превышения hard limit.
    if max_chars <= 0:
        raise ValueError('max_chars должен быть положительным.')
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start = 0
    while len(text) - start > max_chars:
        search_min = start + max(max_chars // 2, 1)
        search_max = min(len(text), start + max_chars)
        boundary = -1
        for separator in ('\n\n', '. ', '! ', '? ', '… ', '。', '！', '？', '; ', ', ', ' '):
            pos = text.rfind(separator, search_min, search_max + 1)
            candidate = pos + len(separator)
            if pos >= 0 and candidate <= search_max:
                boundary = max(boundary, candidate)
        if boundary <= start:
            boundary = search_max
        chunks.append(text[start:boundary])
        start = boundary
    chunks.append(text[start:])
    return chunks
def _source_text_nodes(root) -> list:
    # Возвращает значимые DOM text-nodes в canonical порядке до policy-классификации.
    return [
        node for node in root.find_all(string=True)
        if not isinstance(node, Comment) and str(node).strip() and not node.find_parent(['script', 'style'])
    ]
def _source_node_dom_path(root, node) -> str:
    # Кодирует устойчивый tag-path и ordinal исходного text node без привязки к policy-фильтрации.
    parent = getattr(node, 'parent', None)
    if parent is None:
        return ''
    parts = []
    current = parent
    while current is not None:
        if current is root:
            parts.append(getattr(current, 'name', None) or '[document]')
            break
        ancestor = getattr(current, 'parent', None)
        if ancestor is None:
            break
        siblings = [child for child in ancestor.children if isinstance(child, Tag)]
        sibling_index = next((index for index, sibling in enumerate(siblings) if sibling is current), 0)
        parts.append(f'{getattr(current, "name", None) or "[document]"}[{sibling_index}]')
        current = ancestor
    text_siblings = [
        child for child in parent.children
        if isinstance(child, NavigableString) and not isinstance(child, Comment)
    ]
    text_ordinal = next((index for index, sibling in enumerate(text_siblings) if sibling is node), 0)
    return '/' + '/'.join(reversed(parts)) + f'/text()[{text_ordinal}]'
def _source_document_title_from_soup(soup: BeautifulSoup) -> str:
    # Extracts a source document title from an already parsed tree so preparation never reparses the same HTML.
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(' ', strip=True)
    root = soup.body or soup
    heading = root.find(['h1', 'h2', 'h3'])
    return heading.get_text(' ', strip=True) if heading else ''
def _analyze_source_document(html_content: str, current_chapter_path: str | None = None, current_chapter_title: str | None = None, next_chapter_title: str | None = None):
    # Parses source HTML once and returns the canonical tree, text nodes, title and policy decisions used by all source preparation paths.
    from .preparation_metrics import record_preparation_event, timed_preparation_event
    if not html_content:
        return None, None, [], '', []
    with timed_preparation_event('source_dom_build'):
        soup = BeautifulSoup(html_content, 'html.parser')
    record_preparation_event('beautifulsoup_parse_source')
    root = soup.body or soup
    nodes = _source_text_nodes(root)
    texts = [str(node) for node in nodes]
    effective_title = current_chapter_title or _source_document_title_from_soup(soup)
    current_number = _extract_chapter_number(effective_title) or _extract_chapter_number(current_chapter_path or '')
    tail_start = find_source_next_chapter_tail_start(
        texts,
        current_chapter_title=effective_title,
        current_chapter_path=current_chapter_path,
        next_chapter_title=next_chapter_title,
    )
    title_index = None
    for index, node in enumerate(nodes):
        parent_name = getattr(getattr(node, 'parent', None), 'name', '')
        if parent_name not in {'h1', 'h2', 'h3'}:
            continue
        node_number = _extract_chapter_number(str(node))
        if current_number is None or node_number == current_number or _normalize_heading_key(str(node)) == _normalize_heading_key(effective_title):
            title_index = index
            break
    decisions = []
    last_visible_index = len(nodes) - 1
    for index, node in enumerate(nodes):
        text = str(node)
        if tail_start is not None and index >= tail_start:
            unit_kind, reason = SOURCE_UNIT_NEXT_CHAPTER_BOUNDARY, 'appended_next_chapter_tail'
        elif index == last_visible_index and is_source_tail_boilerplate_text(text):
            unit_kind, reason = SOURCE_UNIT_BOILERPLATE, 'tail_site_hint_label'
        elif is_source_boilerplate_text(text):
            unit_kind, reason = SOURCE_UNIT_BOILERPLATE, 'scraper_or_promotion_watermark'
        elif index == title_index:
            unit_kind, reason = SOURCE_UNIT_CURRENT_CHAPTER_TITLE, 'current_chapter_heading'
        else:
            unit_kind, reason = SOURCE_UNIT_CONTENT, 'narrative_content'
        decisions.append({
            'text_node_index': index,
            'text': text,
            'unit_kind': unit_kind,
            'classification_reason': reason,
            'translatable': unit_kind in TRANSLATABLE_SOURCE_UNIT_KINDS,
            'classification_confidence': 1.0,
        })
    return soup, root, nodes, effective_title, decisions
def get_source_document_title(html_content: str) -> str:
    # Returns source document title while keeping the legacy standalone API for callers that do not own a prepared snapshot.
    soup, _, _, effective_title, _ = _analyze_source_document(html_content)
    return effective_title if soup is not None else ''
def classify_source_text(text: str) -> dict:
    # Returns a legacy-compatible policy decision for one standalone source text without DOM context.
    if is_source_boilerplate_text(text):
        return {'kind': SOURCE_UNIT_BOILERPLATE, 'reason': 'scraper_or_site_boilerplate'}
    normalized = _normalize_source_noise_text(text)
    if _SOURCE_NEXT_CHAPTER_RE.fullmatch(normalized):
        return {'kind': SOURCE_UNIT_NEXT_CHAPTER_BOUNDARY, 'reason': 'explicit_next_chapter_marker'}
    return {'kind': SOURCE_UNIT_CONTENT, 'reason': 'translatable_content'}
def classify_source_text_nodes(html_content: str, current_chapter_path: str | None = None, current_chapter_title: str | None = None, next_chapter_title: str | None = None) -> list[dict]:
    # Classifies raw DOM text nodes through the single-pass source analyzer and preserves the legacy result shape.
    _, _, _, _, decisions = _analyze_source_document(
        html_content,
        current_chapter_path=current_chapter_path,
        current_chapter_title=current_chapter_title,
        next_chapter_title=next_chapter_title,
    )
    return decisions
def strip_nontranslatable_source_units(html_content: str, current_chapter_path: str | None = None, current_chapter_title: str | None = None, next_chapter_title: str | None = None, return_decisions: bool = False):
    # Removes proven non-translatable nodes from the same parsed tree that produced policy decisions.
    if not html_content:
        return (html_content, []) if return_decisions else html_content
    soup, root, nodes, _, decisions = _analyze_source_document(
        html_content,
        current_chapter_path=current_chapter_path,
        current_chapter_title=current_chapter_title,
        next_chapter_title=next_chapter_title,
    )
    node_index_by_id = {id(node): index for index, node in enumerate(nodes)}
    removed = []
    nontranslatable_indexes = {decision['text_node_index'] for decision in decisions if not decision['translatable']}
    for decision in decisions:
        index = decision['text_node_index']
        if index not in nontranslatable_indexes or index >= len(nodes):
            continue
        node = nodes[index]
        if getattr(node, 'parent', None) is None:
            continue
        block = node.find_parent(['p', 'li', 'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
        if block is not None:
            block_nodes = _source_text_nodes(block)
            block_indexes = {node_index_by_id[id(candidate)] for candidate in block_nodes if id(candidate) in node_index_by_id}
            if block_indexes and block_indexes.issubset(nontranslatable_indexes):
                block.decompose()
            else:
                node.extract()
        else:
            node.extract()
        removed.append(decision)
    cleaned = str(soup) if removed else html_content
    return (cleaned, removed) if return_decisions else cleaned
def build_source_snapshot(document_id: str, html_content: str, current_chapter_path: str | None = None, next_chapter_title: str | None = None) -> dict:
    # Builds one immutable-friendly source snapshot from a single DOM parse and reuses it for units, glossary, QA and recovery.
    if not html_content:
        return {'document_id': document_id, 'title': '', 'visible_text_nodes': (), 'source_units': (), 'translatable_source_units': ()}
    _, root, nodes, effective_title, decisions = _analyze_source_document(
        html_content,
        current_chapter_path=current_chapter_path,
        next_chapter_title=next_chapter_title,
    )
    units = []
    translation_order = 0
    for text_node_index, node in enumerate(nodes):
        decision = decisions[text_node_index]
        source_text = str(node)
        node_dom_path = _source_node_dom_path(root, node)
        char_start = 0
        piece_index = 0
        for piece in split_source_text_node(source_text):
            char_end = char_start + len(piece)
            if piece.strip():
                unit_index = len(units)
                is_translatable = decision['translatable']
                units.append({
                    'source_unit_id': f'{document_id}:{unit_index:06d}',
                    'raw_unit_id': f'{document_id}:raw:{text_node_index:06d}:{piece_index:04d}',
                    'unit_index': unit_index,
                    'text_node_index': text_node_index,
                    'piece_index': piece_index,
                    'char_start': char_start,
                    'char_end': char_end,
                    'source_text': piece,
                    'source_text_hash': hashlib.sha256(piece.encode('utf-8')).hexdigest(),
                    'dom_path': f'{node_dom_path}#piece={piece_index}',
                    'unit_kind': decision['unit_kind'],
                    'classification_reason': decision['classification_reason'],
                    'classification_confidence': decision['classification_confidence'],
                    'translation_order': translation_order if is_translatable else None,
                })
                if is_translatable:
                    translation_order += 1
                piece_index += 1
            char_start = char_end
    translatable = tuple(unit for unit in units if unit['unit_kind'] in TRANSLATABLE_SOURCE_UNIT_KINDS)
    return {
        'document_id': document_id,
        'title': effective_title,
        'visible_text_nodes': tuple(str(node) for node in nodes),
        'source_units': tuple(units),
        'translatable_source_units': translatable,
    }
def build_source_units(document_id: str, html_content: str, current_chapter_path: str | None = None, next_chapter_title: str | None = None) -> list[dict]:
    # Returns canonical source units from the unified single-pass source snapshot.
    return list(build_source_snapshot(document_id, html_content, current_chapter_path=current_chapter_path, next_chapter_title=next_chapter_title)['source_units'])
def select_translatable_source_units(document_id: str, html_content: str, current_chapter_path: str | None = None, next_chapter_title: str | None = None) -> list[dict]:
    # Выбирает title/content subset immutable units и сохраняет gaps в raw source identity.
    return [
        unit for unit in build_source_units(document_id, html_content, current_chapter_path=current_chapter_path, next_chapter_title=next_chapter_title)
        if unit['unit_kind'] in TRANSLATABLE_SOURCE_UNIT_KINDS
    ]
