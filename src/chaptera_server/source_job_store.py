"""SQLite-fenced, resumable source chapter jobs (MED checkpoint principles).

A job snapshots an actor-confirmed TOC. Every valid chapter is durably stored
before its lease is released; a crash can replay at most its unfinished chapter.
No automatic translation or library mutation. Completed items are immutable.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from dataclasses import asdict

from .source_catalog_med import source_book_id, validate_url
from .source_collect import (
    CollectedBook, CollectedChapter, SourceCatalog, MAX_BODY_CHARS,
)
from .source_site_storage import _fingerprint
from .source_selection_store import _digest as _selection_digest
from .source_original_archive import SourceOriginalArchive, SourceArchiveConflict
from .storage import QueueStore

MAX_JOB_CHAPTERS = 2000
MAX_ACTIVE_JOBS_PER_ACTOR = 3
MAX_ACTIVE_JOBS_GLOBAL = 32
MAX_ACTIVE_JOBS_PER_SITE = 12
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_ITEM_BYTES = 1 * 1024 * 1024
MAX_JOB_BYTES = 256 * 1024 * 1024
MAX_MANUAL_RETRIES = 3


class SourceJobMissing(KeyError):
    pass


class SourceJobConflict(ValueError):
    pass


def _serialized(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _manifest_hash(serialized: str) -> str:
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


DEFAULT_ACTOR_QUOTA_BYTES = 512 * 1024 * 1024
DEFAULT_GLOBAL_QUOTA_BYTES = 2048 * 1024 * 1024


class SourceJobStore:
    def __init__(
        self, store: QueueStore, *,
        actor_quota_bytes: int = DEFAULT_ACTOR_QUOTA_BYTES,
        global_quota_bytes: int = DEFAULT_GLOBAL_QUOTA_BYTES,
    ):
        if (type(actor_quota_bytes) is not int or type(global_quota_bytes) is not int
            or not 1024 * 1024 <= actor_quota_bytes <= global_quota_bytes
            or global_quota_bytes > 1024 * 1024 * 1024 * 1024):
            raise ValueError("Invalid durable source quota configuration")
        self.store = store
        self.actor_quota_bytes = actor_quota_bytes
        self.global_quota_bytes = global_quota_bytes
        with store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS source_collect_jobs (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
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
                CREATE INDEX IF NOT EXISTS ix_source_collect_job_actor_recent
                    ON source_collect_jobs(actor_id,updated_at DESC);
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
                CREATE TABLE IF NOT EXISTS source_collect_actor_turns (
                    actor_id TEXT PRIMARY KEY,
                    last_turn INTEGER NOT NULL DEFAULT 0
                );
            """)
            # Both the API and independent worker may initialize the same
            # legacy database: serialize the additive, reversible migration.
            con.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in con.execute(
                "PRAGMA table_info(source_collect_jobs)")}
            if "ready_at" not in columns:
                con.execute("ALTER TABLE source_collect_jobs "
                            "ADD COLUMN ready_at REAL NOT NULL DEFAULT 0")
            if "dispatch_turn" not in columns:
                con.execute("ALTER TABLE source_collect_jobs "
                            "ADD COLUMN dispatch_turn INTEGER NOT NULL DEFAULT 0")
            if "defer_reason" not in columns:
                con.execute("ALTER TABLE source_collect_jobs "
                            "ADD COLUMN defer_reason TEXT")
            if "selection_mode" not in columns:
                con.execute("ALTER TABLE source_collect_jobs "
                            "ADD COLUMN selection_mode TEXT NOT NULL DEFAULT 'range'")
            if "selection_sha256" not in columns:
                con.execute("ALTER TABLE source_collect_jobs "
                            "ADD COLUMN selection_sha256 TEXT")
        # Separate, immutable, bounded team archive. No legacy data is
        # copied merely by starting the API or opening the old database.
        self.archive = SourceOriginalArchive(store)

    @staticmethod
    def _used_bytes(con: sqlite3.Connection, actor: str) -> tuple[int, int]:
        """Atomic exact logical source-body usage, including all old jobs.

        No cached ledger can drift after restore or a foreign-key cascade.
        The caller must BEGIN IMMEDIATE for writes. One read query scans
        source checkpoints using SQLite's own INTEGER sums.
        """
        row = con.execute(
            "SELECT COALESCE(SUM(i.bytes),0) AS all_bytes,"
            " COALESCE(SUM(CASE WHEN j.actor_id=? THEN i.bytes ELSE 0 END),0) AS actor_bytes "
            "FROM source_collect_items AS i "
            "JOIN source_collect_jobs AS j ON j.id=i.job_id "
            "WHERE i.status='ok'",
            (actor,),
        ).fetchone()
        return int(row["actor_bytes"]), int(row["all_bytes"])

    def usage(self, actor: str) -> dict:
        """Actor-visible quota; never expose other members' total byte usage."""
        with self.store._connect() as con:
            actor_bytes, global_bytes = self._used_bytes(con, actor)
        return {
            "actor_used_bytes": actor_bytes,
            "actor_limit_bytes": self.actor_quota_bytes,
            "effective_remaining_bytes": min(
                max(self.actor_quota_bytes - actor_bytes, 0),
                max(self.global_quota_bytes - global_bytes, 0),
            ),
            "can_collect": actor_bytes < self.actor_quota_bytes
                and global_bytes < self.global_quota_bytes,
            "scope": "verified_source_bodies_only",
        }

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
        if row["selection_mode"] == "ordered":
            if row["selection_sha256"] != _selection_digest(
                row["catalog_sha256"], [ch["ordinal"] for ch in chapters]
            ):
                raise SourceJobConflict("Ordered selection digest mismatch")
        elif row["selection_mode"] != "range" or row["selection_sha256"] is not None:
            raise SourceJobConflict("Invalid stored source selection mode")
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
            "catalog_id": row["catalog_id"],
            "catalog_sha256": row["catalog_sha256"],
            "manifest_sha256": row["manifest_sha256"],
            "total": len(chapters), "completed": passed, "failed": failed,
            "source_bytes": sum(item["bytes"] for item in con.execute(
                "SELECT bytes FROM source_collect_items WHERE job_id=? AND status='ok'",
                (row["id"],),
            ).fetchall()),
            "pending": len(chapters)-passed-failed,
            "selected_start": min(ch["ordinal"] for ch in chapters),
            "selected_end": max(ch["ordinal"] for ch in chapters),
            "selection_mode": row["selection_mode"],
            "selection_sha256": row["selection_sha256"],
            "selected_ordinals": ([ch["ordinal"] for ch in chapters]
                                  if row["selection_mode"] == "ordered" else None),
            "next_ordinal": next((ch["ordinal"] for ch in chapters if ch["ordinal"] not in done), None),
            "manual_retries": row["manual_retries"],
            "next_attempt_at": (row["ready_at"] if row["status"] == "queued"
                                and row["ready_at"] > time.time() else None),
            "waiting_reason": (row["defer_reason"] if row["status"] == "queued"
                               and row["ready_at"] > time.time() else None),
            "failures": [
                {"ordinal":item["ordinal"],"error":item["error"]}
                for item in outcomes if item["status"]=="failed"
            ][:40],
            "created_at":row["created_at"], "updated_at":row["updated_at"],
        }

    def create(
        self, actor: str, catalog_id: str, catalog: SourceCatalog, ordinals: list[int],
        *, idempotency_key: str | None = None,
        preserve_order: bool = False,
        selection_sha256: str | None = None,
        selection_id: str | None = None,
    ) -> dict:
        if (type(preserve_order) is not bool or
            (preserve_order and (not isinstance(selection_sha256, str) or
                                 re.fullmatch(r"[a-f0-9]{64}", selection_sha256) is None or
                                 not isinstance(selection_id, str) or not selection_id)) or
            (not preserve_order and (selection_sha256 is not None or
                                     selection_id is not None))):
            raise SourceJobConflict("Invalid selection mode or digest")
        if not catalog_id or len(catalog_id) > 128:
            raise SourceJobConflict("Valid source catalog reference required")
        if not 1 <= len(ordinals) <= MAX_JOB_CHAPTERS or len(set(ordinals)) != len(ordinals):
            raise SourceJobConflict("Select 1..2000 unique chapters")
        if any(type(n) is not int for n in ordinals):
            raise SourceJobConflict("Chapter numbers must be integers")
        if not catalog.chapters or catalog.catalog_sha256 != _fingerprint(catalog.chapters, catalog.source, catalog.book_url):
            raise SourceJobConflict("Catalog identity is stale")
        by_number = {ch.ordinal: ch for ch in catalog.chapters}
        if any(n not in by_number for n in ordinals):
            raise SourceJobConflict("Selected chapter missing from observed catalog")
        key = (idempotency_key or "").strip() or None
        if key is not None and len(key) > 128:
            raise SourceJobConflict("Idempotency key too long")
        chapters = [asdict(by_number[n]) for n in (ordinals if preserve_order else sorted(ordinals))]
        selection_mode = "ordered" if preserve_order else "range"
        payload = _serialized(chapters)
        if len(payload.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise SourceJobConflict("Chapter manifest exceeds 2 MiB")
        digest = _manifest_hash(payload)
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if preserve_order:
                plan = con.execute(
                    "SELECT catalog_id,catalog_sha256,selection_sha256,"
                    "ordered_ordinals_json,expires_at FROM source_book_selections "
                    "WHERE id=? AND actor_id=?",
                    (selection_id,actor),
                ).fetchone()
                origin = con.execute(
                    "SELECT catalog_sha256,expires_at FROM source_book_catalogs "
                    "WHERE id=? AND actor_id=?",
                    (catalog_id,actor),
                ).fetchone()
                if (plan is None or origin is None or
                    plan["expires_at"]<=now or origin["expires_at"]<=now or
                    plan["catalog_id"]!=catalog_id or
                    plan["catalog_sha256"]!=catalog.catalog_sha256 or
                    origin["catalog_sha256"]!=catalog.catalog_sha256 or
                    plan["selection_sha256"]!=selection_sha256 or
                    plan["selection_sha256"]!=_selection_digest(catalog.catalog_sha256,ordinals) or
                    plan["ordered_ordinals_json"]!=json.dumps(ordinals,separators=(",",":"))):
                    raise SourceJobConflict("Saved selection changed before job creation")
            if key:
                prior = con.execute(
                    "SELECT * FROM source_collect_jobs WHERE actor_id=? AND idempotency_key=?",
                    (actor,key),
                ).fetchone()
                if prior:
                    if (prior["manifest_sha256"] != digest or
                        prior["catalog_sha256"] != catalog.catalog_sha256 or
                        prior["catalog_id"] != catalog_id or
                        prior["selection_mode"] != selection_mode or
                        prior["selection_sha256"] != selection_sha256):
                        raise SourceJobConflict("Idempotency key reused for another selection")
                    return self._public(con,prior)
            actor_used, global_used = self._used_bytes(con, actor)
            if actor_used >= self.actor_quota_bytes or global_used >= self.global_quota_bytes:
                raise SourceJobConflict(
                    "Verified source storage quota exhausted; delete completed source captures"
                )
            active = con.execute(
                "SELECT COUNT(*) FROM source_collect_jobs "
                "WHERE actor_id=? AND status IN ('queued','running')",
                (actor,),
            ).fetchone()[0]
            if active >= MAX_ACTIVE_JOBS_PER_ACTOR:
                raise SourceJobConflict("Too many active source collection jobs")
            global_active = con.execute(
                "SELECT COUNT(*) FROM source_collect_jobs WHERE status IN ('queued','running')"
            ).fetchone()[0]
            if global_active >= MAX_ACTIVE_JOBS_GLOBAL:
                raise SourceJobConflict("Shared source job queue is full")
            site_active = con.execute(
                "SELECT COUNT(*) FROM source_collect_jobs "
                "WHERE source=? AND status IN ('queued','running')",
                (catalog.source,),
            ).fetchone()[0]
            if site_active >= MAX_ACTIVE_JOBS_PER_SITE:
                raise SourceJobConflict("This source site already has too many queued jobs")
            job_id = str(uuid.uuid4())
            con.execute(
                "INSERT INTO source_collect_jobs("
                "id,actor_id,catalog_id,idempotency_key,source,book_url,catalog_sha256,"
                "manifest_sha256,manifest_json,selection_mode,selection_sha256,"
                "status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?, 'queued',?,?)",
                (job_id,actor,catalog_id,key,catalog.source,catalog.book_url,catalog.catalog_sha256,
                 digest,payload,selection_mode,selection_sha256,now,now),
            )
            row = con.execute("SELECT * FROM source_collect_jobs WHERE id=?",(job_id,)).fetchone()
            return self._public(con,row)

    def get(self, actor: str, job_id: str) -> dict:
        with self.store._connect() as con:
            return self._public(con,self._owned(con,actor,job_id))

    def list_recent(self, actor: str, *, limit: int = 20) -> list[dict]:
        """Actor-only recent job index; never disclose stored source prose."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Recent source job limit must be 1..50")
        with self.store._connect() as con:
            rows = con.execute(
                "SELECT * FROM source_collect_jobs WHERE actor_id=? "
                "ORDER BY updated_at DESC,id DESC LIMIT ?",
                (actor,limit),
            ).fetchall()
            return [self._public(con,row) for row in rows]

    def delete_finished(self, actor: str, job_id: str) -> dict:
        """Explicitly purge ONLY a terminal crawler job, never its imports.

        A concurrent worker cannot commit into a deleted job: all state
        transitions and the delete share a SQLite BEGIN IMMEDIATE gate.
        """
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._owned(con,actor,job_id)
            if row["status"] in ("queued","running"):
                raise SourceJobConflict("Cancel the running collection before deleting it")
            if row["status"] not in ("complete","partial","cancelled"):
                raise SourceJobConflict("Collection is not deletable")
            # Preserve every verified source original BEFORE the personal
            # capture is removed. Archive capture and the FK cascade share
            # this transaction: no half-archive, no silent text loss.
            try:
                retained = self.archive.capture_before_delete(
                    con,row,self._manifest(row),
                )
            except SourceArchiveConflict as exc:
                raise SourceJobConflict(str(exc)) from exc
            count = con.execute(
                "SELECT COUNT(*) FROM source_collect_items WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
            bytes_freed = con.execute(
                "SELECT COALESCE(SUM(bytes),0) FROM source_collect_items WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
            con.execute(
                "DELETE FROM source_collect_jobs WHERE id=? AND actor_id=?",
                (job_id,actor),
            )
            # SQLite foreign keys are enabled by QueueStore._connect(); the
            # source_collect_items rows cascade. Staged/imported chapter data
            # belong to different tables and are untouched.
            left = con.execute(
                "SELECT COUNT(*) FROM source_collect_items WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
            if left:
                raise SourceJobConflict("Crawler checkpoints were not deleted")
            return {
                "deleted":True,"job_id":job_id,
                "deleted_checkpoints":count,"freed_source_bytes":bytes_freed,
                "library_unchanged":True,
                **retained,
            }

    def claim(
        self, *, lease_seconds: float = 180.0,
        only_job_id: str | None = None,
    ) -> dict | None:
        """Claim any ready job, or exactly one explicitly targeted pilot job.

        The optional SQL filter is inside the same transactional claim, so a
        targeted one-shot cannot accidentally touch another moderator's job.
        """
        if lease_seconds < 30:
            raise ValueError("Source job lease must be at least 30 seconds")
        if only_job_id is not None:
            if (type(only_job_id) is not str or len(only_job_id) != 36 or
                not re.fullmatch(r"[a-f0-9-]{36}", only_job_id) or
                str(uuid.UUID(only_job_id)) != only_job_id):
                raise ValueError("One-shot source job ID must be a canonical UUID")
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            candidates = con.execute(
                "SELECT j.* FROM source_collect_jobs AS j "
                "LEFT JOIN source_collect_actor_turns AS a ON a.actor_id=j.actor_id "
                "WHERE ((j.status='queued' AND j.ready_at<=?) "
                "   OR (j.status='running' AND j.lease_expires_at<?)) "
                "AND (? IS NULL OR j.id=?) "
                "ORDER BY COALESCE(a.last_turn,0),j.dispatch_turn,j.created_at "
                "LIMIT 24",
                (now,now,only_job_id,only_job_id),
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
                # A transactionally durable round-robin turn is shared by
                # every worker process. One moderator with three queued jobs
                # does not get triple priority over another with one job.
                turn = con.execute(
                    "SELECT COALESCE(MAX(last_turn),0)+1 FROM source_collect_actor_turns"
                ).fetchone()[0]
                con.execute(
                    "INSERT INTO source_collect_actor_turns(actor_id,last_turn) "
                    "VALUES(?,?) ON CONFLICT(actor_id) "
                    "DO UPDATE SET last_turn=excluded.last_turn",
                    (row["actor_id"],turn),
                )
                con.execute(
                    "UPDATE source_collect_jobs SET status='running',lease_token=?,"
                    "lease_expires_at=?,current_index=?,updated_at=?,"
                    "dispatch_turn=?,ready_at=0,defer_reason=NULL WHERE id=?",
                    (token,now+lease_seconds,next_index,now,turn,row["id"]),
                )
                return {
                    "job_id":row["id"], "lease_token":token,
                    "source":row["source"],"book_url":row["book_url"],
                    "catalog_sha256":row["catalog_sha256"],
                    "manifest_sha256":row["manifest_sha256"],
                    "item_index":next_index,"chapter":chapters[next_index],
                }
        return None

    def defer(self, job_id: str, token: str, *,
              retry_after: int, reason: str) -> dict:
        """Requeue a busy-site chapter without an erroneous failure receipt.

        The exact missing item stays unmodified. Stale/cancelled leases cannot
        put the job back in the queue or erase a completed chapter.
        """
        if type(retry_after) is not int or not 1 <= retry_after <= 86400:
            raise ValueError("Site retry delay must be 1..86400 seconds")
        if reason not in {"upstream_cooldown","upstream_429","request_interval",
                          "inflight_request","daily_site_budget","clock_regression"}:
            raise ValueError("Unrecognized source admission delay")
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM source_collect_jobs WHERE id=?", (job_id,),
            ).fetchone()
            if (row is None or row["status"] != "running" or
                row["lease_token"] != token or row["lease_expires_at"] is None or
                row["lease_expires_at"] <= now):
                raise SourceJobConflict("Stale source job lease")
            con.execute(
                "UPDATE source_collect_jobs SET status='queued',lease_token=NULL,"
                "lease_expires_at=NULL,current_index=NULL,ready_at=?,"
                "defer_reason=?,updated_at=? WHERE id=?",
                (now+retry_after,reason,now,job_id),
            )
            return self._public(con,self._owned(con,row["actor_id"],job_id))

    def finish(
        self, job_id: str, token: str, *,
        body: str | None = None, error: str | None = None,
    ) -> dict:
        if (body is None) == (error is None):
            raise ValueError("Either chapter body or failure receipt required")
        if body is not None and not 100 <= len(body) <= MAX_BODY_CHARS:
            raise ValueError("Invalid source chapter length")
        if body is not None and len(body.encode("utf-8")) > MAX_ITEM_BYTES:
            raise ValueError("Source chapter exceeds durable byte limit")
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
            if body is not None:
                item_bytes = len(body.encode("utf-8"))
                used_bytes = con.execute(
                    "SELECT COALESCE(SUM(bytes),0) FROM source_collect_items "
                    "WHERE job_id=? AND status='ok'",
                    (job_id,),
                ).fetchone()[0]
                actor_used, global_used = self._used_bytes(con, row["actor_id"])
                if used_bytes + item_bytes > MAX_JOB_BYTES:
                    error = "Collection exceeds 256 MiB verified-source budget"
                    body = None
                elif actor_used + item_bytes > self.actor_quota_bytes:
                    error = "Member's verified source quota exhausted; clean up older collections"
                    body = None
                elif global_used + item_bytes > self.global_quota_bytes:
                    error = "Shared verified source storage quota exhausted; contact administrator"
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
            for n in ordinals:
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
                assembled.append(CollectedChapter(
                    n, ch["title"], ch["url"], ch["source_id"], body, sha,
                    ch.get("entry_type", "unknown"), ch.get("author_number"),
                    ch.get("classification_source", "none"),
                    ch.get("classification_confidence", "unverified"),
                ))
            return CollectedBook(row["catalog_sha256"],tuple(ordinals),tuple(assembled),())
