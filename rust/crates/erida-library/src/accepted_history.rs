//! RUST-M1c: accepted-translation history and revision-fenced restoration.
//! Reads the existing Python journal and never rewrites an old audit record.
use super::{chapters::get_chapter, now, LibraryError};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};

fn chapter_revisions(
    db: &Connection,
    book_id: &str,
    chapter_id: &str,
) -> Result<(i64, i64), LibraryError> {
    db.query_row(
        "SELECT accepted_revision,source_revision FROM book_chapters \
         WHERE book_id=?1 AND chapter_id=?2",
        params![book_id, chapter_id],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )
    .optional()?
    .ok_or(LibraryError::Missing("Chapter not found"))
}

/// The history list intentionally excludes translated_html, as in Python.
/// Most recent first, up to 100 entries.
pub fn accepted_history(
    db: &Connection,
    book_id: &str,
    chapter_id: &str,
) -> Result<Value, LibraryError> {
    chapter_revisions(db, book_id, chapter_id)?;
    let mut stmt = db.prepare(
        "SELECT revision,source_revision,actor_id,created_at \
         FROM book_accepted_revisions \
         WHERE book_id=?1 AND chapter_id=?2 ORDER BY revision DESC LIMIT 100",
    )?;
    let rows = stmt.query_map(params![book_id, chapter_id], |row| {
        Ok(json!({
            "revision": row.get::<_, i64>(0)?,
            "source_revision": row.get::<_, i64>(1)?,
            "actor_id": row.get::<_, String>(2)?,
            "created_at": row.get::<_, f64>(3)?,
        }))
    })?;
    let mut revisions = Vec::new();
    for row in rows {
        revisions.push(row?);
    }
    Ok(json!({"revisions": revisions}))
}

/// Historical accepted translation with exact old HTML, not the current text.
pub fn accepted_version(
    db: &Connection,
    book_id: &str,
    chapter_id: &str,
    revision: i64,
) -> Result<Value, LibraryError> {
    chapter_revisions(db, book_id, chapter_id)?;
    db.query_row(
        "SELECT revision,source_revision,translated_html,actor_id,created_at \
         FROM book_accepted_revisions \
         WHERE book_id=?1 AND chapter_id=?2 AND revision=?3",
        params![book_id, chapter_id, revision],
        |row| {
            Ok(json!({
                "revision": row.get::<_, i64>(0)?,
                "source_revision": row.get::<_, i64>(1)?,
                "translated_html": row.get::<_, String>(2)?,
                "actor_id": row.get::<_, String>(3)?,
                "created_at": row.get::<_, f64>(4)?,
            }))
        },
    )
    .optional()?
    .ok_or(LibraryError::Missing("Accepted revision not found"))
}

/// Restoring an older accepted translation creates a NEW revision, preserving
/// the old history, its original author, and the chapter source untouched.
/// BEGIN IMMEDIATE serializes Python and Rust writers on the same SQLite.
pub fn restore_accepted(
    db: &mut Connection,
    book_id: &str,
    chapter_id: &str,
    revision: i64,
    expected_current_revision: i64,
    expected_source_revision: i64,
    actor: &str,
) -> Result<Value, LibraryError> {
    if revision < 1 || expected_current_revision < 0 || expected_source_revision < 1 {
        return Err(LibraryError::Invalid("Invalid accepted restoration request"));
    }
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (current_revision, source_revision) = chapter_revisions(&tx, book_id, chapter_id)?;
    if current_revision != expected_current_revision || source_revision != expected_source_revision {
        return Err(LibraryError::Conflict("Chapter revisions changed before restore"));
    }

    let historical: Option<(String, i64)> = tx
        .query_row(
            "SELECT translated_html,source_revision FROM book_accepted_revisions \
             WHERE book_id=?1 AND chapter_id=?2 AND revision=?3",
            params![book_id, chapter_id, revision],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    let (translated_html, historical_source) =
        historical.ok_or(LibraryError::Missing("Accepted revision not found"))?;
    if historical_source != expected_source_revision {
        return Err(LibraryError::Conflict(
            "Historical translation belongs to a different source revision",
        ));
    }
    if revision == expected_current_revision {
        return Err(LibraryError::Conflict("Selected revision is already current"));
    }

    let next_revision = expected_current_revision
        .checked_add(1)
        .ok_or(LibraryError::Invalid("Accepted revision limit reached"))?;
    let timestamp = now()?;
    tx.execute(
        "UPDATE book_chapters \
         SET accepted_html=?1,accepted_revision=?2, \
             accepted_source_revision=?3,updated_at=?4 \
         WHERE book_id=?5 AND chapter_id=?6",
        params![translated_html, next_revision, expected_source_revision,
                timestamp, book_id, chapter_id],
    )?;
    tx.execute(
        "INSERT INTO book_accepted_revisions \
         (book_id,chapter_id,revision,source_revision, \
          translated_html,actor_id,created_at) \
         VALUES(?1,?2,?3,?4,?5,?6,?7)",
        params![book_id, chapter_id, next_revision, expected_source_revision,
                translated_html, actor, timestamp],
    )?;
    let response = get_chapter(&tx, book_id, chapter_id)?
        .ok_or(LibraryError::Missing("Chapter not found"))?;
    tx.commit()?;
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch(
            "PRAGMA foreign_keys=ON;\
             CREATE TABLE book_chapters(\
               book_id TEXT NOT NULL,chapter_id TEXT NOT NULL,\
               chapter_index INTEGER NOT NULL,chapter_path TEXT NOT NULL,\
               source_html TEXT NOT NULL,source_hash TEXT NOT NULL,\
               source_revision INTEGER NOT NULL,accepted_html TEXT,\
               accepted_revision INTEGER NOT NULL,accepted_source_revision INTEGER NOT NULL,\
               updated_at REAL NOT NULL,PRIMARY KEY(book_id,chapter_id));\
             CREATE TABLE book_accepted_revisions(\
               book_id TEXT NOT NULL,chapter_id TEXT NOT NULL,revision INTEGER NOT NULL,\
               source_revision INTEGER NOT NULL,translated_html TEXT NOT NULL,\
               actor_id TEXT NOT NULL,created_at REAL NOT NULL,\
               PRIMARY KEY(book_id,chapter_id,revision),\
               FOREIGN KEY(book_id,chapter_id) REFERENCES book_chapters(book_id,chapter_id));\
             INSERT INTO book_chapters VALUES(\
               'b','c',1,'1.xhtml','<p>Source.</p>','hash',1,\
               '<p>Second.</p>',2,1,10);\
             INSERT INTO book_accepted_revisions VALUES\
               ('b','c',1,1,'<p>First.</p>','alice',1),\
               ('b','c',2,1,'<p>Second.</p>','bob',2);",
        )
        .unwrap();
        db
    }

    #[test]
    fn history_is_private_metadata_and_versions_preserve_text() {
        let db = fixture();
        let history = accepted_history(&db, "b", "c").unwrap();
        assert_eq!(history["revisions"].as_array().unwrap().len(), 2);
        assert_eq!(history["revisions"][0]["revision"], 2);
        assert!(history["revisions"][0].get("translated_html").is_none());
        assert_eq!(accepted_version(&db, "b", "c", 1).unwrap()["translated_html"], "<p>First.</p>");
        assert!(matches!(accepted_history(&db, "b", "missing"), Err(LibraryError::Missing("Chapter not found"))));
    }

    #[test]
    fn restoration_creates_new_immutable_revision() {
        let mut db = fixture();
        let before = get_chapter(&db, "b", "c").unwrap().unwrap();
        let restored = restore_accepted(&mut db, "b", "c", 1, 2, 1, "chief").unwrap();
        assert_eq!(restored["accepted_revision"], 3);
        assert_eq!(restored["accepted_html"], "<p>First.</p>");
        assert_eq!(restored["accepted_source_revision"], 1);
        assert_eq!(restored["source_html"], before["source_html"]);
        assert_eq!(restored["source_hash"], before["source_hash"]);
        assert_eq!(accepted_version(&db, "b", "c", 2).unwrap()["translated_html"], "<p>Second.</p>");
        assert_eq!(accepted_version(&db, "b", "c", 3).unwrap()["actor_id"], "chief");
        assert_eq!(accepted_history(&db, "b", "c").unwrap()["revisions"][0]["revision"], 3);
    }

    #[test]
    fn stale_fences_missing_history_and_current_target_never_write() {
        let mut db = fixture();
        assert!(matches!(
            restore_accepted(&mut db, "b", "c", 1, 1, 1, "mod"),
            Err(LibraryError::Conflict("Chapter revisions changed before restore"))
        ));
        assert!(matches!(
            restore_accepted(&mut db, "b", "c", 99, 2, 1, "mod"),
            Err(LibraryError::Missing("Accepted revision not found"))
        ));
        assert!(matches!(
            restore_accepted(&mut db, "b", "c", 2, 2, 1, "mod"),
            Err(LibraryError::Conflict("Selected revision is already current"))
        ));
        db.execute("UPDATE book_chapters SET source_revision=2 WHERE chapter_id='c'", []).unwrap();
        assert!(matches!(
            restore_accepted(&mut db, "b", "c", 1, 2, 2, "mod"),
            Err(LibraryError::Conflict("Historical translation belongs to a different source revision"))
        ));
        assert!(matches!(
            restore_accepted(&mut db, "b", "missing", 1, 0, 1, "mod"),
            Err(LibraryError::Missing("Chapter not found"))
        ));
        let row_count: i64 = db.query_row(
            "SELECT COUNT(*) FROM book_accepted_revisions", [], |row| row.get(0),
        ).unwrap();
        assert_eq!(row_count, 2);
    }
}
