"""Safe, read-only book import inspection for the Chaptera team API.

No database writes, outbound requests, file extraction or execution. This is the
first half of the import workflow; an explicit selection/revision-aware commit
must be implemented before any uploaded chapters can become canonical sources.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, NavigableString, Tag

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4000
MAX_CHAPTERS = 2000
MAX_CHAPTER_CHARACTERS = 2_000_000
MAX_BOOKS_IN_CATALOG = 2000
MAX_CATALOG_CHAPTERS = 50000

# Only anchored, unmistakable headings. Do not infer chapter numbers from
# ordinary prose, filenames or the surrounding DOM.
HEADING = re.compile(
    r"^(?:(?:глава|chapter)\s*#?\s*(?P<arabic>\d{1,7})\b(?P<suffix>[\s.\-—–:：].*)?"
    r"|第\s*(?P<cjk>[零〇一二三四五六七八九十百千两兩\d]+)\s*[章节節回话話](?P<cjk_suffix>.*))$",
    re.IGNORECASE,
)
MD_TITLE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
ZIP_TYPES = {'.epub', '.docx'}


class ImportFormatError(ValueError):
    """Reject unsupported, ambiguous, corrupt or unsafe uploaded sources."""


@dataclass(frozen=True)
class Candidate:
    title: str
    number: int | None
    html: str
    path: str


def _filename(name: str) -> str:
    result = str(name or '').replace('\\', '/').rsplit('/', 1)[-1].strip()
    if not result or result in {'.', '..'}:
        raise ImportFormatError('A file name with a supported extension is required')
    return result[:255]


def _decode(data: bytes) -> str:
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        try:
            return data.decode('utf-16')
        except UnicodeDecodeError as exc:
            raise ImportFormatError('Invalid UTF-16 text') from exc
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            return data.decode('cp1251')
        except UnicodeDecodeError as exc:
            raise ImportFormatError('Text encoding is not supported') from exc


def _cjk_integer(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '兩': 2, '三': 3,
              '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    scales = {'十': 10, '百': 100, '千': 1000}
    result, digit = 0, 0
    for char in value:
        if char in digits:
            digit = digits[char]
        elif char in scales:
            result += (digit or 1) * scales[char]
            digit = 0
        else:
            return None
    return result + digit


def _heading(text: str) -> tuple[str, int] | None:
    text = re.sub(r'\s+', ' ', text).strip()
    markdown = MD_TITLE.fullmatch(text)
    if markdown:
        text = markdown.group(1)
    if len(text) > 180:
        return None
    match = HEADING.fullmatch(text)
    if not match:
        return None
    number = int(match.group('arabic')) if match.group('arabic') else _cjk_integer(match.group('cjk'))
    if number is None or number < 1 or number > 9999999:
        return None
    return text, number


def _paragraphs_to_html(lines: list[str]) -> str:
    groups, pending = [], []
    for line in lines:
        if not line.strip():
            if pending:
                groups.append(pending)
                pending = []
        else:
            pending.append(line)
    if pending:
        groups.append(pending)
    return '\n'.join('<p>' + '<br/>'.join(html.escape(line.strip()) for line in group) + '</p>'
                     for group in groups)


def _text(html_source: str) -> str:
    return BeautifulSoup(html_source, 'html.parser').get_text(' ', strip=True)


def _from_lines(text: str, title: str, source_path: str) -> tuple[list[Candidate], str]:
    candidates: list[Candidate] = []
    prefix: list[str] = []
    body: list[str] = []
    current: tuple[str, int] | None = None
    found_heading = False
    for line in text.splitlines():
        match = _heading(line)
        if match:
            found_heading = True
            if current is not None:
                candidates.append(Candidate(current[0], current[1], _paragraphs_to_html(body), source_path))
            elif body:
                prefix = body
            current, body = match, []
        else:
            body.append(line)
    if current is not None:
        candidates.append(Candidate(current[0], current[1], _paragraphs_to_html(body), source_path))
    if not found_heading:
        candidates = [Candidate(title, None, _paragraphs_to_html(body), source_path)]
    return candidates, _paragraphs_to_html(prefix)


def _sanitize_markup(node: Tag) -> str:
    # Do not include scripts, event handlers, remote images or active links in
    # an import candidate; actual canonical HTML storage is a separate step.
    soup = BeautifulSoup(str(node), 'html.parser')
    for bad in soup.find_all(['script', 'style', 'iframe', 'object', 'embed', 'form', 'svg', 'math', 'img']):
        bad.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith('on') or attr.lower() in {'src', 'href', 'srcset', 'style', 'formaction'}:
                del tag.attrs[attr]
    return str(soup)


def _block_children(root: Tag):
    """Descend through layout wrappers only when they contain chapter headings."""
    for child in root.children:
        if isinstance(child, NavigableString):
            if child.strip():
                yield '<p>' + html.escape(str(child)) + '</p>', None
            continue
        if not isinstance(child, Tag):
            continue
        if child.name in {'script', 'style', 'nav', 'noscript', 'header', 'footer', 'iframe', 'form'}:
            continue
        if child.name in {'div', 'section', 'article', 'main'} and child.find(
            lambda t: isinstance(t, Tag) and t.name in {'h1', 'h2', 'h3', 'h4'} and _heading(t.get_text(' ', strip=True)) is not None
        ):
            yield from _block_children(child)
        else:
            mark = _heading(child.get_text(' ', strip=True)) if child.name in {'h1', 'h2', 'h3', 'h4'} else None
            yield _sanitize_markup(child), mark


def _from_html(source: str, title: str, source_path: str) -> tuple[list[Candidate], str]:
    soup = BeautifulSoup(source, 'html.parser')
    container = soup.body or soup
    candidates: list[Candidate] = []
    prefix: list[str] = []
    body: list[str] = []
    current: tuple[str, int] | None = None
    for fragment, heading in _block_children(container):
        if heading is not None:
            if current is not None:
                candidates.append(Candidate(current[0], current[1], '\n'.join(body), source_path))
            elif body:
                prefix = body
            current, body = heading, []
        else:
            body.append(fragment)
    if current is not None:
        candidates.append(Candidate(current[0], current[1], '\n'.join(body), source_path))
    if not candidates:
        return [Candidate(title, None, '\n'.join(body), source_path)], ''
    return candidates, '\n'.join(prefix)


def _checked_zip(data: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ImportFormatError('Too many archive entries')
        total = 0
        for item in infos:
            norm = item.filename.replace('\\', '/')
            if norm.startswith('/') or '..' in norm.split('/') or '\x00' in norm:
                raise ImportFormatError('Archive contains an unsafe path')
            if item.flag_bits & 1:
                raise ImportFormatError('Encrypted archives are not supported')
            if not item.is_dir():
                total += item.file_size
                if (item.file_size > MAX_ARCHIVE_EXPANDED_BYTES or total > MAX_ARCHIVE_EXPANDED_BYTES
                        or item.file_size > max(1024, item.compress_size) * 250):
                    raise ImportFormatError('Archive exceeds decompression safety limits')
        return zf
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        raise ImportFormatError('Invalid ZIP-based book') from exc


def _archive_member(zf: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = zf.getinfo(name)
        if info.file_size > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ImportFormatError('Document is too large')
        with zf.open(info) as fh:
            return fh.read(info.file_size + 1)
    except (KeyError, RuntimeError, zipfile.BadZipFile, OSError) as exc:
        raise ImportFormatError(f'Archive document is missing or corrupt: {name[:100]}') from exc


def _xml_document(raw: bytes) -> ET.Element:
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ImportFormatError('XML external entities and DTD are forbidden')
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ImportFormatError('Invalid document XML') from exc


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _epub(data: bytes, fallback_title: str) -> tuple[str, list[Candidate], str]:
    with _checked_zip(data) as zf:
        container = _xml_document(_archive_member(zf, 'META-INF/container.xml'))
        opf_name = next((e.attrib.get('full-path') for e in container.iter() if _local(e.tag) == 'rootfile'), None)
        if not opf_name or opf_name not in zf.namelist():
            raise ImportFormatError('EPUB package path is missing')
        opf = _xml_document(_archive_member(zf, opf_name))
        title = next((e.text.strip() for e in opf.iter() if _local(e.tag) == 'title' and e.text and e.text.strip()), fallback_title)
        manifest = {}
        for e in opf.iter():
            if _local(e.tag) == 'item' and e.get('id'):
                manifest[e.get('id')] = e.attrib
        spine = [e.get('idref') for e in opf.iter() if _local(e.tag) == 'itemref']
        if not spine:
            raise ImportFormatError('EPUB spine has no readable chapters')
        chapters: list[Candidate] = []
        prefixes: list[str] = []
        for ref in spine:
            item = manifest.get(ref or '')
            if not item or 'nav' in (item.get('properties') or '').split():
                continue
            if item.get('media-type') not in {'application/xhtml+xml', 'text/html'}:
                continue
            href = unquote(item.get('href', '').split('#', 1)[0])
            name = posixpath.normpath(posixpath.join(posixpath.dirname(opf_name), href))
            if name.startswith('../') or name.startswith('/') or not name or name not in zf.namelist():
                raise ImportFormatError('EPUB spine points outside the archive')
            part, prefix = _from_html(_decode(_archive_member(zf, name)), PurePath(name).stem, name)
            chapters.extend(part)
            if prefix.strip():
                prefixes.append(prefix)
            if len(chapters) > MAX_CHAPTERS:
                raise ImportFormatError('Too many chapters in a book')
        if not chapters:
            raise ImportFormatError('EPUB contains no readable chapters')
        return title, chapters, '\n'.join(prefixes)


def _docx(data: bytes, fallback_title: str) -> tuple[str, list[Candidate], str]:
    ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    with _checked_zip(data) as zf:
        document = _xml_document(_archive_member(zf, 'word/document.xml'))
        text_lines: list[str] = []
        for p in document.iter(ns + 'p'):
            tokens = []
            for t in p.iter():
                if t.tag == ns + 't' and t.text:
                    tokens.append(t.text)
                elif t.tag == ns + 'tab':
                    tokens.append('\t')
                elif t.tag in {ns + 'br', ns + 'cr'}:
                    tokens.append('\n')
            text_lines.append(''.join(tokens))
        if not text_lines or not any(line.strip() for line in text_lines):
            raise ImportFormatError('DOCX has no readable text')
        title = fallback_title
        if 'docProps/core.xml' in zf.namelist():
            core = _xml_document(_archive_member(zf, 'docProps/core.xml'))
            title = next((x.text.strip() for x in core.iter() if _local(x.tag) == 'title' and x.text and x.text.strip()), title)
        candidates, prefix = _from_lines('\n'.join(text_lines), title, 'word/document.xml')
        return title, candidates, prefix


def _catalog(raw: bytes) -> dict:
    try:
        data = json.loads(_decode(raw))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ImportFormatError('Invalid metadata JSON') from exc
    if not isinstance(data, dict) or data.get('format') != 'rulate_account_metadata' or data.get('schema_version') != 1:
        raise ImportFormatError('Expected Rulate account metadata schema v1')
    books = data.get('books')
    if not isinstance(books, list) or len(books) > MAX_BOOKS_IN_CATALOG:
        raise ImportFormatError('Metadata catalog book limit exceeded')
    output, total = [], 0
    for item in books:
        if not isinstance(item, dict) or not isinstance(item.get('chapters'), list):
            raise ImportFormatError('Invalid book entry in metadata catalog')
        total += len(item['chapters'])
        if total > MAX_CATALOG_CHAPTERS:
            raise ImportFormatError('Metadata catalog chapter limit exceeded')
        chapters = []
        for chapter in item['chapters']:
            if not isinstance(chapter, dict):
                raise ImportFormatError('Invalid chapter entry in metadata catalog')
            raw_num = chapter.get('number')
            if raw_num is not None and (type(raw_num) is not int or raw_num < 1):
                raise ImportFormatError('Invalid chapter number in metadata catalog')
            chapters.append({'number': raw_num, 'title': str(chapter.get('title') or '')[:500],
                             'url': str(chapter.get('url') or '')[:2048], 'has_text': False,
                             'rulate_chapter_id': chapter.get('rulate_chapter_id')})
        output.append({'rulate_book_id': item.get('rulate_book_id'), 'title': str(item.get('title') or '')[:500],
                       'url': str(item.get('url') or '')[:2048], 'source': str(item.get('source') or '')[:2048] or None,
                       'declared_chapters': item.get('chapter_count'),
                       'toc_complete': item.get('toc_complete') is True,
                       'chapters': chapters})
    return {'kind': 'metadata_catalog', 'format': 'rulate_account_metadata', 'schema_version': 1,
            'book_count': len(output), 'chapter_count': total, 'books': output,
            'warnings': ['Metadata only: no source or translated chapter text is present. Nothing has been stored.']}


def preview_book_import(filename: str, data: bytes) -> dict:
    """Parse without persistent mutations and return safe outline + bounded samples."""
    filename = _filename(filename)
    if not data:
        raise ImportFormatError('Uploaded file is empty')
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImportFormatError('Uploaded file exceeds 12 MiB limit')
    suffix = '.' + filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    fallback_title = filename[:-(len(suffix))] if suffix else filename
    if suffix == '.json':
        return _catalog(data)
    if suffix == '.txt':
        title, (candidates, prefix) = fallback_title, _from_lines(_decode(data), fallback_title, filename)
    elif suffix in {'.html', '.htm', '.xhtml'}:
        title, (candidates, prefix) = fallback_title, _from_html(_decode(data), fallback_title, filename)
    elif suffix == '.epub':
        title, candidates, prefix = _epub(data, fallback_title)
    elif suffix == '.docx':
        title, candidates, prefix = _docx(data, fallback_title)
    else:
        raise ImportFormatError('Supported formats: TXT, HTML, EPUB, DOCX, and Rulate metadata JSON')
    if not candidates or len(candidates) > MAX_CHAPTERS:
        raise ImportFormatError('Book has no chapters or exceeds the chapter limit')
    numbered, output, warnings = set(), [], []
    for order, chapter in enumerate(candidates, start=1):
        source_text = _text(chapter.html)
        if len(source_text) > MAX_CHAPTER_CHARACTERS:
            raise ImportFormatError(f'Chapter {order} exceeds the per-chapter limit')
        if not source_text:
            warnings.append(f'Chapter {order} has no readable text')
        if chapter.number is not None and chapter.number in numbered:
            warnings.append(f'Duplicate chapter number {chapter.number}; manual mapping required')
        if chapter.number is not None:
            numbered.add(chapter.number)
        output.append({'sequence': order, 'detected_number': chapter.number,
                       'suggested_index': chapter.number or order, 'title': chapter.title[:500],
                       'source_path': chapter.path[:1024], 'characters': len(source_text),
                       'sha256': hashlib.sha256(source_text.encode('utf-8')).hexdigest(),
                       'sample': source_text[:180], 'has_text': bool(source_text)})
    if not all(chapter.number is not None for chapter in candidates):
        warnings.append('Some chapters have no explicit number: sequence numbers are suggestions only')
    if _text(prefix):
        warnings.append('Text before the first chapter heading is unassigned and needs review')
    return {'kind': 'book', 'format': suffix.lstrip('.'), 'filename': filename,
            'title': title[:500], 'chapter_count': len(output), 'chapters': output,
            'unassigned_prefix_characters': len(_text(prefix)),
            'unassigned_prefix_sample': _text(prefix)[:180], 'warnings': warnings,
            'stored': False}
