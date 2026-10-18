"""RE-06C: atomic, revision-bound multi-chapter submissions on the existing SQLite queue.

A preview is read-only. A confirmed batch enqueues all chapter jobs in one
BEGIN IMMEDIATE transaction and stores just immutable identifiers and hashes.
The existing single-worker scheduler, per-moderator quota and job states remain
the only execution authority.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid

from .library import BookLibrary, LibraryConflict, LibraryNotFound
from .storage import JobSpec, QueueStore


class BatchConflict(LibraryConflict):
    """A stale selection, manifest or reused idempotency key."""


class BatchNotFound(LibraryNotFound):
    """Batch does not belong to this book or no longer exists."""


def _digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TranslationBatches:
    """Book-level batch intent, using the same DB and queue as /v1/jobs."""

    _OPTIONS = (
        "source_language", "target_language", "style_instructions",
        "max_attempts", "transport",
    )
    _STATUSES = ("queued", "running", "retry_wait", "completed", "failed", "cancelled")

    def __init__(self, queue: QueueStore, library: BookLibrary, *,
                 require_personal_budget: bool = False):
        self.queue = queue
        self.library = library
        self.require_personal_budget = require_personal_budget
        with queue._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS translation_batches (
                    id TEXT PRIMARY KEY,
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    actor_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(actor_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS ix_translation_batches_book
                    ON translation_batches(book_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS translation_batch_items (
                    batch_id TEXT NOT NULL REFERENCES translation_batches(id) ON DELETE CASCADE,
                    chapter_id TEXT NOT NULL,
                    chapter_index INTEGER NOT NULL CHECK(chapter_index > 0),
                    source_revision INTEGER NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    glossary_revision INTEGER NOT NULL,
                    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE RESTRICT,
                    PRIMARY KEY(batch_id, chapter_id),
                    UNIQUE(batch_id, chapter_index)
                );
            """)

    def _resolve(self, con: sqlite3.Connection, book_id: str, payload: dict):
        book = con.execute("SELECT glossary_revision FROM books WHERE id=?", (book_id,)).fetchone()
        if book is None:
            raise LibraryNotFound("Book not found")
        glossary_revision = int(book["glossary_revision"])
        if glossary_revision != int(payload["expected_glossary_revision"]):
            raise BatchConflict("Book glossary changed after chapter selection")

        seen = set()
        options = {name: payload[name] for name in self._OPTIONS}
        entries = []
        total_source_bytes = 0
        for selected in payload["chapters"]:
            cid = selected["chapter_id"].strip()
            if not cid or cid in seen:
                raise ValueError("Every selected chapter must have a unique nonempty ID")
            seen.add(cid)
            try:
                inputs = self.library._inputs(con, book_id, cid)
            except LibraryNotFound as exc:
                raise BatchConflict("Selected chapter no longer exists") from exc
            if inputs.chapter_revision != selected["expected_source_revision"]:
                raise BatchConflict("Selected chapter source revision changed")
            total_source_bytes += len(inputs.source_html.encode("utf-8"))
            if total_source_bytes > 16 * 1024 * 1024:
                raise ValueError("Selected batch exceeds the 16 MiB total source limit")
            spec = JobSpec(
                chapter_id=cid, source_html=inputs.source_html,
                chapter_path=inputs.chapter_path,
                source_language=options["source_language"],
                target_language=options["target_language"],
                glossary=inputs.glossary, glossary_versions=inputs.glossary_versions,
                glossary_authority=inputs.glossary_authority,
                style_instructions=options["style_instructions"],
                max_attempts=options["max_attempts"],
                transport=options["transport"],
                book_id=book_id, book_chapter_revision=inputs.chapter_revision,
                book_glossary_revision=inputs.glossary_revision,
            )
            entries.append((inputs, spec))

        # Book indices, not request order or UUIDs, define the batch sequence.
        entries.sort(key=lambda entry: (entry[0].chapter_index, entry[0].chapter_id))
        proof = {
            "book_id": book_id, "options": options,
            "glossary_revision": glossary_revision,
            "chapters": [
                {
                    "chapter_id": inputs.chapter_id,
                    "chapter_index": inputs.chapter_index,
                    "chapter_path": inputs.chapter_path,
                    "source_revision": inputs.chapter_revision,
                    "source_sha256": inputs.source_hash,
                }
                for inputs, _ in entries
            ],
        }
        return _digest(proof), entries

    def preview(self, book_id: str, payload: dict) -> dict:
        with self.queue._connect() as con:
            con.execute("BEGIN")
            manifest, entries = self._resolve(con, book_id, payload)
            return {
                "book_id": book_id, "chapter_count": len(entries),
                "manifest_sha256": manifest,
                "chapters": [
                    {
                        "chapter_id": inputs.chapter_id,
                        "chapter_index": inputs.chapter_index,
                        "source_revision": inputs.chapter_revision,
                        "source_sha256": inputs.source_hash,
                    }
                    for inputs, _ in entries
                ],
            }

    def submit(self, book_id: str, payload: dict, *, actor_id: str,
               idempotency_key: str) -> dict:
        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise ValueError("A nonempty Idempotency-Key of at most 128 characters is required")
        expected = payload["expected_manifest_sha256"]
        request_sha = _digest({"book_id": book_id, "payload": payload})
        with self.queue._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute(
                "SELECT id,request_sha256 FROM translation_batches "
                "WHERE actor_id=? AND idempotency_key=?",
                (actor_id, key),
            ).fetchone()
            if previous is not None:
                if previous["request_sha256"] != request_sha:
                    raise BatchConflict("Idempotency-Key already belongs to another batch request")
                # A valid retry never repeats work, even when the original
                # book chapter or glossary changed after submission.
                return self._details(con, book_id, previous["id"])

            if self.require_personal_budget:
                # Admit a paid batch only when its *submitter* has at least
                # one explicitly capped personal project. Old idempotent
                # receipts are returned above even after key revocation.
                admitted = con.execute("""
                    SELECT 1 FROM member_gemini_keys AS k
                    JOIN member_gemini_budgets AS b ON b.project_id=k.project_id
                    WHERE k.member_id=? AND b.member_id=k.member_id
                      AND b.daily_request_cap>0 LIMIT 1
                """, (actor_id,)).fetchone()
                if not admitted:
                    raise BatchConflict(
                        "Set a daily request cap for your Gemini project "
                        "in the personal cabinet before submitting a batch"
                    )
            manifest, entries = self._resolve(con, book_id, payload)
            if manifest != expected:
                raise BatchConflict("Batch preview expired: source or options changed")
            now, batch_id = time.time(), str(uuid.uuid4())
            con.execute(
                "INSERT INTO translation_batches "
                "(id,book_id,actor_id,idempotency_key,request_sha256,manifest_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (batch_id, book_id, actor_id, key, request_sha, manifest, now),
            )
            for inputs, spec in entries:
                queued = QueueStore._enqueue_in_transaction(
                    con, actor_id, spec, now,
                )
                con.execute(
                    "INSERT INTO translation_batch_items "
                    "(batch_id,chapter_id,chapter_index,source_revision,source_sha256,"
                    "glossary_revision,job_id) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, inputs.chapter_id, inputs.chapter_index,
                     inputs.chapter_revision, inputs.source_hash,
                     inputs.glossary_revision, queued["id"]),
                )
            return self._details(con, book_id, batch_id)

    def _details(self, con: sqlite3.Connection, book_id: str, batch_id: str) -> dict:
        batch = con.execute(
            "SELECT * FROM translation_batches WHERE id=? AND book_id=?",
            (batch_id, book_id),
        ).fetchone()
        if batch is None:
            raise BatchNotFound("Translation batch not found")
        rows = con.execute("""
            SELECT i.chapter_id, i.chapter_index, i.source_revision,
                   i.source_sha256, i.glossary_revision, i.job_id,
                   j.status, j.attempts
            FROM translation_batch_items AS i
            JOIN jobs AS j ON j.id=i.job_id
            WHERE i.batch_id=?
            ORDER BY i.chapter_index, i.chapter_id
        """, (batch_id,)).fetchall()
        chapters = [dict(row) for row in rows]
        counters = {name: sum(row["status"] == name for row in chapters)
                    for name in self._STATUSES}
        total = len(chapters)
        if total and counters["completed"] == total:
            state = "completed"
        elif total and (counters["completed"] + counters["failed"] +
                        counters["cancelled"]) == total:
            state = "finished_with_errors"
        else:
            state = "active"
        return {
            "batch_id": batch_id, "book_id": book_id,
            "actor_id": batch["actor_id"],
            "manifest_sha256": batch["manifest_sha256"],
            "created_at": batch["created_at"],
            "status": state, "chapter_count": total,
            "counts": counters, "chapters": chapters,
        }

    def get(self, book_id: str, batch_id: str) -> dict:
        with self.queue._connect() as con:
            return self._details(con, book_id, batch_id)

    def list(self, book_id: str, *, limit: int = 20) -> list[dict]:
        with self.queue._connect() as con:
            if con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone() is None:
                raise LibraryNotFound("Book not found")
            rows = con.execute(
                "SELECT id FROM translation_batches WHERE book_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (book_id, max(1, min(limit, 50))),
            ).fetchall()
            return [self._details(con, book_id, row["id"]) for row in rows]
