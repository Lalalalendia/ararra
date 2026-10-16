"""Revisioned, team-shared book library for the single-node SQLite prototype.

The library is independent of the legacy Windows program. All mutations use
BEGIN IMMEDIATE + optimistic revisions. Context is only returned if its source,
glossary and accepted predecessor inputs still match live DB state.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from chaptera_translation_core.chapter_context import (
    bounded_translation_tail,
    context_is_current,
    glossary_fingerprint,
    normalize_predecessor_dependency,
    sha256_text,
)
from chaptera_translation_core.core.worker_helpers.translation_completeness import strip_source_boilerplate
from chaptera_translation_core.prompt_policy import scoped_glossary
from .storage import JobSpec, QueueStore


class LibraryConflict(ValueError):
    """Revision mismatch, duplicate chapter position or changed dependency."""


class LibraryNotFound(KeyError):
    """Unknown book or chapter."""


class StaleBookJob(ValueError):
    """An immutable queued job no longer matches the book's current revisions."""


@dataclass(frozen=True)
class ContextInputs:
    book_id: str
    chapter_id: str
    chapter_path: str
    chapter_index: int
    source_html: str
    source_hash: str
    chapter_revision: int
    glossary: dict
    glossary_versions: dict
    glossary_authority: str
    glossary_revision: int
    glossary_hash: str
    predecessor: dict
    predecessor_anchor: dict

    def guard(self) -> dict:
        return {
            "source_hash": self.source_hash,
            "chapter_revision": self.chapter_revision,
            "glossary_revision": self.glossary_revision,
            "glossary_hash": self.glossary_hash,
            "predecessor": self.predecessor,
        }


def _normalized_path(value: str) -> str:
    result = str(value or "").strip().replace('\\', '/')
    if not result or result.startswith('/') or '..' in result.split('/'):
        raise ValueError("chapter_path must be a non-empty relative path without '..'")
    return result


def _normalized_source_hash(html: str, chapter_path: str) -> str:
    return sha256_text(strip_source_boilerplate(html, current_chapter_path=chapter_path))


def _translation_tail(html: str) -> str:
    # For continuity only: never include HTML/markup from a predecessor in model context.
    soup = BeautifulSoup(html or '', 'html.parser')
    text = soup.get_text(' ', strip=True)
    return bounded_translation_tail(text) if text else ''


class BookLibrary:
    def __init__(self, queue: QueueStore):
        self.queue = queue
        self._initialize()

    def _initialize(self) -> None:
        with self.queue._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    glossary_json TEXT NOT NULL DEFAULT '{}',
                    glossary_versions_json TEXT NOT NULL DEFAULT '{}',
                    glossary_authority TEXT NOT NULL DEFAULT 'READY',
                    glossary_revision INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS book_glossary_revisions (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    glossary_json TEXT NOT NULL,
                    versions_json TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(book_id, revision)
                );
                CREATE TABLE IF NOT EXISTS book_chapters (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    chapter_id TEXT NOT NULL,
                    chapter_index INTEGER NOT NULL CHECK(chapter_index > 0),
                    chapter_path TEXT NOT NULL,
                    source_html TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    source_revision INTEGER NOT NULL,
                    accepted_html TEXT,
                    accepted_revision INTEGER NOT NULL DEFAULT 0,
                    accepted_source_revision INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(book_id, chapter_id),
                    UNIQUE(book_id, chapter_index),
                    UNIQUE(book_id, chapter_path)
                );
                CREATE TABLE IF NOT EXISTS book_accepted_revisions (
                    book_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    source_revision INTEGER NOT NULL,
                    translated_html TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(book_id, chapter_id, revision),
                    FOREIGN KEY(book_id, chapter_id) REFERENCES book_chapters(book_id, chapter_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS chapter_context_snapshots (
                    book_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(book_id, chapter_id),
                    FOREIGN KEY(book_id, chapter_id) REFERENCES book_chapters(book_id, chapter_id)
                        ON DELETE CASCADE
                );
            """)

    def create_book(self, book_id: str, title: str) -> dict:
        book_id, title = str(book_id or '').strip(), str(title or '').strip()
        if not book_id or not title or len(book_id) > 128 or len(title) > 500:
            raise ValueError('Valid book id and title are required')
        with self.queue._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            existing = con.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
            if existing:
                if existing['title'] != title:
                    raise LibraryConflict('Book already exists with a different title')
                return self._book_dict(existing)
            now = time.time()
            con.execute('INSERT INTO books(id,title,created_at,updated_at) VALUES(?,?,?,?)',
                        (book_id, title, now, now))
            return self._book_dict(con.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone())

    @staticmethod
    def _book_dict(row: sqlite3.Row) -> dict:
        return {
            'book_id': row['id'], 'title': row['title'],
            'glossary': json.loads(row['glossary_json']),
            'glossary_versions': json.loads(row['glossary_versions_json']),
            'glossary_authority': row['glossary_authority'],
            'glossary_revision': row['glossary_revision'],
        }

    def get_book(self, book_id: str) -> dict:
        with self.queue._connect() as con:
            row = con.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
            if not row:
                raise LibraryNotFound('Book not found')
            return self._book_dict(row)

    def put_glossary(self, book_id: str, *, glossary: dict, versions: dict,
                     authority: str, expected_revision: int, actor_id: str) -> dict:
        if authority not in {'READY', 'READY_WITH_WARNINGS', 'EMPTY_EXPECTED', 'UNAVAILABLE', 'DISABLED'}:
            raise ValueError('Invalid glossary authority')
        if not isinstance(glossary, dict) or not isinstance(versions, dict):
            raise ValueError('Glossary and versions must be objects')
        for term, rules in versions.items():
            if not isinstance(term, str) or not isinstance(rules, list):
                raise ValueError('Versioned glossary rules must be arrays keyed by source term')
        packed = json.dumps(glossary, ensure_ascii=False, sort_keys=True)
        packed_versions = json.dumps(versions, ensure_ascii=False, sort_keys=True)
        with self.queue._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
            if row is None:
                raise LibraryNotFound('Book not found')
            if int(row['glossary_revision']) != expected_revision:
                raise LibraryConflict('Glossary revision mismatch')
            for chapter in con.execute('SELECT chapter_path FROM book_chapters WHERE book_id=?', (book_id,)):
                # Do not save a glossary that makes existing chapters untranslatable.
                scoped_glossary(glossary, versions, chapter['chapter_path'], authority)
            if (row['glossary_json'] == packed and row['glossary_versions_json'] == packed_versions
                    and row['glossary_authority'] == authority):
                return self._book_dict(row)
            rev = expected_revision + 1
            now = time.time()
            con.execute('UPDATE books SET glossary_json=?,glossary_versions_json=?,glossary_authority=?,glossary_revision=?,updated_at=? WHERE id=?',
                        (packed, packed_versions, authority, rev, now, book_id))
            con.execute('INSERT INTO book_glossary_revisions VALUES(?,?,?,?,?,?,?)',
                        (book_id, rev, packed, packed_versions, authority, actor_id, now))
            return self._book_dict(con.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone())

    @staticmethod
    def _chapter_dict(row: sqlite3.Row) -> dict:
        return {
            'book_id': row['book_id'], 'chapter_id': row['chapter_id'],
            'chapter_index': row['chapter_index'], 'chapter_path': row['chapter_path'],
            'source_html': row['source_html'], 'source_hash': row['source_hash'],
            'source_revision': row['source_revision'],
            'accepted_revision': row['accepted_revision'],
            'accepted_source_revision': row['accepted_source_revision'],
            'accepted_current': bool(row['accepted_html']) and row['accepted_source_revision'] == row['source_revision'],
            'accepted_html': row['accepted_html'],
        }

    def put_chapter(self, book_id: str, chapter_id: str, *, chapter_index: int,
                    chapter_path: str, source_html: str, expected_revision: int) -> dict:
        chapter_id = str(chapter_id or '').strip()
        chapter_path = _normalized_path(chapter_path)
        if not chapter_id or len(chapter_id) > 255 or chapter_index <= 0 or not str(source_html or '').strip():
            raise ValueError('Valid chapter id, index, path and source HTML are required')
        if len(source_html) > 2_000_000:
            raise ValueError('Chapter source is too large')
        source_hash = _normalized_source_hash(source_html, chapter_path)
        with self.queue._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute('SELECT 1 FROM books WHERE id=?', (book_id,)).fetchone() is None:
                raise LibraryNotFound('Book not found')
            row = con.execute('SELECT * FROM book_chapters WHERE book_id=? AND chapter_id=?',
                              (book_id, chapter_id)).fetchone()
            rev = int(row['source_revision']) if row else 0
            if rev != expected_revision:
                raise LibraryConflict('Chapter source revision mismatch')
            if row and (row['chapter_index'], row['chapter_path'], row['source_html']) == (chapter_index, chapter_path, source_html):
                return self._chapter_dict(row)
            try:
                if not row:
                    con.execute('INSERT INTO book_chapters(book_id,chapter_id,chapter_index,chapter_path,source_html,source_hash,source_revision,updated_at) VALUES(?,?,?,?,?,?,1,?)',
                                (book_id, chapter_id, chapter_index, chapter_path, source_html, source_hash, time.time()))
                else:
                    con.execute('UPDATE book_chapters SET chapter_index=?,chapter_path=?,source_html=?,source_hash=?,source_revision=?,updated_at=? WHERE book_id=? AND chapter_id=?',
                                (chapter_index, chapter_path, source_html, source_hash, rev + 1, time.time(), book_id, chapter_id))
            except sqlite3.IntegrityError as exc:
                raise LibraryConflict('Chapter index or path already belongs to another chapter') from exc
            return self._chapter_dict(con.execute('SELECT * FROM book_chapters WHERE book_id=? AND chapter_id=?',
                                                   (book_id, chapter_id)).fetchone())

    def get_chapter(self, book_id: str, chapter_id: str) -> dict:
        with self.queue._connect() as con:
            row = con.execute('SELECT * FROM book_chapters WHERE book_id=? AND chapter_id=?',
                              (book_id, chapter_id)).fetchone()
            if row is None:
                raise LibraryNotFound('Chapter not found')
            return self._chapter_dict(row)

    def accept_translation(self, book_id: str, chapter_id: str, translated_html: str,
                           expected_revision: int, expected_source_revision: int,
                           actor_id: str = 'system') -> dict:
        if not str(translated_html or '').strip() or not _translation_tail(translated_html):
            raise ValueError('A nonempty accepted translation is required')
        with self.queue._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM book_chapters WHERE book_id=? AND chapter_id=?',
                              (book_id, chapter_id)).fetchone()
            if row is None:
                raise LibraryNotFound('Chapter not found')
            if int(row['accepted_revision']) != expected_revision:
                raise LibraryConflict('Accepted translation revision mismatch')
            if int(row['source_revision']) != expected_source_revision:
                raise LibraryConflict('Source revision changed before translation acceptance')
            if row['accepted_html'] == translated_html and row['accepted_source_revision'] == row['source_revision']:
                return self._chapter_dict(row)
            now = time.time()
            con.execute('UPDATE book_chapters SET accepted_html=?,accepted_revision=?,accepted_source_revision=?,updated_at=? WHERE book_id=? AND chapter_id=?',
                        (translated_html, expected_revision + 1, row['source_revision'], now, book_id, chapter_id))
            con.execute('INSERT INTO book_accepted_revisions(book_id,chapter_id,revision,source_revision,translated_html,actor_id,created_at) VALUES(?,?,?,?,?,?,?)',
                        (book_id, chapter_id, expected_revision + 1, row['source_revision'], translated_html, actor_id, now))
            return self._chapter_dict(con.execute('SELECT * FROM book_chapters WHERE book_id=? AND chapter_id=?',
                                                   (book_id, chapter_id)).fetchone())

    def _inputs(self, con: sqlite3.Connection, book_id: str, chapter_id: str) -> ContextInputs:
        row = con.execute('SELECT c.*,b.glossary_json,b.glossary_versions_json,b.glossary_authority,b.glossary_revision FROM book_chapters c JOIN books b ON b.id=c.book_id WHERE c.book_id=? AND c.chapter_id=?',
                          (book_id, chapter_id)).fetchone()
        if row is None:
            raise LibraryNotFound('Chapter not found')
        base = json.loads(row['glossary_json'])
        versions = json.loads(row['glossary_versions_json'])
        effective = scoped_glossary(base, versions, row['chapter_path'], row['glossary_authority'])
        previous = con.execute('SELECT * FROM book_chapters WHERE book_id=? AND chapter_index=?',
                               (book_id, int(row['chapter_index']) - 1)).fetchone()
        predecessor = {}
        anchor = {}
        if previous is not None:
            accepted_current = (previous['accepted_html'] and previous['accepted_source_revision'] == previous['source_revision'])
            tail = _translation_tail(previous['accepted_html']) if accepted_current else ''
            predecessor = normalize_predecessor_dependency({
                'chapter_path': previous['chapter_path'],
                'source_hash': previous['source_hash'],
                # Revision is included: even an edit outside the 2400-char tail invalidates context.
                'translation_tail_hash': sha256_text(f"{previous['accepted_revision']}:{tail}") if accepted_current else '',
                'translation_tail': tail,
            })
            anchor = {
                'chapter_path': previous['chapter_path'],
                'source': BeautifulSoup(previous['source_html'], 'html.parser').get_text(' ', strip=True)[:10000],
                'translation_tail': tail,
            }
        return ContextInputs(
            book_id=book_id, chapter_id=chapter_id, chapter_path=row['chapter_path'],
            chapter_index=row['chapter_index'], source_html=row['source_html'], source_hash=row['source_hash'],
            chapter_revision=row['source_revision'], glossary=base, glossary_versions=versions,
            glossary_authority=row['glossary_authority'], glossary_revision=row['glossary_revision'],
            glossary_hash=glossary_fingerprint(effective), predecessor=normalize_predecessor_dependency(predecessor),
            predecessor_anchor=anchor,
        )

    def execution_inputs(self, job_data: dict) -> ContextInputs:
        book_id = str(job_data.get('book_id') or '')
        chapter_id = str(job_data.get('chapter_id') or '')
        with self.queue._connect() as con:
            current = self._inputs(con, book_id, chapter_id)
        if (current.chapter_revision != int(job_data.get('book_chapter_revision') or 0)
                or current.glossary_revision != int(job_data.get('book_glossary_revision') or 0)
                or current.source_html != job_data.get('source_html')
                or current.chapter_path != job_data.get('chapter_path')):
            raise StaleBookJob('Book source or glossary was edited after the translation job was queued')
        return current

    def new_job(self, book_id: str, chapter_id: str, *, source_language: str,
                target_language: str, style_instructions: str, max_attempts: int) -> JobSpec:
        with self.queue._connect() as con:
            inputs = self._inputs(con, book_id, chapter_id)
        return JobSpec(
            chapter_id=chapter_id, source_html=inputs.source_html,
            chapter_path=inputs.chapter_path,
            source_language=source_language, target_language=target_language,
            glossary=inputs.glossary, glossary_versions=inputs.glossary_versions,
            glossary_authority=inputs.glossary_authority, style_instructions=style_instructions,
            max_attempts=max_attempts,
            book_id=book_id, book_chapter_revision=inputs.chapter_revision,
            book_glossary_revision=inputs.glossary_revision,
        )

    def current_context(self, inputs: ContextInputs) -> dict | None:
        with self.queue._connect() as con:
            row = con.execute('SELECT snapshot_json FROM chapter_context_snapshots WHERE book_id=? AND chapter_id=?',
                              (inputs.book_id, inputs.chapter_id)).fetchone()
        if not row:
            return None
        snapshot = json.loads(row['snapshot_json'])
        if (int(snapshot.get('book_glossary_revision', -1)) != inputs.glossary_revision
                or int(snapshot.get('book_chapter_revision', -1)) != inputs.chapter_revision
                or str(snapshot.get('chapter_path') or '') != inputs.chapter_path
                or not context_is_current(snapshot, source_hash=inputs.source_hash,
                                          glossary_hash=inputs.glossary_hash, predecessor=inputs.predecessor)):
            return None
        return snapshot

    def context_for_chapter(self, book_id: str, chapter_id: str) -> dict:
        with self.queue._connect() as con:
            inputs = self._inputs(con, book_id, chapter_id)
        snapshot = self.current_context(inputs)
        # Never reveal a stale snapshot. Clients do not have to track fingerprints.
        return {'status': 'current' if snapshot else 'missing_or_stale',
                'snapshot': snapshot,
                'chapter_revision': inputs.chapter_revision,
                'glossary_revision': inputs.glossary_revision}

    def save_context(self, inputs: ContextInputs, snapshot: dict) -> bool:
        if (not context_is_current(snapshot, source_hash=inputs.source_hash,
                                   glossary_hash=inputs.glossary_hash, predecessor=inputs.predecessor)
                or str(snapshot.get('chapter_path') or '') != inputs.chapter_path):
            raise ValueError('Invalid chapter context dependency fingerprint')
        with self.queue._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            live = self._inputs(con, inputs.book_id, inputs.chapter_id)
            if live.guard() != inputs.guard():
                raise StaleBookJob('Book inputs changed while generating context')
            con.execute('INSERT INTO chapter_context_snapshots(book_id,chapter_id,snapshot_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(book_id,chapter_id) DO UPDATE SET snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at',
                        (inputs.book_id, inputs.chapter_id,
                         json.dumps({**snapshot, 'book_glossary_revision': inputs.glossary_revision,
                                     'book_chapter_revision': inputs.chapter_revision}, ensure_ascii=False), time.time()))
            return True

    def complete_book_job(self, job_id: str, token: str, translated_html: str,
                          source_hash: str, unit_count: int, inputs: ContextInputs) -> bool:
        """Fence the final result and all revision checks in ONE SQLite write transaction."""
        now = time.time()
        with self.queue._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT status,cancel_requested FROM jobs WHERE id=? AND lease_owner=?',
                              (job_id, token)).fetchone()
            if row is None or row['status'] != 'running':
                return False
            live = self._inputs(con, inputs.book_id, inputs.chapter_id)
            stale = live.guard() != inputs.guard()
            status = 'cancelled' if row['cancel_requested'] else 'failed' if stale else 'completed'
            message = 'Book source, glossary or predecessor changed while translation was running' if stale else ''
            con.execute('UPDATE jobs SET status=?,result_html=?,source_hash=?,unit_count=?,last_error=?,run_after=?,lease_until=NULL,lease_owner=NULL,updated_at=? WHERE id=? AND lease_owner=?',
                        (status, translated_html if status == 'completed' else None,
                         source_hash if status == 'completed' else None,
                         unit_count if status == 'completed' else None,
                         message, now, now, job_id, token))
            self.queue._event(con, job_id, status, message, now)
            return status == 'completed'
