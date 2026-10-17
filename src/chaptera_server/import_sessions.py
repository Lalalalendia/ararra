"""Bounded SQLite import staging and atomic chapter confirmation (ReadEra RE-02).

Preview remains read-only. Parsed chapter HTML is private to the staging actor
until the actor explicitly maps every source chapter to create/replace/skip.
There are no network calls, translation jobs or publication side effects.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid

from bs4 import BeautifulSoup

from .book_import import ImportFormatError, parse_book_import
from .library import LibraryConflict, LibraryNotFound, _normalized_path, _normalized_source_hash
from .storage import QueueStore

MAX_STAGE_BYTES = 24 * 1024 * 1024
MAX_ACTIVE_STAGES_PER_ACTOR = 3
STAGE_TTL_SECONDS = 1800
COMMITTED_TTL_SECONDS = 86400


class StageMissing(KeyError):
    """The stage is not owned by this member or does not exist."""


class StageExpired(ValueError):
    """The stage cannot be committed past its deadline."""


class StageConflict(ValueError):
    """The selection is incomplete, stale, ambiguous or reuses a token."""


class StageTooLarge(ValueError):
    """The bounded temporary staging budget was exceeded."""


def _packed(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_packed(value).encode("utf-8")).hexdigest()


def _meaningful(html: str) -> bool:
    return bool(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))


class ImportSessions:
    """Staging uses the same SQLite connection and transaction as BookLibrary."""

    def __init__(self, store: QueueStore):
        self.store = store
        with store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS book_import_stages (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    idempotency_key TEXT,
                    input_fingerprint TEXT NOT NULL,
                    original_sha256 TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('staged','committed')),
                    preview_json TEXT NOT NULL,
                    payload_json TEXT,
                    prefix_html TEXT NOT NULL,
                    confirm_fingerprint TEXT,
                    commit_result_json TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    UNIQUE(actor_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS ix_book_import_stages_expiry
                    ON book_import_stages(expires_at);
                CREATE TABLE IF NOT EXISTS book_import_unassigned (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    stage_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    source_html TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(book_id, stage_id)
                );
            """)

    @staticmethod
    def _cleanup(con: sqlite3.Connection, now: float) -> None:
        con.execute("DELETE FROM book_import_stages WHERE expires_at < ?", (now,))

    @staticmethod
    def _public(row: sqlite3.Row, *, include_prefix: bool = False) -> dict:
        result = {
            "stage_id": row["id"],
            "status": row["status"],
            "file_sha256": row["original_sha256"],
            "expires_at": row["expires_at"],
            "preview": json.loads(row["preview_json"]),
            "unassigned_prefix_saved": _meaningful(row["prefix_html"]),
        }
        if include_prefix:
            result["unassigned_prefix_html"] = row["prefix_html"]
        if row["status"] == "committed":
            result["commit_result"] = json.loads(row["commit_result_json"])
        return result

    def stage(self, actor: str, filename: str, data: bytes,
              *, idempotency_key: str | None = None) -> dict:
        key = (idempotency_key or "").strip() or None
        if key is not None and len(key) > 128:
            raise ValueError("Idempotency-Key exceeds 128 characters")
        preview, candidates, prefix = parse_book_import(filename, data)
        if preview["kind"] != "book":
            raise ImportFormatError(
                "Metadata-only catalogs contain no chapter text; source import is forbidden"
            )
        if any(not _meaningful(item.html) for item in candidates):
            raise ImportFormatError("Empty chapters must be corrected before staging")
        payload = {
            "chapters": [{
                "sequence": n,
                "title": item.title,
                "detected_number": item.number,
                "source_path": item.path,
                "source_html": item.html,
            } for n, item in enumerate(candidates, start=1)]
        }
        packed = _packed(payload)
        if len(packed.encode("utf-8")) + len(prefix.encode("utf-8")) > MAX_STAGE_BYTES:
            raise StageTooLarge("Expanded source exceeds the 24 MiB staging limit")
        source_sha256 = hashlib.sha256(data).hexdigest()
        fingerprint = _fingerprint([preview["filename"], source_sha256])
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._cleanup(con, now)
            if key:
                previous = con.execute(
                    "SELECT * FROM book_import_stages WHERE actor_id=? AND idempotency_key=?",
                    (actor, key),
                ).fetchone()
                if previous:
                    if previous["input_fingerprint"] != fingerprint:
                        raise StageConflict("Idempotency-Key refers to a different uploaded file")
                    return self._public(previous)
            active = con.execute(
                "SELECT COUNT(*) AS n FROM book_import_stages WHERE actor_id=? AND status='staged'",
                (actor,),
            ).fetchone()["n"]
            if active >= MAX_ACTIVE_STAGES_PER_ACTOR:
                raise StageTooLarge("Too many unconfirmed import stages for this member")
            stage_id = str(uuid.uuid4())
            con.execute(
                """INSERT INTO book_import_stages(
                    id,actor_id,idempotency_key,input_fingerprint,original_sha256,
                    filename,status,preview_json,payload_json,prefix_html,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,'staged',?,?,?,?,?)""",
                (stage_id, actor, key, fingerprint, source_sha256, preview["filename"],
                 _packed(preview), packed, prefix, now, now + STAGE_TTL_SECONDS),
            )
            row = con.execute("SELECT * FROM book_import_stages WHERE id=?", (stage_id,)).fetchone()
            return self._public(row)

    @staticmethod
    def _owned(con: sqlite3.Connection, actor: str, stage_id: str) -> sqlite3.Row:
        row = con.execute(
            "SELECT * FROM book_import_stages WHERE id=? AND actor_id=?", (stage_id, actor),
        ).fetchone()
        if row is None:
            raise StageMissing("Import stage not found")
        if row["expires_at"] < time.time():
            raise StageExpired("Import stage expired; upload the source again")
        return row

    def get(self, actor: str, stage_id: str) -> dict:
        with self.store._connect() as con:
            return self._public(self._owned(con, actor, stage_id), include_prefix=True)

    def prefixes(self, book_id: str) -> list[dict]:
        """Expose preserved, explicitly unassigned prefaces to team editors."""
        with self.store._connect() as con:
            if con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone() is None:
                raise LibraryNotFound("Book not found")
            return [dict(row) for row in con.execute(
                """SELECT stage_id,filename,source_html,source_sha256,created_at
                   FROM book_import_unassigned WHERE book_id=? ORDER BY created_at""",
                (book_id,),
            ).fetchall()]

    def confirm(self, actor: str, stage_id: str, request: dict) -> dict:
        fingerprint = _fingerprint({
            **request,
            "chapters": sorted(request["chapters"], key=lambda choice: choice["sequence"]),
        })
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            stage = self._owned(con, actor, stage_id)
            if stage["status"] == "committed":
                if stage["confirm_fingerprint"] != fingerprint:
                    raise StageConflict("Already committed with a different chapter selection")
                return json.loads(stage["commit_result_json"])

            stored = json.loads(stage["payload_json"])
            candidates = {chapter["sequence"]: chapter for chapter in stored["chapters"]}
            decisions = request["chapters"]
            if len(decisions) != len(candidates) or {d["sequence"] for d in decisions} != set(candidates):
                raise StageConflict("Every staged chapter must have exactly one explicit decision")
            if len({d["sequence"] for d in decisions}) != len(decisions):
                raise StageConflict("A staged chapter cannot be selected twice")
            prefix_present = _meaningful(stage["prefix_html"])
            if prefix_present and request["prefix_policy"] not in ("preserve", "discard"):
                raise StageConflict("Explicitly preserve or discard unassigned preface before import")
            if request["prefix_policy"] == "preserve" and not prefix_present:
                raise StageConflict("There is no unassigned preface to preserve")

            book_id = request["book_id"].strip()
            title = (request.get("title") or "").strip()
            if not book_id or len(book_id) > 128:
                raise ValueError("Valid book_id is required")
            book = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
            if request["book_mode"] == "create":
                if book:
                    raise LibraryConflict("Book already exists; explicitly select existing mode")
                if not title or len(title) > 500:
                    raise ValueError("A book title is required for create mode")
            elif request["book_mode"] == "existing":
                if book is None:
                    raise LibraryNotFound("Book not found; explicitly select create mode")
                if title and title != book["title"]:
                    raise StageConflict("Confirmation cannot silently rename an existing book")
            else:
                raise ValueError("Invalid book mode")

            writes, result_chapters = [], []
            used_ids, used_indexes, used_paths = set(), set(), set()
            for decision in decisions:
                source = candidates[decision["sequence"]]
                action = decision["action"]
                if action == "skip":
                    if any(decision.get(field) is not None for field in (
                        "chapter_id", "chapter_index", "chapter_path", "expected_revision"
                    )):
                        raise StageConflict("Skip action must not specify a destination")
                    result_chapters.append({"sequence": decision["sequence"], "action": "skip"})
                    continue
                if action not in ("create", "replace"):
                    raise StageConflict("Invalid selection action")
                chapter_id = str(decision.get("chapter_id") or "").strip()
                index, path = decision.get("chapter_index"), decision.get("chapter_path")
                expected = decision.get("expected_revision")
                if not chapter_id or len(chapter_id) > 255 or type(index) is not int or index < 1:
                    raise StageConflict("Valid chapter_id and positive index required")
                if not isinstance(path, str) or len(path) > 1024:
                    raise StageConflict("Valid chapter path is required")
                try:
                    path = _normalized_path(path)
                except ValueError as exc:
                    raise StageConflict(str(exc)) from exc
                if type(expected) is not int or expected < 0:
                    raise StageConflict("Expected source revision is required")
                if action == "create" and expected != 0:
                    raise StageConflict("New chapter must use expected_revision=0")
                if not _meaningful(source["source_html"]):
                    raise StageConflict("Cannot import a chapter without readable text")
                if (chapter_id in used_ids or index in used_indexes or path in used_paths):
                    raise StageConflict("The batch maps more than one chapter to a destination")
                used_ids.add(chapter_id)
                used_indexes.add(index)
                used_paths.add(path)
                writes.append((decision["sequence"], action, chapter_id, index, path,
                               source["source_html"], expected))
            if not writes:
                raise StageConflict("Select at least one chapter for import")

            # Validate EVERY row and destination against live revisions while holding
            # the SQLite write lock; no partial writes or silent reassignment.
            for seq, action, chapter_id, index, path, html, expected in writes:
                old = con.execute(
                    "SELECT * FROM book_chapters WHERE book_id=? AND chapter_id=?",
                    (book_id, chapter_id),
                ).fetchone()
                if action == "create" and old is not None:
                    raise LibraryConflict("Chapter already exists: explicit replace required")
                if action == "replace" and old is None:
                    raise LibraryConflict("Selected replacement chapter does not exist")
                if action == "replace" and old["source_revision"] != expected:
                    raise LibraryConflict("Chapter source revision changed after preview")
                owners = con.execute(
                    "SELECT chapter_id FROM book_chapters WHERE book_id=? AND (chapter_index=? OR chapter_path=?)",
                    (book_id, index, path),
                ).fetchall()
                if any(owner["chapter_id"] != chapter_id for owner in owners):
                    raise LibraryConflict("Chapter index or path is owned by another chapter")

            now = time.time()
            if request["book_mode"] == "create":
                con.execute(
                    "INSERT INTO books(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                    (book_id, title, now, now),
                )
            try:
                for seq, action, chapter_id, index, path, html, expected in writes:
                    source_hash = _normalized_source_hash(html, path)
                    old = con.execute(
                        "SELECT * FROM book_chapters WHERE book_id=? AND chapter_id=?",
                        (book_id, chapter_id),
                    ).fetchone()
                    if old and (old["chapter_index"], old["chapter_path"], old["source_html"]) == (index, path, html):
                        revision = old["source_revision"]
                    elif old:
                        revision = expected + 1
                        con.execute(
                            """UPDATE book_chapters SET chapter_index=?,chapter_path=?,source_html=?,
                               source_hash=?,source_revision=?,updated_at=?
                               WHERE book_id=? AND chapter_id=?""",
                            (index, path, html, source_hash, revision, now, book_id, chapter_id),
                        )
                    else:
                        revision = 1
                        con.execute(
                            """INSERT INTO book_chapters(book_id,chapter_id,chapter_index,
                               chapter_path,source_html,source_hash,source_revision,updated_at)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (book_id, chapter_id, index, path, html, source_hash, revision, now),
                        )
                    result_chapters.append({
                        "sequence": seq, "action": action, "chapter_id": chapter_id,
                        "chapter_index": index, "source_revision": revision,
                    })
                if prefix_present and request["prefix_policy"] == "preserve":
                    con.execute(
                        """INSERT INTO book_import_unassigned(book_id,stage_id,filename,
                           source_html,source_sha256,actor_id,created_at) VALUES(?,?,?,?,?,?,?)""",
                        (book_id, stage_id, stage["filename"], stage["prefix_html"],
                         hashlib.sha256(stage["prefix_html"].encode("utf-8")).hexdigest(),
                         actor, now),
                    )
            except sqlite3.IntegrityError as exc:
                raise LibraryConflict("A chapter or preface destination became unavailable") from exc

            result = {
                "stage_id": stage_id, "status": "committed",
                "book_id": book_id, "created_book": request["book_mode"] == "create",
                "imported_count": len(writes),
                "skipped_count": len(candidates) - len(writes),
                "unassigned_prefix_preserved": prefix_present and request["prefix_policy"] == "preserve",
                "chapters": sorted(result_chapters, key=lambda chapter: chapter["sequence"]),
            }
            con.execute(
                """UPDATE book_import_stages
                   SET status='committed',payload_json=NULL,confirm_fingerprint=?,
                       commit_result_json=?,expires_at=? WHERE id=?""",
                (fingerprint, _packed(result), now + COMMITTED_TTL_SECONDS, stage_id),
            )
            return result
