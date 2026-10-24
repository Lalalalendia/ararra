//! M1o: actor-bound disposable import staging, no book mutation.
//!
//! Python ImportSessions is authoritative. All transitions use the same
//! SQLite schema, a BEGIN IMMEDIATE transaction, and canonical JSON packing.
//! Source bodies remain secret to the actor and are never in stage responses.
use super::import_preview::{self, PreviewError};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::time::{SystemTime, UNIX_EPOCH};

const STAGE_TTL: f64 = 1800.0;
const MAX_ACTIVE: i64 = 3;
const MAX_PACKED_BYTES: usize = 24 * 1024 * 1024;

#[derive(Debug)]
pub enum StageError {
    Invalid(&'static str),
    Conflict(&'static str),
    TooLarge(&'static str),
    Missing,
    Expired,
    Sqlite(rusqlite::Error),
    Malformed,
}
impl From<rusqlite::Error> for StageError {
    fn from(err: rusqlite::Error) -> Self { Self::Sqlite(err) }
}
impl From<PreviewError> for StageError {
    fn from(err: PreviewError) -> Self {
        match err {
            PreviewError::Invalid(message) => Self::Invalid(message),
            PreviewError::TooLarge => Self::TooLarge("File exceeds 12 MiB limit"),
        }
    }
}
fn now() -> Result<f64, StageError> {
    SystemTime::now().duration_since(UNIX_EPOCH)
        .map(|v| v.as_secs_f64()).map_err(|_| StageError::Malformed)
}
fn sha(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}
fn packed(value: &Value) -> Result<String, StageError> {
    serde_json::to_string(value).map_err(|_| StageError::Malformed)
}

fn public_projection(
    row: &rusqlite::Row<'_>, include_prefix: bool,
) -> Result<Value, StageError> {
    let preview_raw: String = row.get("preview_json")?;
    let preview: Value = serde_json::from_str(&preview_raw)
        .map_err(|_| StageError::Malformed)?;
    let prefix: String = row.get("prefix_html")?;
    let status: String = row.get("status")?;
    let mut result = json!({
        "stage_id": row.get::<_, String>("id")?,
        "status": status,
        "file_sha256": row.get::<_, String>("original_sha256")?,
        "expires_at": row.get::<_, f64>("expires_at")?,
        "preview": preview,
        "unassigned_prefix_saved": !prefix.is_empty(),
    });
    if include_prefix {
        result["unassigned_prefix_html"] = json!(prefix);
    }
    if status == "committed" {
        let committed_raw: String = row.get("commit_result_json")?;
        result["commit_result"] = serde_json::from_str(&committed_raw)
            .map_err(|_| StageError::Malformed)?;
    }
    Ok(result)
}

fn read_owned(
    db: &Connection, actor: &str, stage_id: &str, timestamp: f64,
    include_prefix: bool,
) -> Result<Value, StageError> {
    let mut query = db.prepare(
        "SELECT * FROM book_import_stages WHERE id=?1 AND actor_id=?2"
    )?;
    let result = query.query_row(params![stage_id, actor], |row| {
        let expires: f64 = row.get("expires_at")?;
        let payload = public_projection(row, include_prefix)
            .map_err(|error| match error {
                StageError::Sqlite(sqlite) => sqlite,
                _ => rusqlite::Error::InvalidQuery,
            })?;
        Ok((expires, payload))
    }).optional()?;
    match result {
        None => Err(StageError::Missing),
        Some((expires, _)) if expires < timestamp => Err(StageError::Expired),
        Some((_, payload)) => Ok(payload),
    }
}

/// Only a valid UTF-8 TXT grammar may enter Rust staging. The reference
/// parser rejects empty story bodies and reserves up to 3 active actor stages.
pub fn stage(
    db: &mut Connection, actor: &str, filename: &str, data: &[u8],
    idempotency_key: Option<&str>,
) -> Result<Value, StageError> {
    let key = idempotency_key.map(str::trim).filter(|s| !s.is_empty());
    if key.is_some_and(|s| s.chars().count() > 128) {
        return Err(StageError::Invalid("Idempotency-Key exceeds 128 characters"));
    }
    let (preview, payload, prefix_html) =
        import_preview::stage_payload_txt(filename, data)?;
    if payload["chapters"].as_array()
        .is_none_or(|chapters| chapters.iter().any(|ch| {
            let raw = ch["source_html"].as_str().unwrap_or("");
            // Canonical escaped source HTML has non-empty visible plain
            // body. A non-empty H1 alone is not a readable story chapter.
            let body = raw.split_once("</h1>\n").map_or(raw, |(_, next)| next);
            !body.contains("<p>")
        }))
    {
        return Err(StageError::Invalid("Empty chapters must be corrected before staging"));
    }
    let payload_json = packed(&payload)?;
    if payload_json.len() + prefix_html.len() > MAX_PACKED_BYTES {
        return Err(StageError::TooLarge("Expanded source exceeds the 24 MiB staging limit"));
    }

    let upload_sha = sha(data);
    let fingerprint = sha(packed(&json!([filename, upload_sha.clone()]))?.as_bytes());
    let stamp = now()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute("DELETE FROM book_import_stages WHERE expires_at < ?1",
               [stamp])?;

    if let Some(key) = key {
        let previous = tx.query_row(
            "SELECT * FROM book_import_stages \
             WHERE actor_id=?1 AND idempotency_key=?2",
            params![actor, key],
            |row| {
                let prior: String = row.get("input_fingerprint")?;
                let projection = public_projection(row, false)
                    .map_err(|error| match error {
                        StageError::Sqlite(e) => e,
                        _ => rusqlite::Error::InvalidQuery,
                    })?;
                Ok((prior, projection))
            },
        ).optional()?;
        if let Some((prior, projection)) = previous {
            if prior != fingerprint {
                return Err(StageError::Conflict(
                    "Idempotency-Key refers to a different uploaded file"
                ));
            }
            // Same actor + key + content replays the original stage ID
            // and TTL. Drop Tx without commit: no new changes on replay.
            return Ok(projection);
        }
    }
    let active: i64 = tx.query_row(
        "SELECT COUNT(*) FROM book_import_stages WHERE actor_id=?1 AND status='staged'",
        [actor], |r| r.get(0),
    )?;
    if active >= MAX_ACTIVE {
        return Err(StageError::TooLarge(
            "Too many unconfirmed import stages for this member"
        ));
    }

    let stage_id = uuid::Uuid::new_v4().to_string();
    let expires = stamp + STAGE_TTL;
    tx.execute(
        "INSERT INTO book_import_stages \
         (id,actor_id,idempotency_key,input_fingerprint,original_sha256,\
          filename,status,preview_json,payload_json,prefix_html,created_at,expires_at)\
         VALUES(?1,?2,?3,?4,?5,?6,'staged',?7,?8,?9,?10,?11)",
        params![stage_id, actor, key, fingerprint, upload_sha, filename,
                packed(&preview)?, payload_json, prefix_html, stamp, expires],
    )?;
    let projection = tx.query_row(
        "SELECT * FROM book_import_stages WHERE id=?1",
        [&stage_id],
        |row| public_projection(row, false).map_err(|error| match error {
            StageError::Sqlite(e) => e,
            _ => rusqlite::Error::InvalidQuery,
        }),
    )?;
    tx.commit()?;
    Ok(projection)
}

/// Other actors see the same 404 as an unknown UUID. A committed receipt is
/// readable without exposing private parsed payload_json or raw source.
pub fn get(
    db: &Connection, actor: &str, stage_id: &str,
) -> Result<Value, StageError> {
    read_owned(db, actor, stage_id, now()?, true)
}

/// Delete is only allowed for uncommitted stages owned by the caller.
/// No write to books, translations, accepted history or jobs.
pub fn cancel(
    db: &mut Connection, actor: &str, stage_id: &str,
) -> Result<Value, StageError> {
    let stamp = now()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let row = read_owned(&tx, actor, stage_id, stamp, false)?;
    if row["status"] != "staged" {
        return Err(StageError::Conflict("Committed import cannot be cancelled"));
    }
    tx.execute(
        "DELETE FROM book_import_stages \
         WHERE id=?1 AND actor_id=?2 AND status='staged'",
        params![stage_id, actor],
    )?;
    tx.commit()?;
    Ok(json!({"stage_id": stage_id, "status": "cancelled"}))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn db() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch(
            "CREATE TABLE book_import_stages(\
             id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,\
             idempotency_key TEXT,input_fingerprint TEXT NOT NULL,\
             original_sha256 TEXT NOT NULL,filename TEXT NOT NULL,\
             status TEXT NOT NULL,preview_json TEXT NOT NULL,\
             payload_json TEXT,prefix_html TEXT NOT NULL,\
             confirm_fingerprint TEXT,commit_result_json TEXT,\
             created_at REAL NOT NULL,expires_at REAL NOT NULL,\
             UNIQUE(actor_id,idempotency_key));",
        ).unwrap();
        c
    }

    #[test]
    fn stage_is_actor_private_and_idempotent_and_cancellable() {
        let mut c = db();
        let data = "Перед началом.\n\nГлава 1\nA & B\n\nГлава 2\nВторая.";
        let first = stage(&mut c, "alice", "story.txt", data.as_bytes(), Some("request-1"))
            .unwrap();
        assert_eq!(first["preview"]["chapter_count"], 2);
        assert_eq!(first["unassigned_prefix_saved"], true);
        assert_eq!(first["status"], "staged");
        let again = stage(&mut c, "alice", "story.txt", data.as_bytes(), Some("request-1"))
            .unwrap();
        assert_eq!(first, again);
        assert!(matches!(stage(&mut c, "alice", "story.txt",
            b"Chapter 1\nOther", Some("request-1")),
            Err(StageError::Conflict(_))));
        assert!(matches!(get(&c, "bob", first["stage_id"].as_str().unwrap()),
            Err(StageError::Missing)));
        let owned = get(&c, "alice", first["stage_id"].as_str().unwrap()).unwrap();
        assert_eq!(owned["unassigned_prefix_html"], "<p>Перед началом.</p>");
        assert_eq!(cancel(&mut c, "alice", first["stage_id"].as_str().unwrap())
            .unwrap()["status"], "cancelled");
        assert!(matches!(get(&c, "alice", first["stage_id"].as_str().unwrap()),
            Err(StageError::Missing)));
    }
}
