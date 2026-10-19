"""Durable single-node development queue with atomic claims and fencing tokens.

SQLite is deliberate for local development. Run exactly one worker with SQLite;
PostgreSQL and distributed workers are a separate deployment milestone.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class IdempotencyConflict(ValueError):
    """The same client key was reused with different job input."""


@dataclass(frozen=True)
class JobSpec:
    chapter_id: str
    source_html: str
    source_language: str = "auto"
    target_language: str = "Russian"
    glossary: dict[str, str | dict[str, Any]] | None = None
    context: str = ""
    chapter_path: str = ""
    max_attempts: int = 3
    glossary_versions: dict[str, list[dict[str, Any]]] | None = None
    glossary_authority: str = "READY"
    chapter_context_snapshot: dict[str, Any] | None = None
    predecessor: dict[str, Any] | None = None
    style_instructions: str = ""
    book_id: str = ""
    book_chapter_revision: int = 0
    book_glossary_revision: int = 0
    transport: str = "html_v0"

    def normalized(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "source_html": self.source_html,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "glossary": dict(self.glossary or {}),
            "context": self.context,
            "chapter_path": self.chapter_path,
            "max_attempts": self.max_attempts,
            "glossary_versions": dict(self.glossary_versions or {}),
            "glossary_authority": self.glossary_authority,
            "chapter_context_snapshot": dict(self.chapter_context_snapshot) if self.chapter_context_snapshot else None,
            "predecessor": dict(self.predecessor) if self.predecessor else None,
            "style_instructions": self.style_instructions,
            "book_id": self.book_id,
            "book_chapter_revision": self.book_chapter_revision,
            "book_glossary_revision": self.book_glossary_revision,
            "transport": self.transport,
        }


class QueueStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _initialize(self) -> None:
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    idempotency_key TEXT,
                    status TEXT NOT NULL CHECK(status IN
                        ('queued', 'running', 'retry_wait', 'completed', 'failed', 'cancelled')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    quota_partial_waits INTEGER NOT NULL DEFAULT 0,
                    transient_streak INTEGER NOT NULL DEFAULT 0,
                    run_after REAL NOT NULL,
                    lease_until REAL,
                    lease_owner TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    result_html TEXT,
                    source_hash TEXT,
                    unit_count INTEGER,
                    provenance_json TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(actor_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS ix_jobs_claim
                    ON jobs(status, run_after, created_at);
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_job_events ON job_events(job_id, id);
                -- Private, validated subrequest responses. Entries are job-scoped;
                -- per-prompt hash checks prevent mixing source/glossary/context.
                CREATE TABLE IF NOT EXISTS atom_job_checkpoints (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    request_fingerprint TEXT NOT NULL,
                    response_text TEXT NOT NULL,
                    response_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(job_id, request_fingerprint)
                );
                -- A desktop-style recovery checkpoint tracks only guarded
                -- phase and debt counts. Source/target text stays in the already
                -- verified atom_job_checkpoints, never in a second debt store.
                CREATE TABLE IF NOT EXISTS atom_recovery_state (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    model_id TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK(phase IN ('debt_repair', 'finalize_only')),
                    remaining_debts INTEGER NOT NULL DEFAULT 0 CHECK(remaining_debts >= 0),
                    debt_passes INTEGER NOT NULL DEFAULT 0 CHECK(debt_passes >= 0),
                    finalize_failures INTEGER NOT NULL DEFAULT 0 CHECK(finalize_failures >= 0),
                    updated_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS trg_recovery_cleanup_terminal
                AFTER UPDATE OF status ON jobs
                WHEN NEW.status IN ('completed','failed','cancelled')
                BEGIN
                    DELETE FROM atom_recovery_state WHERE job_id=NEW.id;
                END;
                -- Regardless of how a job ends (book commit, expired lease,
                -- cancellation, daily failure), remove private subrequest text
                -- in the SAME SQLite transaction as the terminal transition.
                CREATE TRIGGER IF NOT EXISTS trg_checkpoint_cleanup_terminal
                AFTER UPDATE OF status ON jobs
                WHEN NEW.status IN ('completed','failed','cancelled')
                BEGIN
                    DELETE FROM atom_job_checkpoints WHERE job_id=NEW.id;
                END;
            """)
            # Existing v0.2/v0.3 databases have no provenance column; upgrade in place.
            columns = {row['name'] for row in con.execute('PRAGMA table_info(jobs)')}
            if 'provenance_json' not in columns:
                con.execute('ALTER TABLE jobs ADD COLUMN provenance_json TEXT')
            if 'quota_partial_waits' not in columns:
                con.execute('ALTER TABLE jobs ADD COLUMN quota_partial_waits INTEGER NOT NULL DEFAULT 0')
            if 'transient_streak' not in columns:
                con.execute('ALTER TABLE jobs ADD COLUMN transient_streak INTEGER NOT NULL DEFAULT 0')
            from .drive_outbox import install_schema as install_drive_outbox_schema
            install_drive_outbox_schema(con)

    @staticmethod
    def _public(row: sqlite3.Row | dict[str, Any], *, include_result: bool = False) -> dict[str, Any]:
        result = {field: row[field] for field in (
            "id", "actor_id", "chapter_id", "status", "attempts", "max_attempts", "created_at", "updated_at",
            "last_error", "unit_count", "source_hash",
        )}
        if include_result:
            result["translated_html"] = row["result_html"]
            result["atom_provenance"] = json.loads(row["provenance_json"]) if row["provenance_json"] else []
        return result

    @staticmethod
    def _enqueue_in_transaction(con: sqlite3.Connection, actor_id: str, spec: JobSpec,
                                now: float, *, idempotency_key: str | None = None) -> sqlite3.Row:
        """Insert an ordinary queue job into an existing caller-owned transaction.

        Used by atomic book batches and single-chapter create_job alike. The
        caller owns COMMIT/ROLLBACK; no extra connection or worker is started.
        """
        if not actor_id.strip() or not spec.chapter_id.strip() or not spec.source_html.strip():
            raise ValueError("actor_id, chapter_id and nonempty source_html required")
        if spec.transport not in {"html_v0", "atoms_v1"}:
            raise ValueError("Unsupported transport")
        if spec.max_attempts < 1 or spec.max_attempts > 10:
            raise ValueError("max_attempts must be between 1 and 10")
        request_json = json.dumps(spec.normalized(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        jid = str(uuid.uuid4())
        con.execute("""
            INSERT INTO jobs (id, actor_id, chapter_id, input_json, request_hash, idempotency_key,
                              status, max_attempts, run_after, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
        """, (jid, actor_id, spec.chapter_id, request_json, digest,
              idempotency_key, spec.max_attempts, now, now, now))
        QueueStore._event(con, jid, "queued", actor_id, now)
        return con.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()

    def create_job(self, actor_id: str, spec: JobSpec, idempotency_key: str | None = None) -> dict[str, Any]:
        if not actor_id.strip() or not spec.chapter_id.strip() or not spec.source_html.strip():
            raise ValueError("actor_id, chapter_id and nonempty source_html required")
        if spec.transport not in {"html_v0", "atoms_v1"}:
            raise ValueError("Unsupported transport")
        if spec.max_attempts < 1 or spec.max_attempts > 10:
            raise ValueError("max_attempts must be between 1 and 10")
        request_json = json.dumps(spec.normalized(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        key = (idempotency_key or "").strip() or None
        if key is not None and len(key) > 128:
            raise ValueError("idempotency_key too long")
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if key is not None:
                existing = con.execute(
                    "SELECT * FROM jobs WHERE actor_id = ? AND idempotency_key = ?", (actor_id, key)
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != digest:
                        # Backward-compatible replay of a v0.3 job without an explicit
                        # transport field. Treat absent protocol as legacy HTML.
                        previous = json.loads(existing['input_json'])
                        legacy_request = dict(spec.normalized())
                        legacy_request.pop('transport', None)
                        if not (spec.transport == 'html_v0' and previous == legacy_request):
                            con.execute("ROLLBACK")
                            raise IdempotencyConflict("Idempotency key refers to different input")
                    con.execute("COMMIT")
                    return self._public(existing)
            row = self._enqueue_in_transaction(con, actor_id, spec, now, idempotency_key=key)
            con.execute("COMMIT")
            return self._public(row)

    @staticmethod
    def _event(con: sqlite3.Connection, job_id: str, stage: str, detail: str, now: float) -> None:
        con.execute("INSERT INTO job_events(job_id, stage, detail, created_at) VALUES(?, ?, ?, ?)",
                    (job_id, stage, detail[:512], now))

    def get_job(self, job_id: str, *, include_result: bool = True) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._public(row, include_result=include_result) if row else None

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
            return [self._public(row) for row in rows]

    def events(self, job_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT stage, detail, created_at FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT ?",
                               (job_id, max(1, min(limit, 200)))).fetchall()
            return [dict(row) for row in reversed(rows)]

    def claim_next(self, *, lease_seconds: float = 300.0,
                   require_personal_key: bool = False) -> tuple[dict[str, Any], str] | None:
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            # Desktop finalize_only is a zero-request operation. A crash after
            # source proof must not exhaust the paid-translation max_attempts;
            # give it a separate finite local-recovery budget (3 failures).
            con.execute("""
                UPDATE atom_recovery_state SET
                    finalize_failures=finalize_failures+1, updated_at=?
                WHERE phase='finalize_only' AND job_id IN (
                    SELECT id FROM jobs WHERE status='running'
                    AND lease_until < ? AND cancel_requested=0
                )
            """, (now, now))
            con.execute("""
                UPDATE jobs SET
                    status = CASE WHEN cancel_requested=1 THEN 'cancelled'
                                  WHEN EXISTS (
                                      SELECT 1 FROM atom_recovery_state AS r
                                      WHERE r.job_id=jobs.id AND r.phase='finalize_only'
                                      AND r.finalize_failures<3
                                  ) THEN 'retry_wait'
                                  WHEN EXISTS (
                                      SELECT 1 FROM atom_recovery_state AS r
                                      WHERE r.job_id=jobs.id AND r.phase='finalize_only'
                                      AND r.finalize_failures>=3
                                  ) THEN 'failed'
                                  WHEN attempts>=max_attempts THEN 'failed'
                                  ELSE 'retry_wait' END,
                    attempts = CASE WHEN cancel_requested=0 AND EXISTS (
                                    SELECT 1 FROM atom_recovery_state AS r
                                    WHERE r.job_id=jobs.id AND r.phase='finalize_only'
                                    AND r.finalize_failures<3
                               ) THEN MAX(0, attempts - 1)
                               ELSE attempts END,
                    last_error = CASE WHEN cancel_requested=1 THEN last_error
                                      ELSE 'Worker lease expired; proof checkpoint retained' END,
                    run_after=?, lease_owner=NULL, lease_until=NULL, updated_at=?
                WHERE status='running' AND lease_until < ?
            """, (now, now, now))
            # Close small moderators' pending batches first: a moderator with
            # one ready chapter should not wait for another moderator's 100.
            # Depth is the COUNT of ready chapters, never HTML length or tokens.
            # Window aggregation is done in the existing atomic claim; no
            # scheduler tables, new worker, or schema migration required.
            eligible_key = """
                AND EXISTS (
                    SELECT 1 FROM member_gemini_keys AS credentials
                    JOIN member_gemini_budgets AS budget
                      ON budget.project_id=credentials.project_id
                     AND budget.member_id=credentials.member_id
                    WHERE credentials.member_id=jobs.actor_id
                      AND budget.daily_request_cap > 0
                )
                AND NOT EXISTS (
                    SELECT 1 FROM team_access_credentials AS disabled
                    WHERE disabled.member_id=jobs.actor_id AND disabled.active=0
                )
            """ if require_personal_key else ""
            row = con.execute("""
                SELECT * FROM (
                    SELECT jobs.*, jobs.rowid AS enqueue_order,
                           COUNT(*) OVER (PARTITION BY actor_id) AS ready_chapters
                    FROM jobs
                    WHERE status IN ('queued', 'retry_wait')
                      AND run_after <= ? AND cancel_requested = 0
            """ + eligible_key + """
                )
                ORDER BY ready_chapters ASC, run_after ASC, created_at ASC, enqueue_order ASC
                LIMIT 1
            """, (now,)).fetchone()
            if row is None:
                con.execute("COMMIT")
                return None
            token = str(uuid.uuid4())
            con.execute("""
                UPDATE jobs SET status = 'running', attempts = attempts + 1, lease_until = ?, lease_owner = ?, updated_at = ?
                WHERE id = ?
            """, (now + max(2.0, lease_seconds), token, now, row["id"]))
            self._event(con, row["id"], "running", "", now)
            updated = con.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            con.execute("COMMIT")
            return {**dict(updated), "input": json.loads(updated["input_json"])}, token

    def defer_for_missing_gemini_key(self, job_id: str, lease_owner: str) -> bool:
        """Requeue after key deletion/rotation race; no paid calls or retry loss."""
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT attempts,cancel_requested FROM jobs "
                "WHERE id=? AND status='running' AND lease_owner=?",
                (job_id, lease_owner),
            ).fetchone()
            if row is None:
                con.execute("COMMIT")
                return False
            cancelled = bool(row["cancel_requested"])
            status = "cancelled" if cancelled else "retry_wait"
            con.execute("""
                UPDATE jobs SET status=?, attempts=?, run_after=?, lease_owner=NULL,
                    lease_until=NULL, updated_at=?, last_error=?
                WHERE id=? AND lease_owner=?
            """, (status, int(row["attempts"]) if cancelled else max(0, int(row["attempts"]) - 1),
                  now if cancelled else now + 30, now,
                  "" if cancelled else "Gemini key unavailable; waiting for moderator",
                  job_id, lease_owner))
            self._event(con, job_id, status, "Credential unavailable; no API call", now)
            con.execute("COMMIT")
        return True

    def heartbeat(self, job_id: str, lease_owner: str, *, lease_seconds: float = 300.0) -> bool:
        now = time.time()
        with self._connect() as con:
            count = con.execute("""
                UPDATE jobs SET lease_until=?, updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
            """, (now + lease_seconds, now, job_id, lease_owner)).rowcount
            return count == 1

    def cancellation_requested(self, job_id: str, lease_owner: str) -> bool:
        with self._connect() as con:
            row = con.execute("SELECT status, lease_owner, cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            return row is None or row["status"] != "running" or row["lease_owner"] != lease_owner or bool(row["cancel_requested"])

    def record_event(self, job_id: str, lease_owner: str, stage: str, detail: str = "") -> None:
        with self._connect() as con:
            row = con.execute("SELECT 1 FROM jobs WHERE id=? AND status='running' AND lease_owner=?", (job_id, lease_owner)).fetchone()
            if row:
                self._event(con, job_id, stage, detail, time.time())

    def _finish(self, job_id: str, lease_owner: str, status: str, *, message: str = "",
                translated_html: str | None = None, source_hash: str | None = None,
                unit_count: int | None = None, run_after: float | None = None,
                atom_provenance: tuple[dict[str, Any], ...] | None = None) -> bool:
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT cancel_requested FROM jobs WHERE id=? AND status='running' AND lease_owner=?",
                              (job_id, lease_owner)).fetchone()
            if row is None:
                con.execute("COMMIT")
                return False
            actual = "cancelled" if row["cancel_requested"] else status
            if actual != "completed":
                translated_html, source_hash, unit_count, atom_provenance = None, None, None, None
            proof = json.dumps(atom_provenance, ensure_ascii=False) if atom_provenance else None
            con.execute("""
                UPDATE jobs SET status=?, result_html=?, source_hash=?, unit_count=?, provenance_json=?, last_error=?,
                                run_after=?, lease_owner=NULL, lease_until=NULL, updated_at=?
                WHERE id=? AND lease_owner=?
            """, (actual, translated_html, source_hash, unit_count, proof,
                  message[:4000] if actual not in {"completed", "cancelled"} else "",
                  now if run_after is None else run_after, now, job_id, lease_owner))
            if actual in {"completed", "failed", "cancelled"}:
                con.execute("DELETE FROM atom_job_checkpoints WHERE job_id=?", (job_id,))
            self._event(con, job_id, actual, message if actual != "completed" else "", now)
            con.execute("COMMIT")
            return True

    def complete(self, job_id: str, lease_owner: str, translated_html: str, source_hash: str, unit_count: int,
                 atom_provenance: tuple[dict[str, Any], ...] = ()) -> bool:
        if not translated_html.strip():
            raise ValueError("Empty translation must not be persisted")
        return self._finish(job_id, lease_owner, "completed", translated_html=translated_html,
                            source_hash=source_hash, unit_count=unit_count,
                            atom_provenance=atom_provenance)

    def fail(self, job_id: str, lease_owner: str, message: str) -> bool:
        return self._finish(job_id, lease_owner, "failed", message=message)

    def retry(self, job_id: str, lease_owner: str, message: str, *, delay_seconds: float) -> bool:
        with self._connect() as con:
            row = con.execute("SELECT attempts, max_attempts FROM jobs WHERE id=? AND lease_owner=? AND status='running'",
                              (job_id, lease_owner)).fetchone()
        if row is None:
            return False
        if row["attempts"] >= row["max_attempts"]:
            return self.fail(job_id, lease_owner, message)
        return self._finish(job_id, lease_owner, "retry_wait", message=message,
                            run_after=time.time() + max(0.0, delay_seconds))

    def defer_atom_local_finalization(self, job_id: str, lease_owner: str, *,
                                      delay_seconds: float = 2.0) -> bool:
        """One bounded local-only retry after a complete proven translation.

        When finalize_only is persisted, rerunning the normal billable retry
        policy would discard all valid proof on max_attempts=1. Instead,
        consume a separate three-failure *local* budget, never a Gemini RPD.
        """
        if not 0 <= delay_seconds <= 60:
            raise ValueError("Invalid local finalization delay")
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("""
                SELECT j.attempts, j.cancel_requested, r.phase, r.finalize_failures
                FROM jobs AS j JOIN atom_recovery_state AS r ON r.job_id=j.id
                WHERE j.id=? AND j.status='running' AND j.lease_owner=?
            """, (job_id, lease_owner)).fetchone()
            if row is None or row["phase"] != "finalize_only":
                con.execute("ROLLBACK")
                return False
            failures = int(row["finalize_failures"]) + 1
            cancelled = bool(row["cancel_requested"])
            state = "cancelled" if cancelled else "failed" if failures >= 3 else "retry_wait"
            con.execute("""
                UPDATE atom_recovery_state SET finalize_failures=?, updated_at=?
                WHERE job_id=?
            """, (failures, now, job_id))
            con.execute("""
                UPDATE jobs SET status=?, attempts=?, run_after=?,
                    lease_owner=NULL, lease_until=NULL, updated_at=?, last_error=?
                WHERE id=? AND status='running' AND lease_owner=?
            """, (
                state,
                max(0, int(row["attempts"]) - 1) if state == "retry_wait" else int(row["attempts"]),
                now + delay_seconds if state == "retry_wait" else now,
                now,
                "Local-only finalization retry" if state == "retry_wait" else
                "Local finalization retry exhausted" if state == "failed" else "",
                job_id, lease_owner,
            ))
            self._event(con, job_id, "finalize_only_wait" if state == "retry_wait" else state,
                        "Local-only finalization retry" if state == "retry_wait" else "", now)
            con.execute("COMMIT")
            return True

    def defer_atom_debt_handoff(self, job_id: str, lease_owner: str, *,
                                delay_seconds: float = 1.0) -> bool:
        """Yield after one bounded wave of source-owned repairs, not a failed job.

        Mirrors desktop's deferred debt-parent handoff: a single immutable job
        retains its SQLite proofs and gets a new lease; no extra full translation
        is authorized. Max 32 waves prevents indefinite hot looping.
        """
        if not 0 <= delay_seconds <= 60:
            raise ValueError("Invalid source-debt handoff delay")
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("""
                SELECT j.attempts, j.cancel_requested, r.phase, r.debt_passes
                FROM jobs AS j JOIN atom_recovery_state AS r ON r.job_id=j.id
                WHERE j.id=? AND j.status='running' AND j.lease_owner=?
            """, (job_id, lease_owner)).fetchone()
            if row is None or row["phase"] != "debt_repair":
                con.execute("ROLLBACK")
                return False
            passes = int(row["debt_passes"]) + 1
            cancelled = bool(row["cancel_requested"])
            state = "cancelled" if cancelled else "failed" if passes > 32 else "retry_wait"
            con.execute("""
                UPDATE atom_recovery_state SET debt_passes=?, updated_at=?
                WHERE job_id=?
            """, (passes, now, job_id))
            con.execute("""
                UPDATE jobs SET status=?, attempts=?, run_after=?,
                    lease_owner=NULL, lease_until=NULL, updated_at=?,
                    last_error=?
                WHERE id=? AND status='running' AND lease_owner=?
            """, (
                state,
                max(0, int(row["attempts"]) - 1) if state == "retry_wait" else int(row["attempts"]),
                now + delay_seconds if state == "retry_wait" else now,
                now,
                "Source-owned repair wave deferred" if state == "retry_wait" else
                "Atom debt handoff exhausted" if state == "failed" else "",
                job_id, lease_owner,
            ))
            self._event(con, job_id, "debt_wait" if state == "retry_wait" else state,
                        "Source-owned repair wave deferred" if state == "retry_wait" else "", now)
            con.execute("COMMIT")
            return True

    def defer_desktop_transient(self, job_id: str, lease_owner: str, *,
                                kind: str, initial_delay_seconds: float) -> bool:
        """Desktop non-countable retry, transactionally fenced to a job lease.

        Keep a bounded persistent streak to avoid spin after a 503/429 storm;
        do not decrement daily Gemini reservations, which were already sent.
        """
        if kind not in {"TEMPORARY_LIMIT", "PROVIDER_UNAVAILABLE", "NETWORK"}:
            raise ValueError("Only desktop non-countable transient kinds permitted")
        if not 0 <= initial_delay_seconds <= 3600:
            raise ValueError("Invalid retry delay")
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("""
                SELECT attempts, transient_streak, cancel_requested FROM jobs
                WHERE id=? AND status='running' AND lease_owner=?
            """, (job_id, lease_owner)).fetchone()
            if row is None:
                con.execute("COMMIT")
                return False
            streak = min(10000, int(row["transient_streak"]) + 1)
            # Desktop transient errors are not part of the finite failure budget,
            # but repeated outages must back off instead of hot-looping.
            backoff = min(300.0, float(2 ** min(streak + 1, 8)))
            delay = max(float(initial_delay_seconds), backoff)
            cancelled = bool(row["cancel_requested"])
            state = "cancelled" if cancelled else "retry_wait"
            safe_note = f"Gemini transient {kind}; retry backoff {int(delay)}s"
            con.execute("""
                UPDATE jobs SET status=?, run_after=?, attempts=?, transient_streak=?,
                    last_error=?, lease_owner=NULL, lease_until=NULL, updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
            """, (
                state, now if cancelled else now + delay,
                int(row["attempts"]) if cancelled else max(0, int(row["attempts"]) - 1),
                streak, "" if cancelled else safe_note, now, job_id, lease_owner,
            ))
            if cancelled:
                con.execute("DELETE FROM atom_job_checkpoints WHERE job_id=?", (job_id,))
            self._event(con, job_id, "cancelled" if cancelled else "transient_wait",
                        "" if cancelled else safe_note, now)
            con.execute("COMMIT")
        return True

    def defer_for_daily_quota(self, job_id: str, lease_owner: str, *,
                              reset_at: float, made_calls: bool) -> bool:
        """Pause quota-blocked work without burning finite transient retries.

        No remote call => not a failed job attempt. After repeated partial
        expensive runs (preflight/atom repair), fail closed rather than
        re-spending the same daily allowance forever without checkpoints.
        """
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("""
                SELECT cancel_requested, attempts, quota_partial_waits
                FROM jobs WHERE id=? AND status='running' AND lease_owner=?
            """, (job_id, lease_owner)).fetchone()
            if row is None:
                con.execute("COMMIT")
                return False
            partials = row["quota_partial_waits"] + (1 if made_calls else 0)
            if row["cancel_requested"]:
                state, note = "cancelled", ""
            elif partials > 2:
                state = "failed"
                note = "Translation needs more than daily provider allowance; split or checkpoint job"
            else:
                state = "retry_wait"
                note = f"Gemini daily quota; resume at UTC timestamp {int(reset_at)}"
            con.execute("""
                UPDATE jobs SET status=?, run_after=?, attempts=?,
                    quota_partial_waits=?, last_error=?, lease_owner=NULL,
                    lease_until=NULL, updated_at=?
                WHERE id=? AND lease_owner=?
            """, (
                state, max(now, reset_at) if state == "retry_wait" else now,
                max(0, row["attempts"] - 1) if state == "retry_wait" else row["attempts"],
                partials, note, now, job_id, lease_owner,
            ))
            self._event(con, job_id, "quota_wait" if state == "retry_wait" else state, note, now)
            con.execute("COMMIT")
            return True

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                con.execute("COMMIT")
                return None
            if row["status"] in {"queued", "retry_wait"}:
                con.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,updated_at=? WHERE id=?", (now, job_id))
                self._event(con, job_id, "cancelled", "", now)
            elif row["status"] == "running":
                con.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE id=?", (now, job_id))
                self._event(con, job_id, "cancellation_requested", "", now)
            updated = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            con.execute("COMMIT")
            return self._public(updated)
