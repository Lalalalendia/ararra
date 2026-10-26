//! M2b: read-only projection of Python-owned translation batches.
//! Existing SQLite is the source of truth: never initialize schema or enqueue.
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

const STATES: [&str; 6] =
    ["queued", "running", "retry_wait", "completed", "failed", "cancelled"];

fn increment(counts: &mut Map<String, Value>, key: &str) {
    let previous = counts.get(key).and_then(Value::as_i64).unwrap_or(0);
    counts.insert(key.to_owned(), json!(previous + 1));
}

fn details(
    db: &Connection, book_id: &str, batch_id: &str,
) -> Result<Option<Value>, rusqlite::Error> {
    let batch: Option<(String, String, f64)> = db.query_row(
        "SELECT actor_id,manifest_sha256,created_at FROM translation_batches
         WHERE id=?1 AND book_id=?2",
        params![batch_id, book_id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    ).optional()?;
    let Some((actor, manifest, created_at)) = batch else {
        return Ok(None);
    };
    let requested = db.query_row(
        "SELECT 1 FROM translation_batch_drive_requests WHERE batch_id=?1",
        [batch_id], |r| r.get::<_, i64>(0),
    ).optional()?.is_some();
    let mut stmt = db.prepare(
        "SELECT i.chapter_id,i.chapter_index,i.source_revision,
                i.source_sha256,i.glossary_revision,i.job_id,
                j.status,j.attempts,o.status,o.receipt_json
         FROM translation_batch_items AS i
         JOIN jobs AS j ON j.id=i.job_id
         LEFT JOIN drive_export_outbox AS o ON o.job_id=i.job_id
         WHERE i.batch_id=?1 ORDER BY i.chapter_index,i.chapter_id",
    )?;
    let rows = stmt.query_map([batch_id], |r| {
        Ok((
            r.get::<_, String>(0)?, r.get::<_, i64>(1)?,
            r.get::<_, i64>(2)?, r.get::<_, String>(3)?,
            r.get::<_, i64>(4)?, r.get::<_, String>(5)?,
            r.get::<_, String>(6)?, r.get::<_, i64>(7)?,
            r.get::<_, Option<String>>(8)?,
            r.get::<_, Option<String>>(9)?,
        ))
    })?;
    let mut chapters = Vec::new();
    let mut counts = Map::new();
    let mut drive_counts = Map::new();
    for state in STATES { counts.insert(state.to_owned(), json!(0)); }
    for record in rows {
        let (chapter_id, chapter_index, source_revision, source_sha256,
             glossary_revision, job_id, status, attempts,
             outbox_status, receipt_json) = record?;
        let mut chapter = json!({
            "chapter_id": chapter_id,
            "chapter_index": chapter_index,
            "source_revision": source_revision,
            "source_sha256": source_sha256,
            "glossary_revision": glossary_revision,
            "job_id": job_id,
            "status": status,
            "attempts": attempts,
        });
        if counts.contains_key(&status) {
            increment(&mut counts, &status);
        }
        let mut delivery = if !requested {
            "not_requested"
        } else if let Some(export) = outbox_status.as_deref() {
            match export {
                "pending" => "awaiting_exporter",
                "running" => "uploading",
                "done" => "exported",
                _ => "requires_review",
            }
        } else if status == "completed" {
            "requires_review"
        } else if status == "failed" || status == "cancelled" {
            "not_delivered"
        } else {
            "awaiting_translation"
        };
        if delivery == "exported" {
            let id = receipt_json.as_deref()
                .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
                .and_then(|record| record.get("translation_file_id")
                    .and_then(Value::as_str).filter(|v| !v.is_empty())
                    .map(str::to_owned));
            if let Some(id) = id {
                chapter["drive_translation_file_id"] = json!(id);
            } else {
                delivery = "requires_review";
            }
        }
        chapter["drive_status"] = json!(delivery);
        increment(&mut drive_counts, delivery);
        chapters.push(chapter);
    }
    let size = chapters.len() as i64;
    let completed = counts["completed"].as_i64().unwrap_or(0);
    let failed = counts["failed"].as_i64().unwrap_or(0);
    let cancelled = counts["cancelled"].as_i64().unwrap_or(0);
    let state = if size > 0 && completed == size {
        "completed"
    } else if size > 0 && completed + failed + cancelled == size {
        "finished_with_errors"
    } else {
        "active"
    };
    Ok(Some(json!({
        "batch_id": batch_id,
        "book_id": book_id,
        "actor_id": actor,
        "manifest_sha256": manifest,
        "created_at": created_at,
        "status": state,
        "chapter_count": size,
        "counts": counts,
        "chapters": chapters,
        "drive_export_requested": requested,
        "drive_counts": drive_counts,
    })))
}

/// Python _details returns a missing-batch 404 even when the book is absent.
pub fn get(
    db: &Connection, book_id: &str, batch_id: &str,
) -> Result<Option<Value>, rusqlite::Error> {
    details(db, book_id, batch_id)
}

/// Python list checks the book first and bounds list limit to 1..50.
pub fn list(
    db: &Connection, book_id: &str, limit: i64,
) -> Result<Option<Value>, rusqlite::Error> {
    let exists = db.query_row(
        "SELECT 1 FROM books WHERE id=?1",
        [book_id], |r| r.get::<_, i64>(0),
    ).optional()?.is_some();
    if !exists { return Ok(None); }
    let mut stmt = db.prepare(
        "SELECT id FROM translation_batches WHERE book_id=?1
         ORDER BY created_at DESC,rowid DESC LIMIT ?2",
    )?;
    let mut ids = Vec::new();
    let rows = stmt.query_map(params![book_id, limit.clamp(1, 50)],
        |r| r.get::<_, String>(0))?;
    for id in rows { ids.push(id?); }
    let mut batches = Vec::with_capacity(ids.len());
    for id in ids {
        batches.push(details(db, book_id, &id)?.ok_or(rusqlite::Error::InvalidQuery)?);
    }
    Ok(Some(json!({"book_id": book_id, "batches": batches})))
}
