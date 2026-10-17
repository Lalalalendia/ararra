"""SQLite-fenced, resumable source chapter jobs (MED checkpoint principles).

A job snapshots an actor-confirmed TOC. Every valid chapter is durably stored
before its lease is released; a crash can replay at most its unfinished chapter.
No automatic translation or library mutation. Completed items are immutable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict

from .source_catalog_med import source_book_id, validate_url
from .source_collect import (
    CollectedBook, CollectedChapter, SourceCatalog, MAX_BODY_CHARS,
)
from .source_site_storage import _fingerprint
from .storage import QueueStore

MAX_JOB_CHAPTERS = 2000
MAX_ACTIVE_JOBS_PER_ACTOR = 3
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_ITEM_BYTES = 1 * 1024 * 1024
MAX_MANUAL_RETRIES = 3


class SourceJobMissing(KeyError):
    pass


class SourceJobConflict(ValueError):
    pass


def _serialized(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _manifest_hash(serialized: str) -> str:
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class SourceJobStore:
    def __init__(self, store: QueueStore):
        self.store = store
        with store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS source_collect_jobs (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    idempotency_key TEXT,
                    source TEXT NOT NULL,
                    book_url TEXT NOT NULL,
                    catalog_sha256 TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('queued','running','complete','partial','cancelled')),
                    lease_token TEXT,
                    lease_expires_at REAL,
                    current_index INTEGER,
                    manual_retries INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(actor_id,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS ix_source_collect_job_claim
                    ON source_collect_jobs(status,lease_expires_at,created_at);
                CREATE TABLE IF NOT EXISTS source_collect_items (
                    job_id TEXT NOT NULL REFERENCES source_collect_jobs(id) ON DELETE CASCADE,
                    item_index INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    source_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ok','failed')),
                    body TEXT,
                    sha256 TEXT,
                    error TEXT,
                    bytes INTEGER NOT NULL,
                    saved_at REAL NOT NULL,
                    PRIMARY KEY(job_id,item_index),
                    UNIQUE(job_id,ordinal)
                );
            """)

    @staticmethod
    def _manifest(row: sqlite3.Row) -> list[dict]:
        payload = row["manifest_json"]
        if _manifest_hash(payload) != row["manifest_sha256"]:
            raise SourceJobConflict("Stored source manifest digest mismatch")
        chapters = json.loads(payload)
        if not isinstance(chapters,list) or not 1 <= len(chapters) <= MAX_JOB_CHAPTERS:
            raise SourceJobConflict("Invalid persisted source manifest")
        seen = set()
        for ch in chapters:
            if (not isinstance(ch,dict) or
                type(ch.get("ordinal")) is not int or
                not 1 <= ch["ordinal"] <= 10_000_000 or
                ch["ordinal"] in seen or
                not isinstance(ch.get("title"),str) or
                not isinstance(ch.get("source_id"),str) or
                not isinstance(ch.get("url"),str)):
                raise SourceJobConflict("Invalid persisted source chapter")
            seen.add(ch["ordinal"])
            validate_url(ch["url"],row["source"])
            if source_book_id(ch["url"],row["source"]) != source_book_id(row["book_url"],row["source"]):
                raise SourceJobConflict("Foreign source book chapter in manifest")
        return chapters

    @staticmethod
    def _owned(con: sqlite3.Connection, actor: str, job_id: str) -> sqlite3.Row:
        row = con.execute(
            "SELECT * FROM source_collect_jobs WHERE id=? AND actor_id=?",
            (job_id, actor),
        ).fetchone()
        if row is None:
            raise SourceJobMissing("Collection job not found")
        return row

    @staticmethod
    def _public(con: sqlite3.Connection, row: sqlite3.Row) -> dict:
        chapters = SourceJobStore._manifest(row)
        outcomes = con.execute(
            "SELECT ordinal,status,sha256,error FROM source_collect_items "
            "WHERE job_id=? ORDER BY item_index",
            (row["id"],),
        ).fetchall()
        passed = sum(item["status"] == "ok" for item in outcomes)
        failed = sum(item["status"] == "failed" for item in outcomes)
        done = {item["ordinal"] for item in outcomes}
        return {
            "job_id": row["id"], "status": row["status"],
            "source": row["source"], "book_url": row["book_url"],
            "catalog_sha256": row["catalog_sha256"],
            "manifest_sha256": row["manifest_sha256"],
            "total": len(chapters), "completed": passed, "failed": failed,
            "pending": len(chapters)-passed-failed,
            "selected_start": chapters[0]["ordinal"],
            "selected_end": chapters[-1]["ordinal"],
            "next_ordinal": next((ch["ordinal"] for ch in chapters if ch["ordinal"] not in done), None),
            "manual_retries": row["manual_retries"],
            "failures": [
                {"ordinal":item["ordinal"],"error":item["error"]}
                for item in outcomes if item["status"]=="failed"
            ][:40],
            "created_at":row["created_at"], "updated_at":row["updated_at"],
        }

    def create(
        self, actor: str, catalog: SourceCatalog, ordinals: list[int],
        *, idempotency_key: str | None = None,
    ) -> dict:
        if not 1 <= len(ordinals) <= MAX_JOB_CHAPTERS or len(set(ordinals)) != len(ordinals):
            raise SourceJobConflict("Select 1..2000 unique chapters")
        if any(type(n) is not int for n in ordinals):
            raise SourceJobConflict("Chapter numbers must be integers")
        if not catalog.chapters or catalog.catalog_sha256 != _fingerprint(catalog.chapters):
            raise SourceJobConflict("Catalog identity is stale")
        by_number = {ch.ordinal: ch for ch in catalog.chapters}
        if any(n not in by_number for n in ordinals):
            raise SourceJobConflict("Selected chapter missing from observed catalog")
        key = (idempotency_key or "").strip() or None
        if key is not None and len(key) > 128:
            raise SourceJobConflict("Idempotency key too long")
        chapters = [asdict(by_number[n]) for n in sorted(ordinals)]
        payload = _serialized(chapters)
        if len(payload.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise SourceJobConflict("Chapter manifest exceeds 2 MiB")
        digest = _manifest_hash(payload)
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if key:
                prior = con.execute(
                    "SELECT * FROM source_collect_jobs WHERE actor_id=? AND idempotency_key=?",
                    (actor,key),
                ).fetchone()
                if prior:
                    if prior["manifest_sha256"] != digest or prior["catalog_sha256"] != catalog.catalog_sha256:
                        raise SourceJobConflict("Idempotency key reused for another selection")
                    return self._public(con,prior)
            active = con.execute(
                "SELECT COUNT(*) FROM source_collect_jobs "
                "WHERE actor_id=? AND status IN ('queued','running')",
                (actor,),
            ).fetchone()[0]
            if active >= MAX_ACTIVE_JOBS_PER_ACTOR:
                raise SourceJobConflict("Too many active source collection jobs")
            job_id = str(uuid.uuid4())
            con.execute(
                "INSERT INTO source_collect_jobs("
                "id,actor_id,idempotency_key,source,book_url,catalog_sha256,"
                "manifest_sha256,manifest_json,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?, 'queued',?,?)",
                (job_id,actor,key,catalog.source,catalog.book_url,catalog.catalog_sha256,
                 digest,payload,now,now),
            )
            row = con.execute("SELECT * FROM source_collect_jobs WHERE id=?",(job_id,)).fetchone()
            return self._public(con,row)

    def get(self, actor: str, job_id: str) -> dict:
        with self.store._connect() as con:
            return self._public(con,self._owned(con,actor,job_id))

    def claim(self, *, lease_seconds: float = 180.0) -> dict | None:
        if lease_seconds < 30:
            raise ValueError("Source job lease must be at least 30 seconds")
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            candidates = con.execute(
                "SELECT * FROM source_collect_jobs "
                "WHERE status='queued' OR (status='running' AND lease_expires_at<?) "
                "ORDER BY created_at LIMIT 10",
                (now,),
            ).fetchall()
            for row in candidates:
                chapters = self._manifest(row)
                items = {
                    item["item_index"] for item in con.execute(
                        "SELECT item_index FROM source_collect_items WHERE job_id=?",
                        (row["id"],),
                    ).fetchall()
                }
                next_index = next((i for i in range(len(chapters)) if i not in items),None)
                if next_index is None:
                    failed = con.execute(
                        "SELECT COUNT(*) FROM source_collect_items WHERE job_id=? AND status='failed'",
                        (row["id"],),
                    ).fetchone()[0]
                    con.execute(
                        "UPDATE source_collect_jobs SET status=?,lease_token=NULL,"
                        "lease_expires_at=NULL,current_index=NULL,updated_at=? WHERE id=?",
                        ("partial" if failed else "complete",now,row["id"]),
                    )
                    continue
                token = str(uuid.uuid4())
                con.execute(
                    "UPDATE source_collect_jobs SET status='running',lease_token=?,"
                    "lease_expires_at=?,current_index=?,updated_at=? WHERE id=?",
                    (token,now+lease_seconds,next_index,now,row["id"]),
                )
                return {
                    "job_id":row["id"], "lease_token":token,
                    "source":row["source"],"book_url":row["book_url"],
                    "catalog_sha256":row["catalog_sha256"],
                    "manifest_sha256":row["manifest_sha256"],
                    "item_index":next_index,"chapter":chapters[next_index],
                }
        return None

    def finish(
        self, job_id: str, token: str, *,
        body: str | None = None, error: str | None = None,
    ) -> dict:
        if (body is None) == (error is None):
            raise ValueError("Either chapter body or failure receipt required")
        if body is not None and not 100 <= len(body) <= MAX_BODY_CHARS:
            raise ValueError("Invalid source chapter length")
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM source_collect_jobs WHERE id=?",(job_id,)).fetchone()
            if (row is None or row["status"] != "running" or
                row["lease_token"] != token or row["lease_expires_at"] is None or
                row["lease_expires_at"] <= now or row["current_index"] is None):
                raise SourceJobConflict("Stale source job lease")
            chapters = self._manifest(row)
            index = row["current_index"]
            ch = chapters[index]
            sha = hashlib.sha256(body.encode("utf-8")).hexdigest() if body is not None else None
            if sha is not None:
                duplicate = con.execute(
                    "SELECT ordinal FROM source_collect_items "
                    "WHERE job_id=? AND sha256=? AND status='ok'",
                    (job_id,sha),
                ).fetchone()
                if duplicate:
                    error = f"Duplicate body of chapter {duplicate['ordinal']}"
                    body = None
            status = "ok" if body is not None else "failed"
            con.execute(
                "INSERT INTO source_collect_items("
                "job_id,item_index,ordinal,source_id,url,status,body,sha256,error,bytes,saved_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id,index,ch["ordinal"],ch["source_id"],ch["url"],
                 status,body,sha,str(error)[:180] if error else None,
                 len(body.encode("utf-8")) if body is not None else 0,now),
            )
            total = con.execute(
                "SELECT COUNT(*) FROM source_collect_items WHERE job_id=?",(job_id,),
            ).fetchone()[0]
            failed = con.execute(
                "SELECT COUNT(*) FROM source_collect_items WHERE job_id=? AND status='failed'",
                (job_id,),
            ).fetchone()[0]
            state = ("partial" if failed else "complete") if total == len(chapters) else "queued"
            con.execute(
                "UPDATE source_collect_jobs SET status=?,lease_token=NULL,"
                "lease_expires_at=NULL,current_index=NULL,updated_at=? WHERE id=?",
                (state,now,job_id),
            )
            return self._public(con,con.execute(
                "SELECT * FROM source_collect_jobs WHERE id=?",(job_id,),
            ).fetchone())

    def retry_failures(self, actor: str, job_id: str) -> dict:
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._owned(con,actor,job_id)
            if row["status"] != "partial" or row["manual_retries"] >= MAX_MANUAL_RETRIES:
                raise SourceJobConflict("Source collection not eligible for retry")
            con.execute(
                "DELETE FROM source_collect_items WHERE job_id=? AND status='failed'",
                (job_id,),
            )
            con.execute(
                "UPDATE source_collect_jobs SET status='queued',manual_retries=manual_retries+1,"
                "updated_at=? WHERE id=?",
                (time.time(),job_id),
            )
            return self._public(con,self._owned(con,actor,job_id))

    def cancel(self, actor: str, job_id: str) -> dict:
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._owned(con,actor,job_id)
            if row["status"] not in ("queued","running","partial"):
                raise SourceJobConflict("Job is not cancellable")
            con.execute(
                "UPDATE source_collect_jobs SET status='cancelled',lease_token=NULL,"
                "lease_expires_at=NULL,current_index=NULL,updated_at=? WHERE id=?",
                (time.time(),job_id),
            )
            return self._public(con,self._owned(con,actor,job_id))

    def materialize(self, actor: str, job_id: str, ordinals: list[int]) -> CollectedBook:
        if not ordinals or len(ordinals)>20 or len(ordinals)!=len(set(ordinals)):
            raise SourceJobConflict("Choose 1..20 distinct chapters for staging")
        with self.store._connect() as con:
            row = self._owned(con,actor,job_id)
            chapters = self._manifest(row)
            selected = {ch["ordinal"]:ch for ch in chapters}
            if any(n not in selected for n in ordinals):
                raise SourceJobConflict("Chapter not in the collection manifest")
            by_ordinal = {
                item["ordinal"]:item for item in con.execute(
                    "SELECT * FROM source_collect_items WHERE job_id=?",
                    (job_id,),
                ).fetchall()
            }
            assembled=[]
            for n in sorted(ordinals):
                item=by_ordinal.get(n)
                if item is None or item["status"] != "ok" or item["body"] is None:
                    raise SourceJobConflict("Selected chapter not yet verified")
                body=item["body"]
                sha=hashlib.sha256(body.encode("utf-8")).hexdigest()
                if sha != item["sha256"] or len(body.encode("utf-8"))!=item["bytes"]:
                    raise SourceJobConflict("Stored source body checksum mismatch")
                ch=selected[n]
                if item["source_id"] != ch["source_id"] or item["url"] != ch["url"]:
                    raise SourceJobConflict("Stored source identity mismatch")
                assembled.append(CollectedChapter(n,ch["title"],ch["url"],ch["source_id"],body,sha))
            return CollectedBook(row["catalog_sha256"],tuple(sorted(ordinals)),tuple(assembled),())
