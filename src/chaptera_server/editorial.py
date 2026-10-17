"""Durable editorial drafts, review notes and accepted revision audit.

One book/chapter owns one shared draft, not one draft per person. Every write
is guarded by BEGIN IMMEDIATE + optimistic versions. No publication, LLM
requests, or book ownership ACL are involved: admin and moderators share books.
"""
from __future__ import annotations

import sqlite3
import time
from uuid import uuid4

from .library import BookLibrary, LibraryConflict, LibraryNotFound
from .storage import QueueStore


class EditorialStore:
    def __init__(self, store: QueueStore):
        self.store = store
        self._initialize()

    def _initialize(self) -> None:
        with self.store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS editor_drafts (
                    book_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    source_revision INTEGER NOT NULL,
                    accepted_revision INTEGER NOT NULL,
                    body_text TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(book_id, chapter_id),
                    FOREIGN KEY(book_id,chapter_id)
                      REFERENCES book_chapters(book_id,chapter_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS editor_draft_revisions (
                    book_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    source_revision INTEGER NOT NULL,
                    accepted_revision INTEGER NOT NULL,
                    body_text TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(book_id, chapter_id, revision),
                    FOREIGN KEY(book_id,chapter_id)
                      REFERENCES book_chapters(book_id,chapter_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS editor_review_notes (
                    id TEXT PRIMARY KEY,
                    book_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    source_revision INTEGER NOT NULL,
                    draft_revision INTEGER NOT NULL,
                    accepted_revision INTEGER NOT NULL,
                    body_text TEXT NOT NULL CHECK (length(body_text) BETWEEN 1 AND 4000),
                    status TEXT NOT NULL CHECK (status IN ('open','resolved')),
                    note_revision INTEGER NOT NULL CHECK (note_revision > 0),
                    author_id TEXT NOT NULL,
                    resolver_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(book_id,chapter_id)
                      REFERENCES book_chapters(book_id,chapter_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_editor_notes_chapter
                  ON editor_review_notes(book_id,chapter_id,created_at,id);
            """)

    @staticmethod
    def _chapter(con: sqlite3.Connection, book_id: str, chapter_id: str) -> sqlite3.Row:
        row = con.execute(
            "SELECT source_revision,accepted_revision FROM book_chapters "
            "WHERE book_id=? AND chapter_id=?", (book_id, chapter_id)
        ).fetchone()
        if row is None:
            raise LibraryNotFound("Chapter not found")
        return row

    @staticmethod
    def _draft(row: sqlite3.Row | None, chapter: sqlite3.Row) -> dict:
        if row is None:
            return {
                "revision": 0, "source_revision": chapter["source_revision"],
                "accepted_revision": chapter["accepted_revision"],
                "body_text": "", "actor_id": None, "updated_at": None,
            }
        return {name: row[name] for name in (
            "revision", "source_revision", "accepted_revision",
            "body_text", "actor_id", "updated_at"
        )}

    def get_draft(self, book_id: str, chapter_id: str) -> dict:
        with self.store._connect() as con:
            chapter = self._chapter(con, book_id, chapter_id)
            row = con.execute("SELECT * FROM editor_drafts WHERE book_id=? AND chapter_id=?",
                              (book_id, chapter_id)).fetchone()
            return self._draft(row, chapter)

    def save_draft(self, book_id: str, chapter_id: str, *,
                   body_text: str, expected_revision: int,
                   expected_source_revision: int, expected_accepted_revision: int,
                   actor_id: str, rebase: bool = False) -> dict:
        if len(body_text) > 1_000_000:
            raise ValueError("Draft exceeds one million characters")
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            chapter = self._chapter(con, book_id, chapter_id)
            if (chapter["source_revision"] != expected_source_revision or
                    chapter["accepted_revision"] != expected_accepted_revision):
                raise LibraryConflict("Source or accepted translation changed; refresh required")
            previous = con.execute(
                "SELECT * FROM editor_drafts WHERE book_id=? AND chapter_id=?",
                (book_id, chapter_id)).fetchone()
            current_rev = previous["revision"] if previous else 0
            if current_rev != expected_revision:
                raise LibraryConflict("Draft revision changed; load and compare before overwriting")
            if previous and (previous["source_revision"] != expected_source_revision or
                             previous["accepted_revision"] != expected_accepted_revision) and not rebase:
                raise LibraryConflict("Draft was based on older revisions; explicit rebase required")
            if previous and previous["body_text"] == body_text and not rebase:
                return self._draft(previous, chapter)
            new_rev = current_rev + 1
            now = time.time()
            con.execute("""
                INSERT INTO editor_drafts VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(book_id,chapter_id) DO UPDATE SET
                    revision=excluded.revision,source_revision=excluded.source_revision,
                    accepted_revision=excluded.accepted_revision,
                    body_text=excluded.body_text,actor_id=excluded.actor_id,
                    updated_at=excluded.updated_at
            """, (book_id, chapter_id, new_rev, expected_source_revision,
                  expected_accepted_revision, body_text, actor_id, now))
            con.execute("INSERT INTO editor_draft_revisions VALUES (?,?,?,?,?,?,?,?)",
                        (book_id, chapter_id, new_rev, expected_source_revision,
                         expected_accepted_revision, body_text, actor_id, now))
            saved = con.execute(
                "SELECT * FROM editor_drafts WHERE book_id=? AND chapter_id=?",
                (book_id, chapter_id)).fetchone()
            return self._draft(saved, chapter)

    @staticmethod
    def _note(row: sqlite3.Row) -> dict:
        return {key: row[key] for key in (
            "id", "book_id", "chapter_id", "source_revision", "draft_revision",
            "accepted_revision", "body_text", "status", "note_revision",
            "author_id", "resolver_id", "created_at", "updated_at"
        )}

    def list_notes(self, book_id: str, chapter_id: str) -> list[dict]:
        with self.store._connect() as con:
            self._chapter(con, book_id, chapter_id)
            rows = con.execute("""
                SELECT * FROM editor_review_notes
                WHERE book_id=? AND chapter_id=?
                ORDER BY created_at,id LIMIT 1000
            """, (book_id, chapter_id)).fetchall()
            return [self._note(row) for row in rows]

    def add_note(self, book_id: str, chapter_id: str, *,
                 body_text: str, expected_source_revision: int,
                 expected_draft_revision: int, expected_accepted_revision: int,
                 actor_id: str) -> dict:
        cleaned = body_text.strip()
        if not cleaned or len(cleaned) > 4000:
            raise ValueError("Note must contain 1 to 4000 characters")
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            chapter = self._chapter(con, book_id, chapter_id)
            draft = con.execute(
                "SELECT revision FROM editor_drafts WHERE book_id=? AND chapter_id=?",
                (book_id, chapter_id)).fetchone()
            if ((draft["revision"] if draft else 0) != expected_draft_revision or
                    chapter["source_revision"] != expected_source_revision or
                    chapter["accepted_revision"] != expected_accepted_revision):
                raise LibraryConflict("Chapter/draft changed; reload before recording review")
            note_id = uuid4().hex
            now = time.time()
            con.execute("INSERT INTO editor_review_notes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (note_id, book_id, chapter_id, expected_source_revision,
                         expected_draft_revision, expected_accepted_revision, cleaned,
                         "open", 1, actor_id, None, now, now))
            return self._note(con.execute(
                "SELECT * FROM editor_review_notes WHERE id=?", (note_id,)
            ).fetchone())

    def resolve_note(self, book_id: str, chapter_id: str, note_id: str, *,
                     status: str, expected_revision: int, actor_id: str) -> dict:
        if status not in {"open", "resolved"}:
            raise ValueError("Invalid review status")
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._chapter(con, book_id, chapter_id)
            row = con.execute(
                "SELECT * FROM editor_review_notes WHERE id=? AND book_id=? AND chapter_id=?",
                (note_id, book_id, chapter_id)
            ).fetchone()
            if row is None:
                raise LibraryNotFound("Review note not found")
            if row["note_revision"] != expected_revision:
                raise LibraryConflict("Review note changed; reload before resolving")
            if row["status"] == status:
                return self._note(row)
            now = time.time()
            con.execute("""UPDATE editor_review_notes
                           SET status=?,note_revision=?,resolver_id=?,updated_at=?
                           WHERE id=?""",
                        (status, expected_revision + 1, actor_id, now, note_id))
            return self._note(con.execute(
                "SELECT * FROM editor_review_notes WHERE id=?", (note_id,)
            ).fetchone())

    def accepted_history(self, book_id: str, chapter_id: str) -> list[dict]:
        with self.store._connect() as con:
            self._chapter(con, book_id, chapter_id)
            rows = con.execute("""
                SELECT revision,source_revision,actor_id,created_at
                FROM book_accepted_revisions
                WHERE book_id=? AND chapter_id=? ORDER BY revision DESC LIMIT 100
            """, (book_id, chapter_id)).fetchall()
            return [dict(row) for row in rows]

    def accepted_version(self, book_id: str, chapter_id: str, revision: int) -> dict:
        with self.store._connect() as con:
            self._chapter(con, book_id, chapter_id)
            row = con.execute("""
                SELECT revision,source_revision,translated_html,actor_id,created_at
                FROM book_accepted_revisions
                WHERE book_id=? AND chapter_id=? AND revision=?
            """, (book_id, chapter_id, revision)).fetchone()
            if row is None:
                raise LibraryNotFound("Accepted revision not found")
            return dict(row)

    def restore_accepted(self, book_id: str, chapter_id: str, *,
                         revision: int, expected_current_revision: int,
                         expected_source_revision: int, actor_id: str) -> dict:
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            chapter = con.execute(
                "SELECT * FROM book_chapters WHERE book_id=? AND chapter_id=?",
                (book_id, chapter_id)
            ).fetchone()
            if chapter is None:
                raise LibraryNotFound("Chapter not found")
            if (chapter["accepted_revision"] != expected_current_revision or
                    chapter["source_revision"] != expected_source_revision):
                raise LibraryConflict("Chapter revisions changed before restore")
            historical = con.execute("""
                SELECT translated_html,source_revision FROM book_accepted_revisions
                WHERE book_id=? AND chapter_id=? AND revision=?
            """, (book_id, chapter_id, revision)).fetchone()
            if historical is None:
                raise LibraryNotFound("Accepted revision not found")
            if historical["source_revision"] != expected_source_revision:
                raise LibraryConflict("Historical translation belongs to a different source revision")
            if revision == expected_current_revision:
                raise LibraryConflict("Selected revision is already current")
            now = time.time()
            new_rev = expected_current_revision + 1
            con.execute("""UPDATE book_chapters
                           SET accepted_html=?,accepted_revision=?,
                               accepted_source_revision=?,updated_at=?
                           WHERE book_id=? AND chapter_id=?""",
                        (historical["translated_html"], new_rev,
                         expected_source_revision, now, book_id, chapter_id))
            con.execute("""INSERT INTO book_accepted_revisions
                           (book_id,chapter_id,revision,source_revision,
                            translated_html,actor_id,created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (book_id, chapter_id, new_rev, expected_source_revision,
                         historical["translated_html"], actor_id, now))
            return BookLibrary._chapter_dict(con.execute(
                "SELECT * FROM book_chapters WHERE book_id=? AND chapter_id=?",
                (book_id, chapter_id)
            ).fetchone())
