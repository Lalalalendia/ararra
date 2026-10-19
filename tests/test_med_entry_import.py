"""MED chapter-entry classification: never conflate story, extras and service records."""
from __future__ import annotations

import pytest

from chaptera_server.book_import import parse_book_import, preview_book_import
from chaptera_server.import_sessions import ImportSessions, StageConflict
from chaptera_server.library import BookLibrary
from chaptera_server.storage import QueueStore


def test_med_txt_entry_types_author_numbers_and_bounded_internal_gaps():
    data = """Описание романа.

第1章 Начало
Первый сюжетный текст.

上架感言
Отдельное объявление автора.

第3章 Продолжение
Третий сюжетный текст.

番外：Вечер
Дополнительный рассказ.

请假条
Объявление о перерыве.
""".encode()
    preview, candidates, prefix = parse_book_import("med.txt", data)
    assert preview["chapter_count"] == 5
    assert preview["story_chapter_count"] == 2
    assert preview["extra_entry_count"] == 1
    assert preview["service_entry_count"] == 2
    assert preview["numbering_gaps"] == [[2, 2]]
    assert [c["catalog_ordinal"] for c in preview["chapters"]] == [1, 2, 3, 4, 5]
    assert [c["author_number"] for c in preview["chapters"]] == [1, None, 3, None, None]
    assert [c["entry_type"] for c in preview["chapters"]] == [
        "chapter", "service", "chapter", "extra", "service",
    ]
    assert [c.entry_type for c in candidates] == [
        "chapter", "service", "chapter", "extra", "service",
    ]
    assert "Описание романа" in prefix
    assert preview["stored"] is False
    assert any("explicit import or skip" in w for w in preview["warnings"])


def test_med_auxiliary_heading_never_matches_loose_prose():
    text = """第1章 Начало
Это не заголовок: 上架感言 описано в тексте.
番外世界并不是番外章节

第2章 Финал
Продолжение книги.
"""
    preview = preview_book_import("novel.txt", text.encode())
    assert preview["chapter_count"] == 2
    assert preview["service_entry_count"] == 0
    assert preview["extra_entry_count"] == 0
    assert preview["numbering_gaps"] == []


def test_med_html_auxiliary_headings_are_structural_not_page_text():
    html = """<html><body><section><h2>第8章 Последняя</h2><p>Текст</p>
<h2>番外：Продолжение</h2><p>Дополнение</p>
<h2>完本感言</h2><p>Слово автора</p></section></body></html>"""
    preview = preview_book_import("book.html", html.encode())
    assert [c["entry_type"] for c in preview["chapters"]] == [
        "chapter", "extra", "service",
    ]
    assert [c["author_number"] for c in preview["chapters"]] == [8, None, None]


def test_med_duplicate_bodies_are_flagged_but_originals_are_unchanged():
    preview = preview_book_import("novel.txt", (
        "第5章 A\nОдинаковый текст.\n\n第6章 B\nОдинаковый текст.\n"
    ).encode())
    assert preview["duplicate_body_pairs"] == [
        {"first_sequence": 1, "duplicate_sequence": 2}
    ]
    assert any("Duplicate story bodies" in w for w in preview["warnings"])


def test_med_huge_author_labels_do_not_expand_missing_ranges():
    preview = preview_book_import("large.txt", (
        "Глава 5000000. A\nПервый текст.\n"
        "Глава 9999999. B\nВторой текст.\n"
    ).encode())
    assert preview["numbering_gaps"] == [[5000001, 9999998]]
    assert len(preview["warnings"]) < 10


def test_med_stage_requires_every_decision_and_preserves_explicit_aux_heading(tmp_path):
    store = QueueStore(tmp_path / "med.sqlite3")
    library = BookLibrary(store)
    sessions = ImportSessions(store)
    raw = """第1章 Начало
Текст основной главы.

番外：Письмо
Текст дополнительной главы.

上架感言
Речь автора.
""".encode()
    staged = sessions.stage("member", "novel.txt", raw)
    assert [c["entry_type"] for c in staged["preview"]["chapters"]] == [
        "chapter", "extra", "service",
    ]
    base = {"book_id": "med-book", "book_mode": "create",
            "title": "MED demo", "prefix_policy": "reject"}
    choices = [
        {"sequence": 1, "action": "create", "chapter_id": "first",
         "chapter_index": 1, "chapter_path": "chapters/1.xhtml", "expected_revision": 0},
        {"sequence": 2, "action": "skip"},
        {"sequence": 3, "action": "create", "chapter_id": "author-note",
         "chapter_index": 99, "chapter_path": "chapters/99.xhtml", "expected_revision": 0},
    ]
    with pytest.raises(StageConflict, match="Every staged"):
        sessions.confirm("member", staged["stage_id"], {**base, "chapters": choices[:2]})
    committed = sessions.confirm("member", staged["stage_id"], {**base, "chapters": choices})
    assert (committed["imported_count"], committed["skipped_count"]) == (2, 1)
    assert "上架感言" in library.get_chapter("med-book", "author-note")["source_html"]
    assert "Речь автора" in library.get_chapter("med-book", "author-note")["source_html"]
    assert "第1章 Начало" in library.get_chapter("med-book", "first")["source_html"]


def test_med_empty_service_marker_can_be_staged_and_explicitly_skipped(tmp_path):
    store = QueueStore(tmp_path / "empty-aux.sqlite3")
    sessions = ImportSessions(store)
    staged = sessions.stage("member", "book.txt",
                            "Глава 1\nТекст.\n\n请假条".encode())
    assert staged["preview"]["service_entry_count"] == 1
    choices = [
        {"sequence": 1, "action": "create", "chapter_id": "first",
         "chapter_index": 1, "chapter_path": "chapters/1.xhtml", "expected_revision": 0},
        {"sequence": 2, "action": "skip"},
    ]
    result = sessions.confirm("member", staged["stage_id"],
                              {"book_id": "book", "book_mode": "create",
                               "title": "Book", "prefix_policy": "reject", "chapters": choices})
    assert (result["imported_count"], result["skipped_count"]) == (1, 1)
