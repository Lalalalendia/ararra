"""Revision-fenced, human-reviewed Chaptera terminology QA reports.

Reports are diagnostic-only and based on the CURRENT accepted translation.
A manually accepted chapter does not carry a machine-proven atom alignment:
never manufacture that proof from matching DOM slot counts. Every finding and
review is owned by the immutable source/accepted/glossary revision tuple.
"""
from __future__ import annotations

import json
import sqlite3
import time
from uuid import uuid4

from chaptera_translation_core.chapter_context import sha256_text
from chaptera_translation_core.post_translation_context import ServerGateContext
from chaptera_translation_core.prompt_policy import scoped_glossary
from chaptera_translation_core.translation_gates import run_translation_gates

from .library import LibraryConflict, LibraryNotFound
from .storage import QueueStore


POLICY_VERSION = 1
MAX_FINDINGS = 64
MAX_EVIDENCE_BYTES = 16_384


class ChapterQaStore:
    def __init__(self, store: QueueStore):
        self.store = store
        self._initialize()

    def _initialize(self) -> None:
        with self.store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS chapter_qa_reports (
                    id TEXT PRIMARY KEY,
                    book_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    source_revision INTEGER NOT NULL,
                    source_hash TEXT NOT NULL,
                    accepted_revision INTEGER NOT NULL,
                    accepted_hash TEXT NOT NULL,
                    glossary_revision INTEGER NOT NULL,
                    glossary_hash TEXT NOT NULL,
                    policy_version INTEGER NOT NULL,
                    finding_count INTEGER NOT NULL,
                    stored_count INTEGER NOT NULL,
                    truncated INTEGER NOT NULL CHECK (truncated IN (0,1)),
                    alignment_proven INTEGER NOT NULL DEFAULT 0 CHECK (alignment_proven=0),
                    generated_by TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE (book_id,chapter_id,source_revision,accepted_revision,
                            glossary_revision,policy_version),
                    FOREIGN KEY(book_id,chapter_id)
                        REFERENCES book_chapters(book_id,chapter_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS chapter_qa_findings (
                    id TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL REFERENCES chapter_qa_reports(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    would_block INTEGER NOT NULL CHECK (would_block IN (0,1)),
                    message TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    review_status TEXT NOT NULL DEFAULT 'open'
                        CHECK (review_status IN ('open','reviewed','waived')),
                    review_revision INTEGER NOT NULL DEFAULT 1,
                    reviewer_id TEXT,
                    review_note TEXT NOT NULL DEFAULT '',
                    reviewed_at REAL,
                    UNIQUE (report_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS chapter_qa_review_events (
                    id TEXT PRIMARY KEY,
                    finding_id TEXT NOT NULL REFERENCES chapter_qa_findings(id) ON DELETE CASCADE,
                    review_revision INTEGER NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    review_note TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE (finding_id,review_revision)
                );
                CREATE INDEX IF NOT EXISTS idx_chapter_qa_report
                    ON chapter_qa_reports(book_id,chapter_id,accepted_revision,glossary_revision);
            """)

    @staticmethod
    def _chapter(con: sqlite3.Connection, book_id: str, chapter_id: str):
        row = con.execute("""
            SELECT c.*, b.glossary_json, b.glossary_versions_json,
                   b.glossary_authority, b.glossary_revision
            FROM book_chapters AS c
            JOIN books AS b ON b.id=c.book_id
            WHERE c.book_id=? AND c.chapter_id=?
        """, (book_id, chapter_id)).fetchone()
        if row is None:
            raise LibraryNotFound("Chapter not found")
        return row

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict:
        glossary_hash = sha256_text(json.dumps({
            "glossary": json.loads(row["glossary_json"]),
            "versions": json.loads(row["glossary_versions_json"]),
            "authority": row["glossary_authority"],
        }, ensure_ascii=False, sort_keys=True))
        return {
            "source_revision": int(row["source_revision"]),
            "source_hash": str(row["source_hash"]),
            "accepted_revision": int(row["accepted_revision"]),
            "accepted_hash": sha256_text(row["accepted_html"] or ""),
            "glossary_revision": int(row["glossary_revision"]),
            "glossary_hash": glossary_hash,
        }

    @staticmethod
    def _accepted_current(row: sqlite3.Row) -> bool:
        return bool(row["accepted_html"]) and row["accepted_revision"] > 0 and (
            row["accepted_source_revision"] == row["source_revision"]
        )

    @classmethod
    def _require_current(cls, row: sqlite3.Row, *,
                         source_revision: int, accepted_revision: int,
                         glossary_revision: int) -> dict:
        state = cls._snapshot(row)
        if not cls._accepted_current(row):
            raise LibraryConflict("Accepted translation is missing or stale; QA unavailable")
        if (state["source_revision"] != source_revision or
                state["accepted_revision"] != accepted_revision or
                state["glossary_revision"] != glossary_revision):
            raise LibraryConflict("Source, accepted or glossary revision changed; QA refresh required")
        return state

    @staticmethod
    def _report(con: sqlite3.Connection, book_id: str, chapter_id: str, state: dict):
        row = con.execute("""
            SELECT * FROM chapter_qa_reports
            WHERE book_id=? AND chapter_id=? AND source_revision=?
              AND accepted_revision=? AND glossary_revision=? AND policy_version=?
        """, (book_id, chapter_id, state["source_revision"],
              state["accepted_revision"], state["glossary_revision"], POLICY_VERSION)).fetchone()
        if row is not None and any(row[key] != state[key]
                                   for key in ("source_hash", "accepted_hash", "glossary_hash")):
            raise LibraryConflict("QA report fingerprints mismatch; revision integrity drift")
        return row

    @staticmethod
    def _present(con: sqlite3.Connection, report: sqlite3.Row) -> dict:
        rows = con.execute("""
            SELECT * FROM chapter_qa_findings WHERE report_id=? ORDER BY ordinal
        """, (report["id"],)).fetchall()
        return {
            "report_id": report["id"],
            "source_revision": report["source_revision"],
            "source_hash": report["source_hash"],
            "accepted_revision": report["accepted_revision"],
            "accepted_hash": report["accepted_hash"],
            "glossary_revision": report["glossary_revision"],
            "glossary_hash": report["glossary_hash"],
            "policy_version": report["policy_version"],
            "alignment_proven": False,
            "diagnostic_only": True,
            "finding_count": report["finding_count"],
            "stored_count": report["stored_count"],
            "truncated": bool(report["truncated"]),
            "generated_by": report["generated_by"],
            "created_at": report["created_at"],
            "findings": [
                {
                    "finding_id": row["id"],
                    "code": row["code"],
                    "severity": row["severity"],
                    "would_block": bool(row["would_block"]),
                    "message": row["message"],
                    "evidence": json.loads(row["evidence_json"]),
                    "review_status": row["review_status"],
                    "review_revision": row["review_revision"],
                    "reviewer_id": row["reviewer_id"],
                    "review_note": row["review_note"],
                    "reviewed_at": row["reviewed_at"],
                }
                for row in rows
            ],
        }

    def current(self, book_id: str, chapter_id: str) -> dict:
        with self.store._connect() as con:
            chapter = self._chapter(con, book_id, chapter_id)
            state = self._snapshot(chapter)
            current = self._accepted_current(chapter)
            report = self._report(con, book_id, chapter_id, state) if current else None
            return {
                "book_id": book_id, "chapter_id": chapter_id,
                "accepted_current": current, **state,
                "report": self._present(con, report) if report else None,
            }

    def scan(self, book_id: str, chapter_id: str, *,
             expected_source_revision: int, expected_accepted_revision: int,
             expected_glossary_revision: int, actor_id: str) -> dict:
        with self.store._connect() as con:
            chapter = self._chapter(con, book_id, chapter_id)
            state = self._require_current(
                chapter, source_revision=expected_source_revision,
                accepted_revision=expected_accepted_revision,
                glossary_revision=expected_glossary_revision,
            )
            existing = self._report(con, book_id, chapter_id, state)
            if existing is not None:
                return self._present(con, existing)
            source_html, accepted_html = chapter["source_html"], chapter["accepted_html"]
            scoped = scoped_glossary(
                json.loads(chapter["glossary_json"]),
                json.loads(chapter["glossary_versions_json"]),
                chapter["chapter_path"], chapter["glossary_authority"],
            )
        # CPU-only desktop QA. Never send the accepted manuscript to Gemini.
        findings = run_translation_gates(
            source_html, accepted_html, ServerGateContext(scoped),
            [chapter["chapter_path"]], alignment_proven=False,
        )
        if not isinstance(findings, list):
            raise ValueError("Invalid desktop QA result")
        prepared = []
        for finding in findings[:MAX_FINDINGS]:
            if not isinstance(finding, dict):
                raise ValueError("Invalid desktop QA finding")
            evidence = json.dumps(finding.get("evidence") or {}, ensure_ascii=False,
                                  sort_keys=True)
            if len(evidence.encode("utf-8")) > MAX_EVIDENCE_BYTES:
                raise ValueError("Desktop QA evidence exceeds safe record limit")
            prepared.append((
                str(finding.get("code") or "UNKNOWN")[:80],
                str(finding.get("severity") or "warning")[:30],
                int(bool(finding.get("blocking"))),
                str(finding.get("message") or "")[:4000],
                evidence,
            ))

        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            fresh = self._chapter(con, book_id, chapter_id)
            renewed = self._require_current(
                fresh, source_revision=expected_source_revision,
                accepted_revision=expected_accepted_revision,
                glossary_revision=expected_glossary_revision,
            )
            if renewed != state:
                raise LibraryConflict("QA chapter changed during diagnostic scan")
            existing = self._report(con, book_id, chapter_id, renewed)
            if existing is not None:
                return self._present(con, existing)
            report_id, now = uuid4().hex, time.time()
            con.execute("""
                INSERT INTO chapter_qa_reports(
                    id,book_id,chapter_id,source_revision,source_hash,accepted_revision,
                    accepted_hash,glossary_revision,glossary_hash,policy_version,
                    finding_count,stored_count,truncated,alignment_proven,generated_by,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
            """, (report_id, book_id, chapter_id, state["source_revision"],
                  state["source_hash"], state["accepted_revision"], state["accepted_hash"],
                  state["glossary_revision"], state["glossary_hash"], POLICY_VERSION,
                  len(findings), len(prepared), int(len(findings) > MAX_FINDINGS),
                  actor_id, now))
            for idx, (code, severity, would_block, message, evidence) in enumerate(prepared):
                con.execute("""
                    INSERT INTO chapter_qa_findings(
                        id,report_id,ordinal,code,severity,would_block,message,evidence_json
                    ) VALUES (?,?,?,?,?,?,?,?)
                """, (uuid4().hex, report_id, idx, code, severity, would_block,
                      message, evidence))
            report = self._report(con, book_id, chapter_id, state)
            return self._present(con, report)

    def review(self, book_id: str, chapter_id: str, finding_id: str, *,
               status: str, expected_review_revision: int,
               expected_source_revision: int, expected_accepted_revision: int,
               expected_glossary_revision: int, actor_id: str,
               note: str = "") -> dict:
        if status not in {"open", "reviewed", "waived"}:
            raise ValueError("Invalid QA review status")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("QA review note exceeds one thousand characters")
        if status == "waived" and not note.strip():
            raise ValueError("Waiving QA requires a written reason")
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            chapter = self._chapter(con, book_id, chapter_id)
            state = self._require_current(
                chapter, source_revision=expected_source_revision,
                accepted_revision=expected_accepted_revision,
                glossary_revision=expected_glossary_revision,
            )
            report = self._report(con, book_id, chapter_id, state)
            if report is None:
                raise LibraryNotFound("QA report not found")
            finding = con.execute("""
                SELECT * FROM chapter_qa_findings WHERE id=? AND report_id=?
            """, (finding_id, report["id"])).fetchone()
            if finding is None:
                raise LibraryNotFound("QA finding not found")
            if finding["review_revision"] != expected_review_revision:
                raise LibraryConflict("QA review revision changed; reload required")
            revision = int(finding["review_revision"]) + 1
            now = time.time()
            con.execute("""
                UPDATE chapter_qa_findings SET review_status=?,review_revision=?,
                    reviewer_id=?,review_note=?,reviewed_at=? WHERE id=? AND report_id=?
            """, (status, revision, actor_id, note.strip(), now, finding_id, report["id"]))
            con.execute("""
                INSERT INTO chapter_qa_review_events(
                    id,finding_id,review_revision,from_status,to_status,actor_id,review_note,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
            """, (uuid4().hex, finding_id, revision, finding["review_status"],
                  status, actor_id, note.strip(), now))
            return self._present(con, report)
