"""Opt-in durable Drive handoff. No Drive secrets in API or Gemini workers.

A SQLite trigger atomically queues only future book-owned completions while
the operator has enabled the policy. One separate short-lived exporter process
claims one item at a time. Errors and expired leases HOLD rather than guessing
whether Google may already have created a file.
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import QueueStore


def install_schema(con) -> None:
    """Idempotent migration; disabled on existing and new installations."""
    con.executescript("""
        CREATE TABLE IF NOT EXISTS drive_export_policy (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1))
        );
        INSERT OR IGNORE INTO drive_export_policy(singleton,enabled) VALUES (1,0);
        CREATE TABLE IF NOT EXISTS drive_export_outbox (
            job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN ('pending','running','done','hold')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            run_after REAL NOT NULL,
            lease_token TEXT,
            lease_until REAL,
            receipt_json TEXT,
            error_code TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_drive_export_claim
            ON drive_export_outbox(status,run_after,created_at);
        -- This is the same SQLite transaction as job completion (including
        -- BookLibrary.complete_book_job). Never scan/backfill historical jobs.
        CREATE TRIGGER IF NOT EXISTS trg_drive_export_book_completed
        AFTER UPDATE OF status ON jobs
        WHEN NEW.status='completed' AND OLD.status!='completed'
             AND (SELECT enabled FROM drive_export_policy WHERE singleton=1)=1
             AND json_valid(NEW.input_json)
             AND NULLIF(json_extract(NEW.input_json,'$.book_id'),'') IS NOT NULL
             AND NULLIF(json_extract(NEW.input_json,'$.chapter_id'),'') IS NOT NULL
        BEGIN
            INSERT OR IGNORE INTO drive_export_outbox(
                job_id,status,run_after,created_at,updated_at
            ) VALUES(NEW.id,'pending',NEW.updated_at,NEW.updated_at,NEW.updated_at);
        END;
    """)


class DriveOutbox:
    """A single-node outbox with explicit enable and no implicit network."""

    def __init__(self, store: QueueStore):
        self.store = store

    def status(self) -> dict:
        with self.store._connect() as con:
            policy = con.execute(
                "SELECT enabled FROM drive_export_policy WHERE singleton=1"
            ).fetchone()
            counts = con.execute(
                "SELECT status,COUNT(*) AS count FROM drive_export_outbox GROUP BY status"
            ).fetchall()
        grouped = {row["status"]:row["count"] for row in counts}
        return {
            "enabled":bool(policy["enabled"]) if policy else False,
            "pending":grouped.get("pending",0),
            "running":grouped.get("running",0),
            "done":grouped.get("done",0),
            "hold":grouped.get("hold",0),
        }

    def set_enabled(self, enabled: bool) -> dict:
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE drive_export_policy SET enabled=? WHERE singleton=1",
                (int(enabled),),
            )
            con.execute("COMMIT")
        return self.status()

    def claim_next(self, *, lease_seconds: float = 7200) -> tuple[str,str] | None:
        """SQLite-fenced claim; expired uncertain work requires review.

        Only one global job may be running. An expired process may have
        already written to Drive, so do not silently restart its upload.
        """
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            enabled = con.execute(
                "SELECT enabled FROM drive_export_policy WHERE singleton=1"
            ).fetchone()
            if not enabled or not enabled["enabled"]:
                con.execute("COMMIT")
                return None
            con.execute("""
                UPDATE drive_export_outbox
                SET status='hold',lease_token=NULL,lease_until=NULL,
                    error_code='expired_lease_requires_review',updated_at=?
                WHERE status='running' AND lease_until <= ?
            """,(now,now))
            if con.execute(
                "SELECT 1 FROM drive_export_outbox WHERE status='running' LIMIT 1"
            ).fetchone():
                con.execute("COMMIT")
                return None
            row = con.execute("""
                SELECT job_id FROM drive_export_outbox
                WHERE status='pending' AND run_after <= ?
                ORDER BY created_at,job_id LIMIT 1
            """,(now,)).fetchone()
            if row is None:
                con.execute("COMMIT")
                return None
            token = str(uuid.uuid4())
            con.execute("""
                UPDATE drive_export_outbox SET status='running', attempts=attempts+1,
                    lease_token=?,lease_until=?,updated_at=?
                WHERE job_id=? AND status='pending'
            """,(token,now+max(600.0,lease_seconds),now,row["job_id"]))
            con.execute("COMMIT")
        return row["job_id"],token

    def finish(self, job_id: str, token: str, receipt: dict) -> bool:
        """The Drive receipt is durable only for the active fencing token."""
        now = time.time()
        with self.store._connect() as con:
            count = con.execute("""
                UPDATE drive_export_outbox
                SET status='done',receipt_json=?,error_code='',
                    lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE job_id=? AND status='running' AND lease_token=?
                  AND lease_until > ?
            """,(json.dumps(receipt,ensure_ascii=False),now,job_id,token,now)).rowcount
        return count==1

    def hold(self, job_id: str, token: str, *, error_code: str) -> bool:
        now = time.time()
        with self.store._connect() as con:
            count = con.execute("""
                UPDATE drive_export_outbox
                SET status='hold',error_code=?,lease_token=NULL,
                    lease_until=NULL,updated_at=?
                WHERE job_id=? AND status='running' AND lease_token=?
            """,(error_code[:64],now,job_id,token)).rowcount
        return count==1

    def retry_held(self, job_id: str) -> bool:
        """Operator-reviewed reconciliation after a possibly partial write."""
        now = time.time()
        with self.store._connect() as con:
            count = con.execute("""
                UPDATE drive_export_outbox
                SET status='pending',run_after=?,error_code='',
                    lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE job_id=? AND status='hold'
            """,(now,now,job_id)).rowcount
        return count==1

    def run_once(self, export: Callable[[str],dict]) -> dict:
        """Use the already accepted exporter; never auto-retry ambiguous writes."""
        claimed = self.claim_next()
        if claimed is None:
            return {"stage":"no_eligible_export",**self.status()}
        job_id,token = claimed
        try:
            receipt = export(job_id)
        except Exception as exc:
            # No exception body or remote metadata in logs, DB or response.
            self.hold(job_id,token,error_code=type(exc).__name__)
            return {"stage":"requires_review","job_id":job_id,
                    "error_code":type(exc).__name__}
        if not self.finish(job_id,token,receipt):
            return {"stage":"fencing_lost","job_id":job_id}
        return {
            "stage":"exported","job_id":job_id,
            "source_file_id":receipt.get("source_file_id"),
            "translation_file_id":receipt.get("translation_file_id"),
        }
