"""Owner-scoped immutable Timotxt RAW review snapshots, separate from verified jobs.

Review rows are NOT source_collect_items.ok, do not enter shared archives,
staging, TXT exports or translation. The caller owns the SQLite lease fence.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from typing import Any

from .source_timotxt_normalization import (
    derive_timotxt_candidate, learn_timotxt_mapping,
)
from .storage import QueueStore

MAX_REVIEW_ITEM_BYTES = 1024 * 1024
MAX_REVIEW_JOB_BYTES = 16 * 1024 * 1024
MAX_REVIEW_ACTOR_BYTES = 64 * 1024 * 1024
MAX_REVIEW_GLOBAL_BYTES = 256 * 1024 * 1024
MAX_DERIVED_GLOBAL_BYTES = 128 * 1024 * 1024
MAX_CANDIDATES_PER_SNAPSHOT = 3
SHA = re.compile(r"^[a-f0-9]{64}$")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SourceReviewStore:
    """Bounded review-only SQLite tables; an old job needs no data migration."""

    def __init__(self, store: QueueStore):
        self.store = store
        with store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("""
                CREATE TABLE IF NOT EXISTS source_collect_review_raw (
                    snapshot_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES source_collect_jobs(id)
                        ON DELETE CASCADE,
                    item_index INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    source_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL,
                    raw_bytes INTEGER NOT NULL CHECK(raw_bytes>0),
                    created_at REAL NOT NULL,
                    UNIQUE(job_id,item_index,raw_sha256)
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS ix_source_review_job
                ON source_collect_review_raw(job_id,ordinal,created_at)
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS source_collect_review_derived (
                    snapshot_id TEXT NOT NULL REFERENCES source_collect_review_raw(snapshot_id)
                        ON DELETE CASCADE,
                    mapping_fingerprint TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    normalized_sha256 TEXT NOT NULL,
                    normalized_bytes INTEGER NOT NULL CHECK(normalized_bytes>0),
                    status TEXT NOT NULL CHECK(status IN
                        ('needs_review','normalized_candidate')),
                    replacements INTEGER NOT NULL,
                    unresolved_glyphs INTEGER NOT NULL,
                    mapping_used_json TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(snapshot_id,mapping_fingerprint)
                )
            """)

    def _record_in_transaction(
        self, con: sqlite3.Connection, row: sqlite3.Row,
        chapter: dict, item_index: int, raw: str, now: float,
    ) -> str | None:
        """Called after a valid lease was checked, under BEGIN IMMEDIATE.

        None means quota exceeded: caller must write a *failed*, non-reviewed
        checkpoint rather than pretending RAW was captured.
        """
        if row["source"] != "timotxt" or not isinstance(raw,str):
            raise ValueError("Only original Timotxt text may be quarantined")
        encoded = raw.encode("utf-8")
        size = len(encoded)
        if not 100 <= len(raw) <= 1_000_000 or not 1 <= size <= MAX_REVIEW_ITEM_BYTES:
            return None
        from .source_timotxt_normalization import has_suspect_glyphs
        if not has_suspect_glyphs(raw):
            raise ValueError("Clean original may not enter Timotxt quarantine")
        digest = hashlib.sha256(encoded).hexdigest()
        old = con.execute(
            "SELECT * FROM source_collect_review_raw "
            "WHERE job_id=? AND item_index=? AND raw_sha256=?",
            (row["id"],item_index,digest),
        ).fetchone()
        if old is not None:
            if (old["raw_text"] != raw or old["ordinal"] != chapter["ordinal"]
                    or old["source_id"] != chapter["source_id"]
                    or old["url"] != chapter["url"] or old["raw_bytes"] != size):
                from .source_job_store import SourceJobConflict
                raise SourceJobConflict("Existing review snapshot identity changed")
            return str(old["snapshot_id"])
        totals = con.execute(
            """SELECT COALESCE(SUM(r.raw_bytes),0) AS all_bytes,
                      COALESCE(SUM(CASE WHEN j.actor_id=? THEN r.raw_bytes ELSE 0 END),0)
                          AS actor_bytes,
                      COALESCE(SUM(CASE WHEN r.job_id=? THEN r.raw_bytes ELSE 0 END),0)
                          AS job_bytes
               FROM source_collect_review_raw r
               JOIN source_collect_jobs j ON j.id=r.job_id""",
            (row["actor_id"],row["id"]),
        ).fetchone()
        if (totals["all_bytes"] + size > MAX_REVIEW_GLOBAL_BYTES
            or totals["actor_bytes"] + size > MAX_REVIEW_ACTOR_BYTES
            or totals["job_bytes"] + size > MAX_REVIEW_JOB_BYTES):
            return None
        snapshot_id = str(uuid.uuid4())
        con.execute(
            "INSERT INTO source_collect_review_raw("
            "snapshot_id,job_id,item_index,ordinal,source_id,url,"
            "raw_text,raw_sha256,raw_bytes,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id,row["id"],item_index,chapter["ordinal"],
             chapter["source_id"],chapter["url"],raw,digest,size,now),
        )
        return snapshot_id

    @staticmethod
    def _owned(con: sqlite3.Connection, actor: str, job_id: str) -> sqlite3.Row:
        from .source_job_store import SourceJobMissing
        owner = con.execute(
            "SELECT * FROM source_collect_jobs WHERE id=? AND actor_id=?",
            (job_id,actor),
        ).fetchone()
        if owner is None:
            raise SourceJobMissing("Collection job not found")
        return owner

    @staticmethod
    def _verified(con: sqlite3.Connection, owner: sqlite3.Row,
                  snapshot_id: str) -> sqlite3.Row:
        from .source_job_store import SourceJobMissing, SourceJobConflict, SourceJobStore
        snapshot = con.execute(
            "SELECT * FROM source_collect_review_raw "
            "WHERE snapshot_id=? AND job_id=?",
            (snapshot_id,owner["id"]),
        ).fetchone()
        if snapshot is None:
            raise SourceJobMissing("Source review snapshot not found")
        manifest = SourceJobStore._manifest(owner)
        index = snapshot["item_index"]
        if not 0 <= index < len(manifest):
            raise SourceJobConflict("Review index is outside the source manifest")
        ch = manifest[index]
        raw = snapshot["raw_text"]
        if (snapshot["ordinal"] != ch["ordinal"]
            or snapshot["source_id"] != ch["source_id"]
            or snapshot["url"] != ch["url"]
            or not isinstance(raw,str)
            or snapshot["raw_sha256"] != _digest(raw)
            or snapshot["raw_bytes"] != len(raw.encode("utf-8"))):
            raise SourceJobConflict("Stored original review integrity mismatch")
        return snapshot

    def list(self, actor: str, job_id: str, *, limit: int = 30) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Review page limit must be 1..50")
        with self.store._connect() as con:
            owner = self._owned(con,actor,job_id)
            rows = con.execute(
                "SELECT snapshot_id FROM source_collect_review_raw "
                "WHERE job_id=? ORDER BY created_at DESC,snapshot_id DESC LIMIT ?",
                (owner["id"],limit),
            ).fetchall()
            return [
                self._summary(con,self._verified(con,owner,item["snapshot_id"]))
                for item in rows
            ]

    @staticmethod
    def _summary(con: sqlite3.Connection, item: sqlite3.Row) -> dict:
        candidate_count = con.execute(
            "SELECT COUNT(*) FROM source_collect_review_derived WHERE snapshot_id=?",
            (item["snapshot_id"],),
        ).fetchone()[0]
        return {
            "snapshot_id":item["snapshot_id"], "ordinal":item["ordinal"],
            "source_id":item["source_id"], "url":item["url"],
            "raw_sha256":item["raw_sha256"],"raw_bytes":item["raw_bytes"],
            "candidate_count":candidate_count, "status":"needs_review",
            "created_at":item["created_at"],
        }

    def detail(self, actor: str, job_id: str, snapshot_id: str) -> dict:
        from .source_job_store import SourceJobConflict
        with self.store._connect() as con:
            owner = self._owned(con,actor,job_id)
            row = self._verified(con,owner,snapshot_id)
            results = []
            for candidate in con.execute(
                "SELECT * FROM source_collect_review_derived "
                "WHERE snapshot_id=? ORDER BY created_at DESC",
                (snapshot_id,),
            ).fetchall():
                candidate_text = candidate["normalized_text"]
                if (candidate["raw_sha256"] != row["raw_sha256"]
                    or candidate["normalized_sha256"] != _digest(candidate_text)
                    or candidate["normalized_bytes"] != len(candidate_text.encode("utf-8"))
                    or not SHA.fullmatch(candidate["mapping_fingerprint"])):
                    raise SourceJobConflict("Derived source candidate integrity mismatch")
                results.append({
                    "mapping_fingerprint":candidate["mapping_fingerprint"],
                    "normalized_text":candidate_text,
                    "normalized_sha256":candidate["normalized_sha256"],
                    "status":candidate["status"],
                    "replacements":candidate["replacements"],
                    "unresolved_glyphs":candidate["unresolved_glyphs"],
                    "mapping_used":json.loads(candidate["mapping_used_json"]),
                    "evidence_sha256":candidate["evidence_sha256"],
                    "candidate_only":True,
                })
            return {
                **self._summary(con,row),
                "raw_text":row["raw_text"],"candidates":results,
                "candidate_only":True, "exportable":False,
            }

    def derive(
        self, actor: str, job_id: str, snapshot_id: str, *,
        expected_raw_sha256: str, glossary_rows: list[dict[str,Any]],
    ) -> dict:
        from .source_job_store import SourceJobConflict
        if not SHA.fullmatch(expected_raw_sha256):
            raise ValueError("Expected exact original SHA-256")
        if not isinstance(glossary_rows,list) or not 2 <= len(glossary_rows) <= 200:
            raise ValueError("Supply 2..200 bounded glossary evidence rows")
        clean_rows = []
        for item in glossary_rows:
            if (not isinstance(item,dict)
                or type(item.get("project_id")) is not int
                or not 0 <= item["project_id"] <= 1_000_000
                or not isinstance(item.get("original"),str)
                or not 1 <= len(item["original"]) <= 120
                or not isinstance(item.get("rus"),str)
                or not 1 <= len(item["rus"]) <= 160):
                raise ValueError("Invalid glossary evidence row")
            clean_rows.append({
                "project_id":item["project_id"],
                "original":item["original"],"rus":item["rus"],
            })
        evidence_json = json.dumps(
            sorted(clean_rows,key=lambda r:(r["project_id"],r["original"],r["rus"])),
            ensure_ascii=False,separators=(",",":"),sort_keys=True,
        )
        if len(evidence_json.encode("utf-8")) > 64000:
            raise ValueError("Glossary evidence exceeds size limit")
        snapshot = self.detail(actor,job_id,snapshot_id)
        if snapshot["raw_sha256"] != expected_raw_sha256:
            raise SourceJobConflict("Original changed; refresh review snapshot")
        learned = learn_timotxt_mapping(clean_rows)
        derived = derive_timotxt_candidate(snapshot["raw_text"],learned)
        if derived["replacements"] == 0:
            raise SourceJobConflict("No evidenced replacement applies to this RAW")
        encoded = derived["normalized_text"].encode("utf-8")
        if not 0 < len(encoded) <= MAX_REVIEW_ITEM_BYTES:
            raise SourceJobConflict("Candidate size outside review limits")
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            owner = self._owned(con,actor,job_id)
            row = self._verified(con,owner,snapshot_id)
            if row["raw_sha256"] != expected_raw_sha256:
                raise SourceJobConflict("Review CAS failed: original changed")
            existing = con.execute(
                "SELECT * FROM source_collect_review_derived "
                "WHERE snapshot_id=? AND mapping_fingerprint=?",
                (snapshot_id,derived["mapping_fingerprint"]),
            ).fetchone()
            if existing is not None:
                if (existing["raw_sha256"] != expected_raw_sha256
                    or existing["normalized_sha256"] != derived["normalized_sha256"]
                    or existing["normalized_text"] != derived["normalized_text"]):
                    raise SourceJobConflict("Existing derived candidate conflicts")
                return self._candidate_receipt(snapshot_id,derived)
            count = con.execute(
                "SELECT COUNT(*) FROM source_collect_review_derived WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()[0]
            used = con.execute(
                "SELECT COALESCE(SUM(normalized_bytes),0) "
                "FROM source_collect_review_derived",
            ).fetchone()[0]
            if (count >= MAX_CANDIDATES_PER_SNAPSHOT
                or used + len(encoded) > MAX_DERIVED_GLOBAL_BYTES):
                raise SourceJobConflict("Derived candidate review quota exceeded")
            con.execute(
                "INSERT INTO source_collect_review_derived("
                "snapshot_id,mapping_fingerprint,raw_sha256,normalized_text,"
                "normalized_sha256,normalized_bytes,status,replacements,"
                "unresolved_glyphs,mapping_used_json,evidence_sha256,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id,derived["mapping_fingerprint"],expected_raw_sha256,
                 derived["normalized_text"],derived["normalized_sha256"],len(encoded),
                 derived["status"],derived["replacements"],derived["unresolved_glyphs"],
                 json.dumps(derived["mapping_used"],ensure_ascii=False,sort_keys=True),
                 hashlib.sha256(evidence_json.encode("utf-8")).hexdigest(),time.time()),
            )
            return self._candidate_receipt(snapshot_id,derived)

    @staticmethod
    def _candidate_receipt(snapshot_id: str, candidate: dict) -> dict:
        return {
            "snapshot_id":snapshot_id,
            "raw_sha256":candidate["raw_sha256"],
            "normalized_sha256":candidate["normalized_sha256"],
            "mapping_fingerprint":candidate["mapping_fingerprint"],
            "replacements":candidate["replacements"],
            "unresolved_glyphs":candidate["unresolved_glyphs"],
            "status":candidate["status"],"candidate_only":True,
            "exportable":False,
        }
