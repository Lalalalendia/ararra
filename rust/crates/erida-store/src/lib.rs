//! Read-only SQLite access with existing ReadEra job JSON contracts.
//! The Python owner remains the only schema initializer and job writer.
use rusqlite::{params, Connection, OpenFlags, Row};

mod writes;
pub use writes::{enqueue, open_disposable_writer, request_cancel, WriteError};
use serde_json::{json, Value};
use std::path::Path;
use std::time::Duration;

#[derive(Debug)]
pub enum StoreError {
    Sqlite(rusqlite::Error),
    MalformedProvenance,
}
impl From<rusqlite::Error> for StoreError {
    fn from(error: rusqlite::Error) -> Self {
        Self::Sqlite(error)
    }
}

pub fn open_read_only(path: &Path) -> Result<Connection, StoreError> {
    let db = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    db.busy_timeout(Duration::from_secs(10))?;
    db.execute_batch("PRAGMA query_only=ON")?;
    Ok(db)
}

/// Startup fails closed on uninitialized or incompatible SQLite.
pub fn check_schema(db: &Connection) -> Result<(), StoreError> {
    for table in [
        "jobs",
        "job_events",
        "team_access_credentials",
        "team_browser_sessions",
    ] {
        let sql = format!("SELECT 1 FROM {table} LIMIT 0");
        let _ = db.prepare(&sql)?;
    }
    Ok(())
}

pub(crate) fn public_job(row: &Row<'_>, include_result: bool) -> Result<Value, StoreError> {
    let mut result = json!({
        "id": row.get::<_, String>("id")?,
        "actor_id": row.get::<_, String>("actor_id")?,
        "chapter_id": row.get::<_, String>("chapter_id")?,
        "status": row.get::<_, String>("status")?,
        "attempts": row.get::<_, i64>("attempts")?,
        "max_attempts": row.get::<_, i64>("max_attempts")?,
        "created_at": row.get::<_, f64>("created_at")?,
        "updated_at": row.get::<_, f64>("updated_at")?,
        "last_error": row.get::<_, Option<String>>("last_error")?,
        "unit_count": row.get::<_, Option<i64>>("unit_count")?,
        "source_hash": row.get::<_, Option<String>>("source_hash")?,
    });
    if include_result {
        result["translated_html"] = json!(row.get::<_, Option<String>>("result_html")?);
        let provenance: Option<String> = row.get("provenance_json")?;
        result["atom_provenance"] = match provenance {
            Some(raw) if !raw.is_empty() => serde_json::from_str(&raw)
                .map_err(|_| StoreError::MalformedProvenance)?,
            _ => json!([]),
        };
    }
    Ok(result)
}

pub fn list_jobs(db: &Connection, limit: i64) -> Result<Value, StoreError> {
    let bounded = limit.clamp(1, 100);
    let mut stmt =
        db.prepare("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?1")?;
    let mut cursor = stmt.query([bounded])?;
    let mut jobs = Vec::new();
    while let Some(row) = cursor.next()? {
        jobs.push(public_job(row, false)?);
    }
    Ok(json!({ "jobs": jobs }))
}

pub fn get_job(db: &Connection, job_id: &str) -> Result<Option<Value>, StoreError> {
    let mut stmt = db.prepare("SELECT * FROM jobs WHERE id=?1")?;
    let mut cursor = stmt.query([job_id])?;
    match cursor.next()? {
        Some(row) => Ok(Some(public_job(row, true)?)),
        None => Ok(None),
    }
}

pub fn events(db: &Connection, job_id: &str) -> Result<Value, StoreError> {
    let mut stmt = db.prepare(
        "SELECT stage,detail,created_at FROM job_events \
         WHERE job_id=?1 ORDER BY id DESC LIMIT ?2",
    )?;
    let mut cursor = stmt.query(params![job_id, 100])?;
    let mut events = Vec::new();
    while let Some(row) = cursor.next()? {
        events.push(json!({
            "stage": row.get::<_, String>(0)?,
            "detail": row.get::<_, String>(1)?,
            "created_at": row.get::<_, f64>(2)?,
        }));
    }
    events.reverse(); // Python QueueStore.events returns oldest-to-newest in the window.
    Ok(json!({ "events": events }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn db() -> Connection {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch(
            "CREATE TABLE jobs( \
             id TEXT,actor_id TEXT,chapter_id TEXT,status TEXT,attempts INTEGER,\
             max_attempts INTEGER,created_at REAL,updated_at REAL,\
             last_error TEXT,unit_count INTEGER,source_hash TEXT,\
             result_html TEXT,provenance_json TEXT); \
             CREATE TABLE job_events(id INTEGER PRIMARY KEY,job_id TEXT,\
             stage TEXT,detail TEXT,created_at REAL);",
        )
        .unwrap();
        db
    }

    #[test]
    fn job_metadata_stays_bounded_and_private_result_is_only_on_detail() {
        let db = db();
        db.execute(
            "INSERT INTO jobs VALUES('a','mod','one','completed',1,3,10.5,11.5,\
             NULL,5,'abc','<p>secret</p>','[{\"a\":1}]')",
            [],
        )
        .unwrap();
        let listing = list_jobs(&db, 0).unwrap();
        assert_eq!(listing["jobs"][0]["id"], "a");
        assert!(listing["jobs"][0].get("translated_html").is_none());
        assert!(listing["jobs"][0].get("atom_provenance").is_none());
        let detail = get_job(&db, "a").unwrap().unwrap();
        assert_eq!(detail["translated_html"], "<p>secret</p>");
        assert_eq!(detail["atom_provenance"][0]["a"], 1);
        assert!(get_job(&db, "not-found").unwrap().is_none());
    }

    #[test]
    fn events_are_reversed_to_chronological_order() {
        let db = db();
        db.execute_batch(
            "INSERT INTO job_events VALUES(1,'job','queued','',1.5);\
             INSERT INTO job_events VALUES(2,'job','running','',2.5);",
        )
        .unwrap();
        let results = events(&db, "job").unwrap();
        assert_eq!(results["events"][0]["stage"], "queued");
        assert_eq!(results["events"][1]["stage"], "running");
    }

    #[test]
    fn malformed_provenance_fails_closed() {
        let db = db();
        db.execute(
            "INSERT INTO jobs VALUES('a','mod','one','completed',1,3,10,11,\
             NULL,NULL,NULL,NULL,'not-json')",
            [],
        )
        .unwrap();
        assert!(matches!(
            get_job(&db, "a"),
            Err(StoreError::MalformedProvenance)
        ));
    }
}
