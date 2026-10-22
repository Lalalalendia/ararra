//! M1h: administrator moderator-key management on EXISTING Python SQLite.
//!
//! This module is not exposed in production: HTTP route owners enforce the
//! canonical disposable writer gate before opening a writable connection.
//! All actions are atomic with their audit events and ownership/job effects.
use crate::AuthConfig;
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use rusqlite::{params, Connection, OptionalExtension, Transaction, TransactionBehavior};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug)]
pub enum ManageError {
    Invalid(&'static str),
    Conflict(&'static str),
    Missing(&'static str),
    Forbidden,
    Storage(rusqlite::Error),
    Entropy,
}
impl From<rusqlite::Error> for ManageError {
    fn from(e: rusqlite::Error) -> Self { Self::Storage(e) }
}

fn now() -> Result<f64, ManageError> {
    SystemTime::now().duration_since(UNIX_EPOCH)
        .map(|v| v.as_secs_f64()).map_err(|_| ManageError::Entropy)
}
fn token() -> Result<String, ManageError> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| ManageError::Entropy)?;
    Ok(format!("re_{}", URL_SAFE_NO_PAD.encode(bytes)))
}
fn digest(s: &str) -> String {
    hex::encode(Sha256::digest(s.as_bytes()))
}
fn assert_admin(actor: &str, cfg: &AuthConfig) -> Result<(), ManageError> {
    if actor.is_empty() || cfg.admin_member_id.is_empty() || actor != cfg.admin_member_id {
        Err(ManageError::Forbidden)
    } else {
        Ok(())
    }
}
fn valid_member_id(id: &str) -> bool {
    id.len() <= 64 && id.chars().next().is_some_and(|ch| ch.is_ascii_alphabetic())
        && id.chars().all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-'))
}
fn audit(
    tx: &Transaction<'_>, member_id: &str, actor: &str, action: &str, stamp: f64,
) -> Result<(), ManageError> {
    tx.execute(
        "INSERT INTO team_access_events(member_id,actor_id,action,created_at) \
         VALUES(?1,?2,?3,?4)",
        params![member_id, actor, action, stamp],
    )?;
    Ok(())
}
fn ensure_moderator(
    tx: &Transaction<'_>, member: &str, cfg: &AuthConfig,
) -> Result<Option<bool>, ManageError> {
    if member == cfg.admin_member_id {
        return Err(ManageError::Conflict(
            "Administrator credentials are managed only on the host",
        ));
    }
    let active = tx.query_row(
        "SELECT active FROM team_access_credentials WHERE member_id=?1",
        [member], |row| row.get::<_, i64>(0),
    ).optional()?;
    if !cfg.team_tokens.contains_key(member) && active.is_none() {
        return Err(ManageError::Missing("Moderator not found"));
    }
    Ok(active.map(|v| v == 1))
}

/// 256-bit key returned exactly once. Replays and host-configured reserved IDs
/// are conflicts; database keeps the SHA256 digest, never the raw key.
pub fn issue(
    db: &mut Connection, member: &str, actor: &str, cfg: &AuthConfig,
) -> Result<Value, ManageError> {
    assert_admin(actor, cfg)?;
    if !valid_member_id(member) {
        return Err(ManageError::Invalid(
            "Member ID must start with a Latin letter and contain up to 64 safe characters",
        ));
    }
    if member == cfg.admin_member_id || cfg.team_tokens.contains_key(member) {
        return Err(ManageError::Conflict(
            "Member ID is already reserved by host configuration",
        ));
    }
    let secret = token()?;
    let stamp = now()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let exists: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM team_access_credentials WHERE member_id=?1)",
        [member], |row| row.get(0),
    )?;
    if exists {
        return Err(ManageError::Conflict("Member ID already exists"));
    }
    tx.execute(
        "INSERT INTO team_access_credentials \
         (member_id,token_sha256,active,created_at,updated_at,created_by,updated_by) \
         VALUES(?1,?2,1,?3,?3,?4,?4)",
        params![member, digest(&secret), stamp, actor],
    )?;
    audit(&tx, member, actor, "issue", stamp)?;
    tx.commit()?;
    Ok(json!({
        "member_id": member, "role": "moderator",
        "status": "active", "access_token": secret,
    }))
}

/// Accepts a configured static moderator or a previously issued/disabled
/// managed moderator; never rotates the protected administrator key.
pub fn rotate(
    db: &mut Connection, member: &str, actor: &str, cfg: &AuthConfig,
) -> Result<Value, ManageError> {
    assert_admin(actor, cfg)?;
    let secret = token()?;
    let stamp = now()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let current = ensure_moderator(&tx, member, cfg)?;
    if current.is_none() {
        tx.execute(
            "INSERT INTO team_access_credentials \
             (member_id,token_sha256,active,created_at,updated_at,created_by,updated_by) \
             VALUES(?1,?2,1,?3,?3,?4,?4)",
            params![member, digest(&secret), stamp, actor],
        )?;
    } else {
        tx.execute(
            "UPDATE team_access_credentials SET token_sha256=?1,active=1,\
             updated_at=?2,updated_by=?3 WHERE member_id=?4",
            params![digest(&secret), stamp, actor, member],
        )?;
    }
    audit(&tx, member, actor, "rotate", stamp)?;
    tx.commit()?;
    Ok(json!({
        "member_id": member, "role": "moderator",
        "status": "active", "access_token": secret,
    }))
}

/// Refuse orphaning owned books. Cancellation of queued/retrying jobs and a
/// cooperative cancellation request for running jobs is part of ONE atomic
/// transaction with credential disabling and audit events.
pub fn disable(
    db: &mut Connection, member: &str, actor: &str, cfg: &AuthConfig,
) -> Result<Value, ManageError> {
    assert_admin(actor, cfg)?;
    let stamp = now()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let active = ensure_moderator(&tx, member, cfg)?;

    // This table exists in the target M1 schema. Refuse a damaged schema,
    // rather than skip the accountable owner protection.
    let owns: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM books WHERE owner_member_id=?1)",
        [member], |row| row.get(0),
    )?;
    if owns {
        return Err(ManageError::Conflict(
            "Reassign owned books before disabling this moderator",
        ));
    }
    if active == Some(false) {
        tx.commit()?;
        return Ok(json!({
            "member_id": member, "role": "moderator", "status": "disabled",
        }));
    }
    if active.is_none() {
        tx.execute(
            "INSERT INTO team_access_credentials \
             (member_id,token_sha256,active,created_at,updated_at,created_by,updated_by) \
             VALUES(?1,NULL,0,?2,?2,?3,?3)",
            params![member, stamp, actor],
        )?;
    } else {
        tx.execute(
            "UPDATE team_access_credentials SET token_sha256=NULL,active=0,\
             updated_at=?1,updated_by=?2 WHERE member_id=?3",
            params![stamp, actor, member],
        )?;
    }

    let jobs: Vec<(String, String)> = {
        let mut stmt = tx.prepare(
            "SELECT id,status FROM jobs WHERE actor_id=?1 \
             AND status IN ('queued','retry_wait','running')",
        )?;
        let rows = stmt.query_map([member], |r| Ok((r.get(0)?, r.get(1)?)))?;
        rows.collect::<Result<Vec<_>, _>>()?
    };
    for (job_id, status) in &jobs {
        let stage = if status == "running" {
            tx.execute(
                "UPDATE jobs SET cancel_requested=1,updated_at=?1 WHERE id=?2",
                params![stamp, job_id],
            )?;
            "cancel_requested"
        } else {
            tx.execute(
                "UPDATE jobs SET status='cancelled',cancel_requested=1,updated_at=?1 \
                 WHERE id=?2",
                params![stamp, job_id],
            )?;
            "cancelled"
        };
        tx.execute(
            "INSERT INTO job_events(job_id,stage,detail,created_at) \
             VALUES(?1,?2,'member_disabled',?3)",
            params![job_id, stage, stamp],
        )?;
    }
    audit(&tx, member, actor, "disable", stamp)?;
    tx.commit()?;
    Ok(json!({
        "member_id": member, "role": "moderator", "status": "disabled",
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    fn fixture() -> (Connection, AuthConfig) {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch(
            "CREATE TABLE team_access_credentials(\
              member_id TEXT PRIMARY KEY,token_sha256 TEXT UNIQUE,\
              active INTEGER NOT NULL,created_at REAL NOT NULL,\
              updated_at REAL NOT NULL,created_by TEXT NOT NULL,updated_by TEXT NOT NULL);\
             CREATE TABLE team_access_events(\
              id INTEGER PRIMARY KEY AUTOINCREMENT,\
              member_id TEXT NOT NULL,actor_id TEXT NOT NULL,\
              action TEXT NOT NULL,created_at REAL NOT NULL);\
             CREATE TABLE books(id TEXT PRIMARY KEY,owner_member_id TEXT NOT NULL);\
             CREATE TABLE jobs(\
              id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,status TEXT NOT NULL,\
              cancel_requested INTEGER NOT NULL DEFAULT 0,updated_at REAL NOT NULL);\
             CREATE TABLE job_events(\
              job_id TEXT,stage TEXT,detail TEXT,created_at REAL);",
        ).unwrap();
        let mut tokens = BTreeMap::new();
        tokens.insert("chief".to_owned(), "chief-key".to_owned());
        tokens.insert("mod".to_owned(), "mod-key".to_owned());
        let config = AuthConfig::new(tokens, "chief".to_owned()).unwrap();
        (db, config)
    }
    #[test]
    fn issue_rotate_and_disable_keep_raw_secrets_out_of_storage() {
        let (mut db, cfg) = fixture();
        assert!(matches!(issue(&mut db, "1bad", "chief", &cfg), Err(ManageError::Invalid(_))));
        assert!(matches!(issue(&mut db, "mod", "chief", &cfg), Err(ManageError::Conflict(_))));
        assert!(matches!(issue(&mut db, "writer", "mod", &cfg), Err(ManageError::Forbidden)));
        let issued = issue(&mut db, "writer", "chief", &cfg).unwrap();
        let key = issued["access_token"].as_str().unwrap();
        assert!(key.starts_with("re_") && key.len() >= 45);
        let stored: String = db.query_row(
            "SELECT token_sha256 FROM team_access_credentials WHERE member_id='writer'",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(stored, digest(key));
        assert_ne!(stored, key);
        assert!(matches!(issue(&mut db, "writer", "chief", &cfg), Err(ManageError::Conflict(_))));
        let next = rotate(&mut db, "writer", "chief", &cfg).unwrap();
        assert_ne!(next["access_token"], issued["access_token"]);
        assert_eq!(disable(&mut db, "writer", "chief", &cfg).unwrap()["status"], "disabled");
        assert_eq!(disable(&mut db, "writer", "chief", &cfg).unwrap()["status"], "disabled");
        let audit: Vec<String> = {
            let mut stmt = db.prepare(
                "SELECT action FROM team_access_events ORDER BY id",
            ).unwrap();
            stmt.query_map([], |r| r.get(0)).unwrap()
                .collect::<Result<Vec<_>, _>>().unwrap()
        };
        assert_eq!(audit, vec!["issue", "rotate", "disable"]);
    }
    #[test]
    fn disable_requires_owner_handoff_and_cancels_all_pending_jobs() {
        let (mut db, cfg) = fixture();
        db.execute("INSERT INTO books VALUES('b','mod')", []).unwrap();
        assert!(matches!(
            disable(&mut db, "mod", "chief", &cfg),
            Err(ManageError::Conflict("Reassign owned books before disabling this moderator"))
        ));
        db.execute("UPDATE books SET owner_member_id='other' WHERE id='b'", []).unwrap();
        db.execute_batch(
            "INSERT INTO jobs VALUES('queued','mod','queued',0,1);\
             INSERT INTO jobs VALUES('retry','mod','retry_wait',0,1);\
             INSERT INTO jobs VALUES('running','mod','running',0,1);\
             INSERT INTO jobs VALUES('done','mod','completed',0,1);",
        ).unwrap();
        assert_eq!(disable(&mut db, "mod", "chief", &cfg).unwrap()["status"], "disabled");
        let rows: Vec<(String, String, i64)> = {
            let mut stmt = db.prepare(
                "SELECT id,status,cancel_requested FROM jobs ORDER BY id",
            ).unwrap();
            stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
                .unwrap().collect::<Result<Vec<_>, _>>().unwrap()
        };
        assert_eq!(rows, vec![
            ("done".to_owned(), "completed".to_owned(), 0),
            ("queued".to_owned(), "cancelled".to_owned(), 1),
            ("retry".to_owned(), "cancelled".to_owned(), 1),
            ("running".to_owned(), "running".to_owned(), 1),
        ]);
        let count: i64 = db.query_row(
            "SELECT COUNT(*) FROM job_events", [], |r| r.get(0),
        ).unwrap();
        assert_eq!(count, 3);
    }
}
