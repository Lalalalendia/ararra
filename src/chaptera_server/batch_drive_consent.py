"""Explicit per-batch Drive handoff consent; no network or policy enablement.

Legacy global outbox semantics remain unchanged. This separate trigger
enqueues *only* newly completed jobs that belong to a user-consented batch.
The actual exporter is a distinct operator-gated process; when disabled,
pending rows remain in SQLite and no remote writes occur.
"""
from __future__ import annotations

import json
import time

from .library import LibraryConflict


class DriveConsentConflict(LibraryConflict):
    """Only the owner of a book can request its automatic Drive handoff."""


def install_schema(con) -> None:
    """Create after translation_batches/items; safe on existing databases."""
    con.executescript("""
        CREATE TABLE IF NOT EXISTS translation_batch_drive_requests (
            batch_id TEXT PRIMARY KEY
                REFERENCES translation_batches(id) ON DELETE CASCADE,
            actor_id TEXT NOT NULL,
            requested_at REAL NOT NULL
        );
        -- Different from the legacy operator-wide export policy.
        -- Consent alone never grants a Drive API mutation.
        CREATE TRIGGER IF NOT EXISTS trg_drive_export_consented_batch_completed
        AFTER UPDATE OF status ON jobs
        WHEN NEW.status='completed' AND OLD.status!='completed'
          AND NULLIF(NEW.result_html,'') IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM translation_batch_items AS i
            JOIN translation_batch_drive_requests AS r ON r.batch_id=i.batch_id
            WHERE i.job_id=NEW.id
          )
          AND json_valid(NEW.input_json)
          AND NULLIF(json_extract(NEW.input_json,'$.book_id'),'') IS NOT NULL
          AND NULLIF(json_extract(NEW.input_json,'$.chapter_id'),'') IS NOT NULL
        BEGIN
            INSERT OR IGNORE INTO drive_export_outbox(
                job_id,status,run_after,created_at,updated_at
            ) VALUES(NEW.id,'pending',NEW.updated_at,NEW.updated_at,NEW.updated_at);
        END;
    """)


def record_request(con, *, book_id: str, batch_id: str, actor_id: str) -> None:
    """Must be called inside the same IMMEDIATE transaction as job enqueue."""
    book = con.execute(
        "SELECT owner_member_id FROM books WHERE id=?", (book_id,)
    ).fetchone()
    if book is None or book["owner_member_id"] != actor_id:
        raise DriveConsentConflict(
            "Only the current book owner can request its Google Drive export"
        )
    con.execute(
        "INSERT INTO translation_batch_drive_requests"
        "(batch_id,actor_id,requested_at) VALUES (?,?,?)",
        (batch_id, actor_id, time.time()),
    )


def describe(con, batch_id: str, chapters: list[dict]) -> dict:
    """Expose only per-chapter states / confirmed IDs, not Drive secrets."""
    requested = con.execute(
        "SELECT 1 FROM translation_batch_drive_requests WHERE batch_id=?",
        (batch_id,),
    ).fetchone() is not None
    files = {
        row["job_id"]: row
        for row in con.execute("""
            SELECT o.job_id,o.status,o.receipt_json
            FROM drive_export_outbox AS o
            JOIN translation_batch_items AS i ON i.job_id=o.job_id
            WHERE i.batch_id=?
        """, (batch_id,)).fetchall()
    }
    labels = {
        "pending": "awaiting_exporter",
        "running": "uploading",
        "done": "exported",
        "hold": "requires_review",
    }
    counts = {}
    for chapter in chapters:
        if not requested:
            state = "not_requested"
        else:
            record = files.get(chapter["job_id"])
            if record:
                state = labels.get(record["status"], "requires_review")
                if state == "exported":
                    try:
                        receipt = json.loads(record["receipt_json"] or "")
                        fid = receipt["translation_file_id"]
                        if not isinstance(fid, str) or not fid:
                            raise ValueError("missing translation file ID")
                        chapter["drive_translation_file_id"] = fid
                    except (ValueError, TypeError, KeyError):
                        state = "requires_review"
            elif chapter["status"] == "completed":
                state = "requires_review"
            elif chapter["status"] in {"failed", "cancelled"}:
                state = "not_delivered"
            else:
                state = "awaiting_translation"
        chapter["drive_status"] = state
        counts[state] = counts.get(state, 0) + 1
    return {"drive_export_requested": requested, "drive_counts": counts}
