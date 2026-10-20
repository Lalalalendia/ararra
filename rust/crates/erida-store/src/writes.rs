//! RUST-02a: transactional translation-job writes for a disposable test DB.
//! The existing Python SQLite schema and invariants remain authoritative.
use super::{public_job, StoreError};
use rusqlite::{params, Connection, OptionalExtension, Transaction, TransactionBehavior};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

#[derive(Debug)]
pub enum WriteError {
    Storage(StoreError),
    Sqlite(rusqlite::Error),
    InvalidInput(&'static str),
    IdempotencyConflict,
}
impl From<rusqlite::Error> for WriteError {
    fn from(error: rusqlite::Error) -> Self {
        Self::Sqlite(error)
    }
}
impl From<StoreError> for WriteError {
    fn from(error: StoreError) -> Self {
        Self::Storage(error)
    }
}

fn now_seconds() -> Result<f64, WriteError> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .map_err(|_| WriteError::InvalidInput("System clock unavailable"))
}

fn view_public(tx: &Transaction<'_>, id: &str) -> Result<Value, WriteError> {
    let mut stmt = tx.prepare("SELECT * FROM jobs WHERE id=?1")?;
    let mut rows = stmt.query([id])?;
    let row = rows
        .next()?
        .ok_or(WriteError::InvalidInput("Stored job disappeared"))?;
    public_job(row, false).map_err(Into::into)
}

pub fn open_disposable_writer(path: &std::path::Path) -> Result<Connection, WriteError> {
    use rusqlite::OpenFlags;
    let db = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    db.busy_timeout(std::time::Duration::from_secs(10))?;
    db.execute_batch("PRAGMA foreign_keys=ON")?;
    Ok(db)
}

/// Idempotency is actor-scoped and checked in the SAME IMMEDIATE transaction
/// as enqueue/event. The payload is already Pydantic-compatible normalized JSON.
pub fn enqueue(
    db: &mut Connection,
    actor: &str,
    normalized: &Value,
    idempotency_key: Option<&str>,
) -> Result<Value, WriteError> {
    let chapter_id = normalized
        .get("chapter_id")
        .and_then(Value::as_str)
        .ok_or(WriteError::InvalidInput("chapter_id required"))?;
    let source_html = normalized
        .get("source_html")
        .and_then(Value::as_str)
        .ok_or(WriteError::InvalidInput("source_html required"))?;
    if actor.trim().is_empty() || chapter_id.trim().is_empty() || source_html.trim().is_empty() {
        return Err(WriteError::InvalidInput(
            "actor_id, chapter_id and nonempty source_html required",
        ));
    }
    if !matches!(
        normalized.get("transport").and_then(Value::as_str),
        Some("html_v0") | Some("atoms_v1")
    ) {
        return Err(WriteError::InvalidInput("Unsupported transport"));
    }
    let max_attempts = normalized
        .get("max_attempts")
        .and_then(Value::as_i64)
        .ok_or(WriteError::InvalidInput("max_attempts required"))?;
    if !(1..=10).contains(&max_attempts) {
        return Err(WriteError::InvalidInput(
            "max_attempts must be between 1 and 10",
        ));
    }
    let trimmed = idempotency_key.map(str::trim).filter(|value| !value.is_empty());
    if trimmed.is_some_and(|key| key.chars().count() > 128) {
        return Err(WriteError::InvalidInput("idempotency_key too long"));
    }

    // serde_json default Map uses sorted keys, as Python's sort_keys=True.
    let request_json =
        serde_json::to_string(normalized).map_err(|_| WriteError::InvalidInput("Invalid job"))?;
    let digest = hex::encode(Sha256::digest(request_json.as_bytes()));
    let now = now_seconds()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    if let Some(key) = trimmed {
        let existing: Option<(String, String, String)> = tx
            .query_row(
                "SELECT id,request_hash,input_json FROM jobs \
                 WHERE actor_id=?1 AND idempotency_key=?2",
                params![actor, key],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        if let Some((id, old_digest, old_json)) = existing {
            if old_digest != digest {
                // The Python owner permits one historical v0.3 replay without
                // a transport key, but only for the default HTML protocol.
                let mut legacy = normalized.clone();
                if let Some(object) = legacy.as_object_mut() {
                    object.remove("transport");
                }
                let previous: Value = serde_json::from_str(&old_json)
                    .map_err(|_| WriteError::IdempotencyConflict)?;
                if normalized.get("transport").and_then(Value::as_str) != Some("html_v0")
                    || previous != legacy
                {
                    return Err(WriteError::IdempotencyConflict);
                }
            }
            let response = view_public(&tx, &id)?;
            tx.commit()?;
            return Ok(response);
        }
    }
    let id = Uuid::new_v4().to_string();
    tx.execute(
        "INSERT INTO jobs \
         (id,actor_id,chapter_id,input_json,request_hash,idempotency_key,\
          status,max_attempts,run_after,created_at,updated_at) \
         VALUES(?1,?2,?3,?4,?5,?6,'queued',?7,?8,?8,?8)",
        params![
            id,
            actor,
            chapter_id,
            request_json,
            digest,
            trimmed,
            max_attempts,
            now
        ],
    )?;
    tx.execute(
        "INSERT INTO job_events(job_id,stage,detail,created_at) \
         VALUES(?1,'queued',?2,?3)",
        params![id, actor, now],
    )?;
    let response = view_public(&tx, &id)?;
    tx.commit()?;
    Ok(response)
}

/// Preserve Python QueueStore.request_cancel exact status transitions.
/// In particular, terminal cancellation is idempotent: no duplicate event.
pub fn request_cancel(db: &mut Connection, id: &str) -> Result<Option<Value>, WriteError> {
    let now = now_seconds()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let status: Option<String> = tx
        .query_row("SELECT status FROM jobs WHERE id=?1", [id], |row| row.get(0))
        .optional()?;
    let Some(status) = status else {
        tx.commit()?;
        return Ok(None);
    };
    if status == "queued" || status == "retry_wait" {
        tx.execute(
            "UPDATE jobs SET status='cancelled',cancel_requested=1,updated_at=?1 WHERE id=?2",
            params![now, id],
        )?;
        tx.execute(
            "INSERT INTO job_events(job_id,stage,detail,created_at) \
             VALUES(?1,'cancelled','',?2)",
            params![id, now],
        )?;
    } else if status == "running" {
        tx.execute(
            "UPDATE jobs SET cancel_requested=1,updated_at=?1 WHERE id=?2",
            params![now, id],
        )?;
        tx.execute(
            "INSERT INTO job_events(job_id,stage,detail,created_at) \
             VALUES(?1,'cancellation_requested','',?2)",
            params![id, now],
        )?;
    }
    let result = view_public(&tx, id)?;
    tx.commit()?;
    Ok(Some(result))
}
