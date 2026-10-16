from __future__ import annotations
import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict, Counter
from dataclasses import dataclass
from typing import Any, Callable
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from chaptera_translation_core.utils.preparation_metrics import record_preparation_event, timed_preparation_event
from chaptera_translation_core.utils.source_units import TRANSLATABLE_SOURCE_UNIT_KINDS, build_source_snapshot
SOURCE_PREPARER_VERSION = 1
PROVIDER_PREPARER_VERSION = 1
GLOSSARY_PREPARER_VERSION = 1
TARGET_PREPARER_VERSION = 1
@dataclass(frozen=True, slots=True)
class PreparedSourceChapter:
    document_id: str
    original_path: str
    source_hash: str
    title: str
    visible_text_nodes: tuple[str, ...]
    source_units: tuple[dict, ...]
    translatable_source_units: tuple[dict, ...]
@dataclass(frozen=True, slots=True)
class PreparedProviderChunk:
    source_hash: str
    marked_source_html: str
    content_with_placeholders: str
    provider_units: tuple[dict, ...]
    unit_count: int
    media_map: dict
    link_map: dict
@dataclass(frozen=True, slots=True)
class PreparedGlossaryContext:
    source_hash: str
    context_revision: int
    chapter_scope: tuple[str, ...]
    advisory_limit: int
    compiled: dict
@dataclass(frozen=True, slots=True)
class PreparedTargetSnapshot:
    target_hash: str
    visible_text_nodes: tuple[str, ...]
    target_slots: tuple[dict, ...]
    slot_signature: tuple[tuple[tuple[int, ...], str, int, int, str], ...]
    combined_text: str
    cjk_residue: tuple[str, ...]
class _PreparationCache:
    def __init__(self) -> None:
        # Creates bounded process-local LRU stores plus single-flight coordination for concurrent workers.
        self._lock = threading.RLock()
        self._stores: dict[str, OrderedDict[Any, Any]] = {
            'source': OrderedDict(),
            'provider': OrderedDict(),
            'glossary': OrderedDict(),
            'target': OrderedDict(),
        }
        self._limits = {'source': 128, 'provider': 256, 'glossary': 256, 'target': 128}
        self._flights: dict[tuple[str, Any], threading.Event] = {}
        self._stats: Counter[str] = Counter()
        self._build_ms: Counter[str] = Counter()
    def get_or_build(self, kind: str, key: Any, builder: Callable[[], Any]) -> Any:
        # Returns one cached immutable snapshot or lets exactly one worker build the missing key at a time.
        flight_key = (kind, key)
        while True:
            owner = False
            with self._lock:
                store = self._stores[kind]
                if key in store:
                    value = store.pop(key)
                    store[key] = value
                    self._stats[f'{kind}.hit'] += 1
                    record_preparation_event(f'{kind}_cache_hit')
                    return value
                event = self._flights.get(flight_key)
                if event is None:
                    event = threading.Event()
                    self._flights[flight_key] = event
                    owner = True
                    self._stats[f'{kind}.miss'] += 1
                    record_preparation_event(f'{kind}_cache_miss')
            if owner:
                started = time.perf_counter()
                try:
                    value = builder()
                except Exception:
                    with self._lock:
                        self._stats[f'{kind}.build_error'] += 1
                        self._flights.pop(flight_key, None)
                        event.set()
                    raise
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                with self._lock:
                    store = self._stores[kind]
                    store[key] = value
                    store.move_to_end(key)
                    while len(store) > self._limits[kind]:
                        store.popitem(last=False)
                        self._stats[f'{kind}.eviction'] += 1
                    self._stats[f'{kind}.build'] += 1
                    self._build_ms[kind] += elapsed_ms
                    self._flights.pop(flight_key, None)
                    event.set()
                record_preparation_event(f'{kind}_cache_build', duration_ms=elapsed_ms)
                return value
            event.wait()
    def clear(self) -> None:
        # Clears bounded session snapshots and session-local cache counters while leaving durable source_units and persisted performance history untouched.
        with self._lock:
            for store in self._stores.values():
                store.clear()
            self._flights.clear()
            self._stats.clear()
            self._build_ms.clear()
    def metrics(self) -> dict[str, Any]:
        # Returns cache hit/miss/build/eviction counters and current entry counts for diagnostics and regression tests.
        with self._lock:
            result = dict(self._stats)
            for kind, store in self._stores.items():
                result[f'{kind}.entries'] = len(store)
                result[f'{kind}.build_ms'] = float(self._build_ms.get(kind, 0.0))
            return result
_CACHE = _PreparationCache()
def _sha256_text(value: str) -> str:
    # Creates a stable cross-process cache key instead of using Python's randomized hash().
    return hashlib.sha256((value or '').encode('utf-8')).hexdigest()
def _source_document_id(original_path: str, source_hash: str) -> str:
    # Builds the same stable fallback identity shape for callers that do not have a persistent quality store document id.
    payload = f'{original_path or ""}\0{source_hash}'
    return 'prepared:' + hashlib.sha256(payload.encode('utf-8')).hexdigest()
def prepare_source_chapter(
    html_content: str,
    *,
    document_id: str | None = None,
    original_path: str = '',
    source_units: list[dict] | tuple[dict, ...] | None = None,
) -> PreparedSourceChapter:
    # Builds or reuses the canonical source snapshot shared by translation, glossary, semantic audit and recovery.
    source_hash = _sha256_text(html_content)
    effective_document_id = document_id or _source_document_id(original_path, source_hash)
    key = (SOURCE_PREPARER_VERSION, effective_document_id, source_hash)
    def _build() -> PreparedSourceChapter:
        # Reconstructs the immutable source snapshot from durable source_units when available and parses HTML only for a never-registered source.
        with timed_preparation_event('prepared_source_build'):
            if source_units is not None:
                units = tuple(dict(item) for item in source_units)
                pieces_by_node = {}
                node_order = []
                for unit in units:
                    node_index = int(unit.get('text_node_index') or 0)
                    if node_index not in pieces_by_node:
                        pieces_by_node[node_index] = []
                        node_order.append(node_index)
                    pieces_by_node[node_index].append((int(unit.get('piece_index') or 0), str(unit.get('source_text') or '')))
                visible_nodes = tuple(
                    ''.join(text for _, text in sorted(pieces_by_node[index]))
                    for index in node_order
                )
                title_parts = [
                    str(unit.get('source_text') or '')
                    for unit in units
                    if unit.get('unit_kind') == 'CURRENT_CHAPTER_TITLE'
                ]
                title = ''.join(title_parts)
            else:
                snapshot = build_source_snapshot(effective_document_id, html_content, current_chapter_path=original_path or None)
                units = tuple(dict(item) for item in snapshot['source_units'])
                visible_nodes = tuple(snapshot.get('visible_text_nodes') or ())
                title = str(snapshot.get('title') or '')
        translatable = tuple(item for item in units if item.get('unit_kind') in TRANSLATABLE_SOURCE_UNIT_KINDS)
        return PreparedSourceChapter(
            document_id=effective_document_id,
            original_path=original_path or '',
            source_hash=source_hash,
            title=title,
            visible_text_nodes=visible_nodes,
            source_units=units,
            translatable_source_units=translatable,
        )
    return _CACHE.get_or_build('source', key, _build)
def prepare_source_from_store(quality_store, original_path: str, source_content: str) -> PreparedSourceChapter:
    # Reuses durable source_units from TranslationQualityStore and avoids parsing an exact source snapshot twice in one or later sessions.
    document_id, units = quality_store.get_or_register_source_snapshot(original_path, source_content)
    return prepare_source_chapter(
        source_content,
        document_id=document_id,
        original_path=original_path,
        source_units=units,
    )
def _copy_placeholder_maps_to_builder(prompt_builder, prepared: PreparedProviderChunk) -> None:
    # Restores the exact media/link maps associated with a cached provider chunk before response parsing uses the builder state.
    prompt_builder.media_map = copy.deepcopy(prepared.media_map)
    prompt_builder.link_map = copy.deepcopy(prepared.link_map)
def prepare_provider_chunk(source_html: str, prompt_builder) -> PreparedProviderChunk:
    # Prepares TSEG markers, source atoms and media placeholders once per exact chunk and reuses them across retries and recovery.
    source_hash = _sha256_text(source_html)
    builder_name = f'{type(prompt_builder).__module__}.{type(prompt_builder).__qualname__}'
    key = (PROVIDER_PREPARER_VERSION, source_hash, builder_name)
    def _build() -> PreparedProviderChunk:
        # Builds the provider envelope with one marker parse and one placeholder parse, then freezes restoration maps for cache reuse.
        from chaptera_translation_core.core.worker_helpers.translation_completeness import build_translation_unit_source_bundle
        marked_source, provider_units = build_translation_unit_source_bundle(source_html)
        content_with_placeholders = prompt_builder._replace_media_with_placeholders(marked_source)
        prepared = PreparedProviderChunk(
            source_hash=source_hash,
            marked_source_html=marked_source,
            content_with_placeholders=content_with_placeholders,
            provider_units=tuple(dict(item) for item in provider_units),
            unit_count=len(provider_units),
            media_map=copy.deepcopy(getattr(prompt_builder, 'media_map', {}) or {}),
            link_map=copy.deepcopy(getattr(prompt_builder, 'link_map', {}) or {}),
        )
        return prepared
    prepared = _CACHE.get_or_build('provider', key, _build)
    _copy_placeholder_maps_to_builder(prompt_builder, prepared)
    return prepared
def context_revision(context_manager) -> int:
    # Returns the monotonic glossary revision maintained by ContextManager so cache keys stay cheap even for large glossaries.
    return int(getattr(context_manager, '_prepared_glossary_revision', 0) or 0)
def prepare_glossary_context(context_manager, source_html: str, current_chapters_list=None, advisory_limit: int = 24, *, compiler: Callable | None = None) -> PreparedGlossaryContext:
    # Compiles exact glossary bindings once for one source/scope/revision and shares the same authority object across prompt and QA.
    source_hash = _sha256_text(source_html)
    chapters = tuple(str(value) for value in (current_chapters_list or ()))
    revision = context_revision(context_manager)
    key = (GLOSSARY_PREPARER_VERSION, id(context_manager), revision, source_hash, chapters, int(advisory_limit), id(compiler))
    def _build() -> PreparedGlossaryContext:
        # Delegates to the uncached compiler exactly once and stores a detached result so callers cannot mutate the cache entry accidentally.
        if compiler is None:
            raise ValueError('glossary compiler is required; provide one through the dependency-injection port')
        compiled = compiler(context_manager, source_html, list(chapters), advisory_limit=advisory_limit)
        return PreparedGlossaryContext(
            source_hash=source_hash,
            context_revision=revision,
            chapter_scope=chapters,
            advisory_limit=int(advisory_limit),
            compiled=copy.deepcopy(compiled),
        )
    return _CACHE.get_or_build('glossary', key, _build)
def glossary_context_dict(context_manager, source_html: str, current_chapters_list=None, advisory_limit: int = 24, *, compiler: Callable | None = None) -> dict:
    # Returns a caller-owned copy of the cached glossary context to preserve the previous mutable dict API safely.
    prepared = prepare_glossary_context(context_manager, source_html, current_chapters_list, advisory_limit=advisory_limit, compiler=compiler)
    return copy.deepcopy(prepared.compiled)
def prepare_target_snapshot(html_content: str) -> PreparedTargetSnapshot:
    # Parses one exact Gemini target once and reuses visible slots, combined text and CJK residue across gates, localization and repair.
    target_hash = _sha256_text(html_content)
    key = (TARGET_PREPARER_VERSION, target_hash)
    def _build() -> PreparedTargetSnapshot:
        # Materializes only compact immutable target text data and intentionally drops the mutable BeautifulSoup tree after extraction.
        with timed_preparation_event('target_dom_build'):
            soup = BeautifulSoup(html_content or '', 'html.parser')
        record_preparation_event('beautifulsoup_parse_target')
        root = soup.body or soup
        nodes = tuple(
            str(node)
            for node in root.find_all(string=True)
            if not isinstance(node, Comment) and str(node).strip() and not node.find_parent(['script', 'style'])
        )
        slots = tuple({'target_slot_id': f'TGT_{index:06d}', 'text': text} for index, text in enumerate(nodes))
        slot_signature = []
        for node in root.find_all(string=True):
            if isinstance(node, Comment) or not str(node).strip() or node.find_parent(['script', 'style']):
                continue
            parent = node.parent
            if parent is None:
                continue
            path = []
            current = parent
            while current is not root:
                ancestor = current.parent
                if ancestor is None:
                    path = []
                    break
                tag_children = [child for child in ancestor.children if isinstance(child, Tag)]
                path.append(next((index for index, child in enumerate(tag_children) if child is current), 0))
                current = ancestor
            text_ordinal = 0
            for sibling in parent.children:
                if sibling is node:
                    break
                if isinstance(sibling, NavigableString) and not isinstance(sibling, Comment) and str(sibling).strip():
                    text_ordinal += 1
            text = str(node).strip()
            slot_signature.append((tuple(reversed(path)), getattr(parent, 'name', None) or '[document]', text_ordinal, len(text), text))
        combined = '\n'.join(nodes)
        import re
        cjk_re = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af\U00020000-\U0002EE5F\U0002F800-\U0002FA1F\U00030000-\U000323AF]')
        residue = tuple(cjk_re.findall(combined))
        return PreparedTargetSnapshot(
            target_hash=target_hash,
            visible_text_nodes=nodes,
            target_slots=slots,
            slot_signature=tuple(slot_signature),
            combined_text=combined,
            cjk_residue=residue,
        )
    return _CACHE.get_or_build('target', key, _build)
def clear_prepared_chapter_cache() -> None:
    # Clears the bounded RAM cache explicitly at session boundaries or in tests without deleting durable source units.
    _CACHE.clear()
def prepared_chapter_cache_metrics() -> dict[str, Any]:
    # Exposes current LRU hit/miss/build/eviction counters to diagnostics and performance tests.
    return _CACHE.metrics()
