"""Offline and HTTP contract fixtures for independent book import preview."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.book_import import ImportFormatError, MAX_UPLOAD_BYTES, preview_book_import
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore


def _make_zip(files: dict[str, str | bytes]) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return data.getvalue()


def _epub() -> bytes:
    return _make_zip({
        'mimetype': 'application/epub+zip',
        'META-INF/container.xml': '''<container><rootfiles>
            <rootfile full-path="OPS/content.opf"/></rootfiles></container>''',
        'OPS/content.opf': '''<package xmlns:dc="http://purl.org/dc/elements/1.1/">
            <metadata><dc:title>Облачный роман</dc:title></metadata>
            <manifest>
              <item id="nav" href="toc.xhtml" media-type="application/xhtml+xml" properties="nav"/>
              <item id="p2" href="two.xhtml" media-type="application/xhtml+xml"/>
              <item id="p1" href="one.xhtml" media-type="application/xhtml+xml"/>
            </manifest><spine><itemref idref="p1"/><itemref idref="p2"/></spine></package>''',
        'OPS/toc.xhtml': '<html><body><p>Navigation, never a chapter</p></body></html>',
        'OPS/one.xhtml': '<html><body><h1>Глава 517. Начало</h1><p>始まり</p></body></html>',
        'OPS/two.xhtml': '<html><body><h2>Глава 518. Финал</h2><p>終わり</p></body></html>',
    })


def _docx() -> bytes:
    return _make_zip({
        'word/document.xml': '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
         <w:body><w:p><w:r><w:t>Глава 11. Герой</w:t></w:r></w:p>
         <w:p><w:r><w:t>Текст</w:t></w:r><w:r><w:t> главы</w:t></w:r></w:p>
         <w:p><w:r><w:t>Глава 12. Финал</w:t></w:r></w:p>
         <w:p><w:r><w:t>Вторая глава</w:t></w:r></w:p></w:body></w:document>''',
        'docProps/core.xml': '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Книга Word</dc:title></cp:coreProperties>',
    })


def test_russian_txt_markup_numbering_and_prefix_remains_visible():
    text = '''Описание книги.
Не пропадать молча.

# Глава 517. Возвращение

Первый абзац.
Вторая строка.

## Глава 518: Победа

Последняя глава.
'''
    result = preview_book_import('Проба.txt', text.encode('utf-8'))
    assert result['kind'] == 'book'
    assert result['title'] == 'Проба'
    assert result['chapter_count'] == 2
    assert [c['detected_number'] for c in result['chapters']] == [517, 518]
    assert 'Первый абзац' in result['chapters'][0]['sample']
    assert result['chapters'][0]['sha256'] != result['chapters'][1]['sha256']
    assert result['unassigned_prefix_characters'] > 0
    assert any('unassigned' in x for x in result['warnings'])
    assert result['stored'] is False


def test_unnumbered_plain_text_remains_a_single_chapter_without_guessing():
    result = preview_book_import('source.txt', b'First paragraph\n\nSecond paragraph')
    assert result['chapter_count'] == 1
    assert result['chapters'][0]['detected_number'] is None
    assert result['chapters'][0]['suggested_index'] == 1


def test_utf16_and_legacy_cp1251_and_cjk_numbered_headings():
    russian = 'Глава 21. Название\nСтрока'.encode('utf-16')
    assert preview_book_import('legacy.txt', russian)['chapters'][0]['detected_number'] == 21
    cp = 'Глава 7. История\nТекст главы'.encode('cp1251')
    assert preview_book_import('legacy.txt', cp)['chapters'][0]['detected_number'] == 7
    cjk = '第一百零二章 旅程\n山河\n\n第103話 旅人\n第二段'
    assert [c['detected_number'] for c in preview_book_import('book.txt', cjk.encode())['chapters']] == [102, 103]


def test_html_nested_sections_strip_active_content_and_count_chapters():
    body = '''<html><head><title>Ignore</title></head><body><main>
    <div><h1>Глава 1 — Встреча</h1><p>文本 <strong>жирный</strong></p>
    <script>alert(1)</script><p onclick="evil()">Осталось</p></div>
    <section><h2>Chapter 2: The End</h2><p>конец</p></section></main></body></html>'''
    result = preview_book_import('novel.html', body.encode())
    assert result['chapter_count'] == 2
    assert result['chapters'][0]['detected_number'] == 1
    assert result['chapters'][1]['detected_number'] == 2
    assert 'alert' not in result['chapters'][0]['sample']
    assert result['chapters'][0]['characters'] > 1


def test_epub_spine_order_and_title_and_nav_exclusion():
    result = preview_book_import('notebook.epub', _epub())
    assert result['title'] == 'Облачный роман'
    assert result['chapter_count'] == 2
    assert [chapter['detected_number'] for chapter in result['chapters']] == [517, 518]
    assert result['chapters'][0]['source_path'] == 'OPS/one.xhtml'
    assert 'Navigation' not in str(result)


def test_docx_reads_word_paragraphs_and_core_title():
    result = preview_book_import('story.docx', _docx())
    assert result['title'] == 'Книга Word'
    assert [c['detected_number'] for c in result['chapters']] == [11, 12]
    assert 'Текст главы' in result['chapters'][0]['sample']


def test_duplicate_chapter_numbers_warn_and_never_silently_renumber():
    result = preview_book_import('duplicate.txt', 'Глава 1\nОдин\nГлава 1\nДва'.encode())
    assert result['chapter_count'] == 2
    assert [c['suggested_index'] for c in result['chapters']] == [1, 1]
    assert any('Duplicate chapter number 1' in w for w in result['warnings'])


def test_metadata_catalog_compatible_with_windows_exporter_and_no_text():
    source = {'format': 'rulate_account_metadata', 'schema_version': 1,
              'account': {'login': 'Atarax'}, 'books': [
                  {'rulate_book_id': 1881, 'title': 'Старое', 'url': 'https://tl.rulate.ru/book/1881',
                   'source': 'https://original.test/novel', 'chapter_count': 2, 'toc_complete': False,
                   'chapters': [
                       {'number': None, 'title': 'Преамбула', 'url': 'https://tl.rulate.ru/book/1881/1', 'rulate_chapter_id': 1},
                       {'number': 2, 'title': 'Глава 2', 'url': 'https://tl.rulate.ru/book/1881/2', 'rulate_chapter_id': 2},
                   ]}
              ]}
    result = preview_book_import('Atarax.json', json.dumps(source, ensure_ascii=False).encode())
    assert result['kind'] == 'metadata_catalog'
    assert result['book_count'] == 1 and result['chapter_count'] == 2
    assert result['books'][0]['source'] == 'https://original.test/novel'
    assert result['books'][0]['chapters'][0]['number'] is None
    assert not any(c['has_text'] for c in result['books'][0]['chapters'])
    assert 'account' not in result
    assert 'metadata' in ' '.join(result['warnings']).lower()


def test_catalog_invalid_schema_invalid_numbers_and_prose_json_rejected():
    with pytest.raises(ImportFormatError, match='schema'):
        preview_book_import('invalid.json', b'{"books": []}')
    bad = {'format': 'rulate_account_metadata', 'schema_version': 1,
           'books': [{'title': 'Book', 'chapters': [{'number': True}]}]}
    with pytest.raises(ImportFormatError, match='number'):
        preview_book_import('invalid.json', json.dumps(bad).encode())


def test_unsafe_archive_paths_and_xml_entities_rejected():
    archive = _make_zip({'../unsafe': b'something', 'word/document.xml': b'<w:document />'})
    with pytest.raises(ImportFormatError, match='unsafe'):
        preview_book_import('bad.docx', archive)
    entity = _make_zip({'word/document.xml': '<!DOCTYPE foo [<!ENTITY x "boom">]><document>&x;</document>'})
    with pytest.raises(ImportFormatError, match='DTD'):
        preview_book_import('entity.docx', entity)


def test_invalid_extension_invalid_zip_and_empty_upload_rejected():
    with pytest.raises(ImportFormatError, match='Supported'):
        preview_book_import('bad.exe', b'binary')
    with pytest.raises(ImportFormatError, match='Invalid ZIP'):
        preview_book_import('bad.epub', b'not a zip')
    with pytest.raises(ImportFormatError, match='empty'):
        preview_book_import('empty.txt', b'')


def test_large_unbounded_file_is_rejected_before_parsing():
    with pytest.raises(ImportFormatError, match='12 MiB'):
        preview_book_import('large.txt', b'a' * (MAX_UPLOAD_BYTES + 1))


def test_http_upload_preview_auth_and_zero_db_side_effects(tmp_path):
    store = QueueStore(tmp_path / 'jobs.sqlite3')
    app = create_app(Settings(db_path=store.db_path, team_tokens={'alice': 'a-secret', 'bob': 'b-secret'}), store)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            upload = {'file': ('sample.txt', 'Глава 3\nТекст\nГлава 4\nЕщё текст'.encode(), 'text/plain')}
            assert (await client.post('/v1/imports/preview', files=upload)).status_code == 401
            response = await client.post('/v1/imports/preview', files=upload, headers={'X-API-Key': 'a-secret'})
            assert response.status_code == 200, response.text
            assert [c['detected_number'] for c in response.json()['chapters']] == [3, 4]
            assert response.json()['stored'] is False
            assert (await client.post('/v1/imports/preview', files={'file': ('x.exe', b'code')}, headers={'X-API-Key': 'a-secret'})).status_code == 422
            assert (await client.post('/v1/imports/preview', files={'file': ('large.txt', b'A' * (MAX_UPLOAD_BYTES + 1))}, headers={'X-API-Key': 'a-secret'})).status_code == 413
            # Preview never creates books, chapters, jobs or accepted translations.
            assert (await client.get('/v1/jobs', headers={'X-API-Key': 'b-secret'})).json()['jobs'] == []
            with store._connect() as db:
                assert db.execute('SELECT COUNT(*) FROM books').fetchone()[0] == 0
                assert db.execute('SELECT COUNT(*) FROM book_chapters').fetchone()[0] == 0
    asyncio.run(scenario())
