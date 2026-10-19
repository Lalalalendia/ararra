"""Immutable, team-readable archive of previously verified source originals.

Only **local** source checkpoints are adopted. This module never contacts
third-party sites, interprets permission to crawl, or reuses a revision to
start another crawler job. Shared archive bodies survive personal job deletion
until an explicit administrative purge. A bounded unique-byte budget protects
the SQLite/WAL filesystem from automatic unlimited retention.

Archive capture and source-job deletion commit in the SAME SQLite transaction.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing

from .source_catalog_med import chapter_identity, source_book_id, validate_url
from .storage import QueueStore

MAX_ARCHIVED_BODY_BYTES = 1 * 1024 * 1024
MAX_SHARED_ARCHIVE_BYTES = 256 * 1024 * 1024
_HEX64 = re.compile(r"^[a-f0-9]{64}$")


class SourceArchiveMissing(KeyError):
    pass


class SourceArchiveConflict(ValueError):
    pass


def _digest(*parts: str) -> str:
    # Unambiguous framing even for exotic legal URLs and titles.
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SourceOriginalArchive:
    def __init__(self, store: QueueStore, *,
                 max_bytes: int = MAX_SHARED_ARCHIVE_BYTES):
        if type(max_bytes) is not int or not 1024 * 1024 <= max_bytes <= MAX_SHARED_ARCHIVE_BYTES:
            raise ValueError("Invalid archive unique-body budget")
        self.store = store
        self.max_bytes = max_bytes
        with store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS source_original_bodies (
                    sha256 TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    bytes INTEGER NOT NULL CHECK(bytes > 0 AND bytes <= 1048576),
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_original_revisions (
                    revision_id TEXT PRIMARY KEY,
                    archive_book_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_book_key TEXT NOT NULL,
                    book_url TEXT NOT NULL,
                    chapter_source_id TEXT NOT NULL,
                    chapter_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    body_sha256 TEXT NOT NULL
                        REFERENCES source_original_bodies(sha256) ON DELETE RESTRICT,
                    collector_member_id TEXT NOT NULL,
                    original_job_id TEXT NOT NULL,
                    captured_at REAL NOT NULL,
                    UNIQUE(source,source_book_key,chapter_source_id,chapter_url,body_sha256)
                );
                CREATE INDEX IF NOT EXISTS ix_source_original_archive_books
                    ON source_original_revisions(archive_book_id,ordinal,captured_at);
                CREATE INDEX IF NOT EXISTS ix_source_original_archive_captured
                    ON source_original_revisions(captured_at DESC);
                CREATE TABLE IF NOT EXISTS source_original_purge_audit (
                    revision_id TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    admin_member_id TEXT NOT NULL,
                    purged_at REAL NOT NULL,
                    PRIMARY KEY(revision_id,purged_at)
                );
            """)

    @staticmethod
    def _identity(source: str, book_url: str, chapter_url: str,
                  source_id: str) -> tuple[str, str]:
        try:
            key = source_book_id(book_url, source)
            validate_url(chapter_url, source)
            if chapter_identity(chapter_url, source, key) != source_id:
                raise SourceArchiveConflict("Archive chapter source identity mismatch")
        except ValueError as exc:
            raise SourceArchiveConflict("Archive chapter is outside its reviewed source book") from exc
        return key, _digest(source, key)

    def capture_before_delete(self, con: sqlite3.Connection, job: sqlite3.Row,
                              chapters: list[dict]) -> dict:
        """Called under BEGIN IMMEDIATE. Any failure rolls back job deletion.

        Reads one chapter per SQLite cursor step; never fetchall()s a full book.
        The source item is checked *again* before being stored in a separate
        immutable archive. Duplicate exact body bytes occupy one body row.
        """
        if not con.in_transaction:
            raise SourceArchiveConflict("An archive capture needs an atomic write transaction")
        now = time.time()
        used = int(con.execute(
            "SELECT COALESCE(SUM(bytes),0) FROM source_original_bodies",
        ).fetchone()[0])
        originals = 0
        added_bodies = 0
        added_bytes = 0
        rows = con.execute(
            "SELECT item_index,ordinal,source_id,url,status,body,sha256,bytes "
            "FROM source_collect_items WHERE job_id=? ORDER BY item_index",
            (job["id"],),
        )
        for item in rows:
            if item["status"] != "ok":
                continue
            index = item["item_index"]
            if type(index) is not int or not 0 <= index < len(chapters):
                raise SourceArchiveConflict("Archive checkpoint index mismatch")
            chapter = chapters[index]
            body = item["body"]
            data = body.encode("utf-8") if isinstance(body, str) else None
            if (
                item["ordinal"] != chapter["ordinal"] or
                item["source_id"] != chapter["source_id"] or
                item["url"] != chapter["url"] or
                data is None or not 0 < len(data) <= MAX_ARCHIVED_BODY_BYTES or
                item["bytes"] != len(data) or
                not isinstance(item["sha256"], str) or
                _HEX64.fullmatch(item["sha256"]) is None or
                hashlib.sha256(data).hexdigest() != item["sha256"]
            ):
                raise SourceArchiveConflict("Archive checkpoint integrity mismatch")
            key, book_id = self._identity(job["source"],job["book_url"],
                                          chapter["url"],chapter["source_id"])
            sha = item["sha256"]
            stored = con.execute(
                "SELECT bytes,body FROM source_original_bodies WHERE sha256=?", (sha,),
            ).fetchone()
            if stored is None:
                if used + len(data) > self.max_bytes:
                    raise SourceArchiveConflict(
                        "Shared archive quota exceeded: job deletion denied to preserve originals"
                    )
                con.execute(
                    "INSERT INTO source_original_bodies(sha256,body,bytes,created_at)"
                    " VALUES(?,?,?,?)", (sha,body,len(data),now),
                )
                used += len(data)
                added_bytes += len(data)
                added_bodies += 1
            elif stored["bytes"] != len(data) or stored["body"] != body:
                raise SourceArchiveConflict("Existing immutable archive body mismatch")
            revision_id = _digest(
                job["source"],key,chapter["source_id"],chapter["url"],sha,
            )
            con.execute(
                "INSERT OR IGNORE INTO source_original_revisions"
                "(revision_id,archive_book_id,source,source_book_key,book_url,"
                "chapter_source_id,chapter_url,title,ordinal,body_sha256,"
                "collector_member_id,original_job_id,captured_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (revision_id,book_id,job["source"],key,job["book_url"],
                 chapter["source_id"],chapter["url"],chapter["title"],
                 chapter["ordinal"],sha,job["actor_id"],job["id"],now),
            )
            originals += 1
        return {
            "archived_verified_chapters": originals,
            "archive_new_unique_bodies": added_bodies,
            "archive_added_unique_bytes": added_bytes,
            "archive_scope": "team_shared_immutable_originals",
        }

    def list_books(self, *, limit: int = 30, offset: int = 0) -> dict:
        if type(limit) is not int or not 1 <= limit <= 50 or (
            type(offset) is not int or not 0 <= offset <= 10_000
        ):
            raise ValueError("Invalid archive index window")
        with closing(self.store._connect()) as con:
            con.execute("BEGIN")
            rows = con.execute(
                "SELECT archive_book_id,source,source_book_key,"
                " MIN(book_url) AS book_url,COUNT(*) AS revisions,"
                " COUNT(DISTINCT chapter_source_id) AS chapter_count,"
                " MAX(captured_at) AS updated_at"
                " FROM source_original_revisions GROUP BY archive_book_id"
                " ORDER BY updated_at DESC,archive_book_id DESC LIMIT ? OFFSET ?",
                (limit+1,offset),
            ).fetchall()
            return {
                "books":[dict(x) for x in rows[:limit]],
                "has_more":len(rows)>limit,
                "scope":"team_shared_original_archive",
                "read_only":True,
            }

    def chapters(self, book_id: str, *, limit: int = 50, offset: int = 0) -> dict:
        if not isinstance(book_id,str) or _HEX64.fullmatch(book_id) is None:
            raise SourceArchiveMissing("Archive book not found")
        if type(limit) is not int or not 1 <= limit <= 100 or (
            type(offset) is not int or not 0 <= offset <= 10_000
        ):
            raise ValueError("Invalid archive chapter window")
        with closing(self.store._connect()) as con:
            con.execute("BEGIN")
            records = con.execute(
                "SELECT revision_id,archive_book_id,source,book_url,"
                "chapter_source_id,chapter_url,title,ordinal,body_sha256,"
                "collector_member_id,captured_at "
                "FROM source_original_revisions WHERE archive_book_id=?"
                " ORDER BY ordinal,captured_at DESC,revision_id LIMIT ? OFFSET ?",
                (book_id,limit+1,offset),
            ).fetchall()
            if not records:
                exists=con.execute(
                    "SELECT 1 FROM source_original_revisions WHERE archive_book_id=?",
                    (book_id,),
                ).fetchone()
                if not exists:
                    raise SourceArchiveMissing("Archive book not found")
            return {
                "archive_book_id":book_id,
                "chapters":[dict(x) for x in records[:limit]],
                "has_more":len(records)>limit,
                "offset":offset,
                "scope":"team_shared_original_archive",
            }

    def chapter(self, revision_id: str) -> dict:
        if not isinstance(revision_id,str) or _HEX64.fullmatch(revision_id) is None:
            raise SourceArchiveMissing("Archived revision not found")
        with closing(self.store._connect()) as con:
            con.execute("BEGIN")
            item = con.execute(
                "SELECT r.*,b.body,b.bytes FROM source_original_revisions AS r "
                "JOIN source_original_bodies AS b ON b.sha256=r.body_sha256 "
                "WHERE r.revision_id=?", (revision_id,),
            ).fetchone()
            if item is None:
                raise SourceArchiveMissing("Archived revision not found")
            key, book_id = self._identity(
                item["source"],item["book_url"],item["chapter_url"],
                item["chapter_source_id"],
            )
            body = item["body"]
            data = body.encode("utf-8") if isinstance(body,str) else None
            if (item["source_book_key"] != key or
                item["archive_book_id"] != book_id or
                revision_id != _digest(
                    item["source"],key,item["chapter_source_id"],
                    item["chapter_url"],item["body_sha256"],
                ) or data is None or len(data) != item["bytes"] or
                hashlib.sha256(data).hexdigest() != item["body_sha256"]):
                raise SourceArchiveConflict("Archived original checksum or identity mismatch")
            return {
                "revision_id":revision_id,
                "archive_book_id":book_id,
                "source":item["source"],
                "book_url":item["book_url"],
                "url":item["chapter_url"],
                "source_id":item["chapter_source_id"],
                "title":item["title"],
                "ordinal":item["ordinal"],
                "sha256":item["body_sha256"],
                "bytes":item["bytes"],
                "body":body,
                "collector_member_id":item["collector_member_id"],
                "scope":"single_verified_archived_original",
            }

    def purge_revision(self, revision_id: str, *, admin_member_id: str) -> dict:
        """Audit one admin-only archive takedown, remove orphaned body bytes."""
        if not isinstance(revision_id,str) or _HEX64.fullmatch(revision_id) is None:
            raise SourceArchiveMissing("Archived revision not found")
        if (not isinstance(admin_member_id,str) or not admin_member_id or
            len(admin_member_id)>128):
            raise ValueError("Administrator identity required for archive purge")
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT body_sha256 FROM source_original_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            if row is None:
                raise SourceArchiveMissing("Archived revision not found")
            # Keep a metadata-only audit; do not persist deleted prose.
            con.execute(
                "INSERT INTO source_original_purge_audit("
                "revision_id,body_sha256,admin_member_id,purged_at)"
                " VALUES(?,?,?,?)",
                (revision_id,row["body_sha256"],admin_member_id,time.time()),
            )
            con.execute("DELETE FROM source_original_revisions WHERE revision_id=?",
                        (revision_id,))
            released = con.execute(
                "DELETE FROM source_original_bodies WHERE sha256=? AND NOT EXISTS("
                "SELECT 1 FROM source_original_revisions WHERE body_sha256=?)",
                (row["body_sha256"],row["body_sha256"]),
            ).rowcount
            return {
                "revision_id":revision_id,
                "purged":True,
                "removed_orphan_body":bool(released),
                "source_jobs_unchanged":True,
                "book_library_unchanged":True,
            }
