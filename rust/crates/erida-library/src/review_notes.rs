//! M1d: shared editor review notes, matching Python EditorialStore on existing SQLite.
//! Notes are deliberately separate from source, accepted HTML and translation jobs.
use super::{now, LibraryError};
use rusqlite::{params, Connection, OptionalExtension, Row, TransactionBehavior};
use serde_json::{json, Value};
use uuid::Uuid;

fn chapter_versions(
    db: &Connection,
    book_id: &str,
    chapter_id: &str,
) -> Result<(i64, i64), LibraryError> {
    db.query_row(
        "SELECT source_revision,accepted_revision FROM book_chapters \
         WHERE book_id=?1 AND chapter_id=?2",
        params![book_id, chapter_id],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )
    .optional()?
    .ok_or(LibraryError::Missing("Chapter not found"))
}

fn note_projection(row: &Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "id": row.get::<_, String>("id")?,
        "book_id": row.get::<_, String>("book_id")?,
        "chapter_id": row.get::<_, String>("chapter_id")?,
        "source_revision": row.get::<_, i64>("source_revision")?,
        "draft_revision": row.get::<_, i64>("draft_revision")?,
        "accepted_revision": row.get::<_, i64>("accepted_revision")?,
        "body_text": row.get::<_, String>("body_text")?,
        "status": row.get::<_, String>("status")?,
        "note_revision": row.get::<_, i64>("note_revision")?,
        "author_id": row.get::<_, String>("author_id")?,
        "resolver_id": row.get::<_, Option<String>>("resolver_id")?,
        "created_at": row.get::<_, f64>("created_at")?,
        "updated_at": row.get::<_, f64>("updated_at")?,
    }))
}

fn find_note(
    db: &Connection,
    book_id: &str,
    chapter_id: &str,
    note_id: &str,
) -> Result<Option<Value>, LibraryError> {
    db.query_row(
        "SELECT * FROM editor_review_notes WHERE id=?1 AND book_id=?2 AND chapter_id=?3",
        params![note_id, book_id, chapter_id],
        note_projection,
    )
    .optional()
    .map_err(Into::into)
}

/// Python-compatible ORDER BY created_at,id and hard limit 1,000.
/// A missing chapter is 404; an existing chapter without notes is [].
pub fn list_notes(
    db: &Connection,
    book_id: &str,
    chapter_id: &str,
) -> Result<Value, LibraryError> {
    chapter_versions(db, book_id, chapter_id)?;
    let mut statement = db.prepare(
        "SELECT * FROM editor_review_notes WHERE book_id=?1 AND chapter_id=?2 \
         ORDER BY created_at,id LIMIT 1000",
    )?;
    let rows = statement.query_map(params![book_id, chapter_id], note_projection)?;
    let mut notes = Vec::new();
    for row in rows {
        notes.push(row?);
    }
    Ok(json!({"notes": notes}))
}

/// Create a note only against the source/draft/accepted versions the author saw.
/// BEGIN IMMEDIATE prevents Python and Rust both winning a stale version fence.
#[allow(clippy::too_many_arguments)]
pub fn create_note(
    db: &mut Connection,
    book_id: &str,
    chapter_id: &str,
    body_text: &str,
    expected_source_revision: i64,
    expected_draft_revision: i64,
    expected_accepted_revision: i64,
    actor: &str,
) -> Result<Value, LibraryError> {
    let cleaned = body_text.trim();
    if cleaned.is_empty() || body_text.chars().count() > 4000 ||
        cleaned.chars().count() > 4000
    {
        return Err(LibraryError::Invalid("Note must contain 1 to 4000 characters"));
    }
    if expected_source_revision < 1 || expected_draft_revision < 0 ||
        expected_accepted_revision < 0
    {
        return Err(LibraryError::Invalid("Invalid review note submission"));
    }
    let transaction = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (source, accepted) = chapter_versions(&transaction, book_id, chapter_id)?;
    let draft_revision: Option<i64> = transaction.query_row(
        "SELECT revision FROM editor_drafts WHERE book_id=?1 AND chapter_id=?2",
        params![book_id, chapter_id],
        |row| row.get(0),
    ).optional()?;
    if draft_revision.unwrap_or(0) != expected_draft_revision ||
        source != expected_source_revision || accepted != expected_accepted_revision
    {
        return Err(LibraryError::Conflict(
            "Chapter/draft changed; reload before recording review",
        ));
    }
    let note_id = Uuid::new_v4().simple().to_string();
    let timestamp = now()?;
    transaction.execute(
        "INSERT INTO editor_review_notes \
         (id,book_id,chapter_id,source_revision,draft_revision,accepted_revision,\
          body_text,status,note_revision,author_id,resolver_id,created_at,updated_at) \
         VALUES(?1,?2,?3,?4,?5,?6,?7,'open',1,?8,NULL,?9,?9)",
        params![
            note_id, book_id, chapter_id, expected_source_revision,
            expected_draft_revision, expected_accepted_revision, cleaned, actor, timestamp
        ],
    )?;
    let created = find_note(&transaction, book_id, chapter_id, &note_id)?
        .ok_or(LibraryError::Missing("Review note not found"))?;
    transaction.commit()?;
    Ok(created)
}

/// Python note CAS does not fence chapter/source after creation: historical
/// notes can still be resolved, but only at their latest note revision.
pub fn set_note_status(
    db: &mut Connection,
    book_id: &str,
    chapter_id: &str,
    note_id: &str,
    status: &str,
    expected_revision: i64,
    actor: &str,
) -> Result<Value, LibraryError> {
    if !matches!(status, "open" | "resolved") || expected_revision < 1 {
        return Err(LibraryError::Invalid("Invalid review status or revision"));
    }
    let transaction = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    chapter_versions(&transaction, book_id, chapter_id)?;
    let previous = find_note(&transaction, book_id, chapter_id, note_id)?
        .ok_or(LibraryError::Missing("Review note not found"))?;
    if previous["note_revision"].as_i64() != Some(expected_revision) {
        return Err(LibraryError::Conflict(
            "Review note changed; reload before resolving",
        ));
    }
    if previous["status"] == status {
        transaction.commit()?;
        return Ok(previous);
    }
    let next = expected_revision.checked_add(1)
        .ok_or(LibraryError::Invalid("Review note revision limit reached"))?;
    let timestamp = now()?;
    transaction.execute(
        "UPDATE editor_review_notes SET status=?1,note_revision=?2,\
         resolver_id=?3,updated_at=?4 WHERE id=?5 AND book_id=?6 AND chapter_id=?7",
        params![status, next, actor, timestamp, note_id, book_id, chapter_id],
    )?;
    let updated = find_note(&transaction, book_id, chapter_id, note_id)?
        .ok_or(LibraryError::Missing("Review note not found"))?;
    transaction.commit()?;
    Ok(updated)
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
               source_revision INTEGER NOT NULL,accepted_revision INTEGER NOT NULL,\
               source_html TEXT NOT NULL,accepted_html TEXT,\
               PRIMARY KEY(book_id,chapter_id));\
             CREATE TABLE editor_drafts(\
               book_id TEXT NOT NULL,chapter_id TEXT NOT NULL,revision INTEGER NOT NULL,\
               PRIMARY KEY(book_id,chapter_id));\
             CREATE TABLE editor_review_notes(\
               id TEXT PRIMARY KEY,book_id TEXT NOT NULL,chapter_id TEXT NOT NULL,\
               source_revision INTEGER NOT NULL,draft_revision INTEGER NOT NULL,\
               accepted_revision INTEGER NOT NULL,\
               body_text TEXT NOT NULL CHECK(length(body_text) BETWEEN 1 AND 4000),\
               status TEXT NOT NULL CHECK(status IN ('open','resolved')),\
               note_revision INTEGER NOT NULL CHECK(note_revision > 0),\
               author_id TEXT NOT NULL,resolver_id TEXT,created_at REAL NOT NULL,\
               updated_at REAL NOT NULL,\
               FOREIGN KEY(book_id,chapter_id) REFERENCES book_chapters(book_id,chapter_id));\
             INSERT INTO book_chapters VALUES('b','c',1,0,'<p>Source</p>',NULL);",
        ).unwrap();
        db
    }

    #[test]
    fn list_and_create_are_scoped_and_fenced() {
        let mut db = fixture();
        assert_eq!(list_notes(&db, "b", "c").unwrap(), json!({"notes":[]}));
        assert!(matches!(
            list_notes(&db, "b", "missing"),
            Err(LibraryError::Missing("Chapter not found"))
        ));
        let created = create_note(&mut db, "b", "c", "  Проверить тезис  ", 1, 0, 0, "alice").unwrap();
        assert_eq!(created["body_text"], "Проверить тезис");
        assert_eq!(created["note_revision"], 1);
        assert_eq!(created["status"], "open");
        assert_eq!(created["author_id"], "alice");
        assert_eq!(created["resolver_id"], Value::Null);
        assert_eq!(created["id"].as_str().unwrap().len(), 32);
        assert_eq!(list_notes(&db, "b", "c").unwrap()["notes"][0], created);
        assert!(matches!(
            create_note(&mut db, "b", "c", " ", 1, 0, 0, "bob"),
            Err(LibraryError::Invalid(_))
        ));
        db.execute("INSERT INTO editor_drafts VALUES('b','c',1)",[]).unwrap();
        assert!(matches!(
            create_note(&mut db, "b", "c", "Stale draft", 1, 0, 0, "bob"),
            Err(LibraryError::Conflict("Chapter/draft changed; reload before recording review"))
        ));
        db.execute("UPDATE book_chapters SET source_revision=2 WHERE chapter_id='c'", []).unwrap();
        assert!(matches!(
            create_note(&mut db, "b", "c", "Stale source", 1, 1, 0, "bob"),
            Err(LibraryError::Conflict("Chapter/draft changed; reload before recording review"))
        ));
        assert_eq!(list_notes(&db, "b", "c").unwrap()["notes"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn resolution_cas_noop_and_originals_are_untouched() {
        let mut db = fixture();
        let created = create_note(&mut db, "b", "c", "Review <script>text</script>", 1, 0, 0, "alice").unwrap();
        let id = created["id"].as_str().unwrap();
        let noop = set_note_status(&mut db, "b", "c", id, "open", 1, "bob").unwrap();
        assert_eq!(noop, created);
        let resolved = set_note_status(&mut db, "b", "c", id, "resolved", 1, "bob").unwrap();
        assert_eq!(resolved["note_revision"], 2);
        assert_eq!(resolved["resolver_id"], "bob");
        assert_eq!(resolved["author_id"], "alice");
        assert!(matches!(
            set_note_status(&mut db, "b", "c", id, "open", 1, "alice"),
            Err(LibraryError::Conflict("Review note changed; reload before resolving"))
        ));
        let same = set_note_status(&mut db, "b", "c", id, "resolved", 2, "alice").unwrap();
        assert_eq!(same, resolved);
        assert!(matches!(
            set_note_status(&mut db, "b", "c", "unknown", "resolved", 1, "alice"),
            Err(LibraryError::Missing("Review note not found"))
        ));
        let (source, accepted): (String, Option<String>) = db.query_row(
            "SELECT source_html,accepted_html FROM book_chapters WHERE book_id='b'",
            [], |row| Ok((row.get(0)?, row.get(1)?)),
        ).unwrap();
        assert_eq!(source, "<p>Source</p>");
        assert!(accepted.is_none());
    }
}
