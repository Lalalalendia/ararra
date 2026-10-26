//! RUST-02b: book directory and owner CAS on existing Python SQLite tables.
//! No schema initializer or production writes. Glossary CAS is disposable-only.
use rusqlite::{params, Connection, TransactionBehavior};
use serde_json::{json, Value};

pub mod chapters;
mod chapter_writes;
mod acceptance;
mod accepted_history;
pub mod book_export;
pub mod import_preview;
pub mod import_stage;
pub mod import_confirm;
pub mod translation_batches;
mod editorial;
mod review_notes;
mod glossary;
pub use chapters::{get_chapter, list_chapters};
pub use chapter_writes::put_chapter_conservative;
pub use acceptance::accept_translation_conservative;
pub use accepted_history::{accepted_history, accepted_version, restore_accepted};
pub use editorial::{get_draft as get_editor_draft, save_draft as save_editor_draft};
pub use review_notes::{list_notes as list_editor_notes, create_note as create_editor_note, set_note_status as set_editor_note_status};
pub use glossary::put_glossary;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug)]
pub enum LibraryError {
    Sqlite(rusqlite::Error),
    Invalid(&'static str),
    Conflict(&'static str),
    Missing(&'static str),
    Malformed,
    UnsupportedSource,
    UnsupportedTranslation,
    GlossaryRuleConflict(String),
}
impl From<rusqlite::Error> for LibraryError {
    fn from(error: rusqlite::Error) -> Self {
        Self::Sqlite(error)
    }
}

fn now() -> Result<f64, LibraryError> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs_f64())
        .map_err(|_| LibraryError::Invalid("System clock unavailable"))
}

fn book_from_row(row: &rusqlite::Row<'_>) -> Result<Value, LibraryError> {
    let glossary_text: String = row.get("glossary_json")?;
    let versions_text: String = row.get("glossary_versions_json")?;
    let glossary =
        serde_json::from_str::<Value>(&glossary_text).map_err(|_| LibraryError::Malformed)?;
    let versions =
        serde_json::from_str::<Value>(&versions_text).map_err(|_| LibraryError::Malformed)?;
    Ok(json!({
        "book_id": row.get::<_, String>("id")?,
        "title": row.get::<_, String>("title")?,
        "owner_member_id": row.get::<_, Option<String>>("owner_member_id")?,
        "owner_revision": row.get::<_, i64>("owner_revision")?,
        "glossary": glossary,
        "glossary_versions": versions,
        "glossary_authority": row.get::<_, String>("glossary_authority")?,
        "glossary_revision": row.get::<_, i64>("glossary_revision")?,
    }))
}

pub fn get_book(db: &Connection, id: &str) -> Result<Option<Value>, LibraryError> {
    let mut stmt = db.prepare("SELECT * FROM books WHERE id=?1")?;
    let mut rows = stmt.query([id])?;
    match rows.next()? {
        Some(row) => Ok(Some(book_from_row(row)?)),
        None => Ok(None),
    }
}

pub fn list_books(db: &Connection) -> Result<Value, LibraryError> {
    let mut stmt = db.prepare(
        "SELECT b.id,b.title,b.owner_member_id,b.owner_revision,\
         COUNT(c.chapter_id) AS chapter_count,\
         COALESCE(SUM(CASE \
         WHEN c.accepted_revision > 0 AND c.accepted_source_revision = c.source_revision \
         THEN 1 ELSE 0 END),0) AS accepted_current_count \
         FROM books AS b LEFT JOIN book_chapters AS c ON c.book_id=b.id \
         GROUP BY b.id ORDER BY b.title,b.id",
    )?;
    let mut rows = stmt.query([])?;
    let mut books = Vec::new();
    while let Some(row) = rows.next()? {
        books.push(json!({
            "book_id": row.get::<_, String>("id")?,
            "title": row.get::<_, String>("title")?,
            "owner_member_id": row.get::<_, Option<String>>("owner_member_id")?,
            "owner_revision": row.get::<_, i64>("owner_revision")?,
            "chapter_count": row.get::<_, i64>("chapter_count")?,
            "accepted_current_count": row.get::<_, i64>("accepted_current_count")?,
        }));
    }
    Ok(json!({"books": books}))
}

/// Insert a single book and its owner event in one BEGIN IMMEDIATE transaction.
/// Replay with identical id/title/owner is a no-op; conflicting replay is 409.
pub fn create_book(
    db: &mut Connection,
    id: &str,
    title: &str,
    owner: &str,
    actor: &str,
) -> Result<Value, LibraryError> {
    let id = id.trim();
    let title = title.trim();
    let owner = owner.trim();
    if id.is_empty() || title.is_empty()
        || id.chars().count() > 128 || title.chars().count() > 500
    {
        return Err(LibraryError::Invalid("Valid book id and title are required"));
    }
    if owner.is_empty() || owner.chars().count() > 128 {
        return Err(LibraryError::Invalid("A valid moderator owner is required"));
    }
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    if let Some(existing) = get_book(&tx, id)? {
        if existing["title"] != title || existing["owner_member_id"] != owner {
            return Err(LibraryError::Conflict(
                "Book already exists with a different title or owner",
            ));
        }
        tx.commit()?;
        return Ok(existing);
    }
    let timestamp = now()?;
    tx.execute(
        "INSERT INTO books(id,title,owner_member_id,created_at,updated_at) \
         VALUES(?1,?2,?3,?4,?4)",
        params![id, title, owner, timestamp],
    )?;
    tx.execute(
        "INSERT INTO book_owner_events \
         (book_id,revision,previous_owner_member_id,new_owner_member_id,actor_id,changed_at) \
         VALUES(?1,0,NULL,?2,?3,?4)",
        params![id, owner, actor, timestamp],
    )?;
    let response = get_book(&tx, id)?.ok_or(LibraryError::Missing("Book not found"))?;
    tx.commit()?;
    Ok(response)
}

/// Revision-fenced update and immutable audit event, no lost ownership update.
/// Caller must independently have verified admin rights and owner's active role.
pub fn transfer_owner(
    db: &mut Connection,
    id: &str,
    new_owner: &str,
    expected_revision: i64,
    actor: &str,
) -> Result<Value, LibraryError> {
    let new_owner = new_owner.trim();
    if new_owner.is_empty() || new_owner.chars().count() > 128 {
        return Err(LibraryError::Invalid("A valid moderator owner is required"));
    }
    if expected_revision < 0 {
        return Err(LibraryError::Invalid("Owner revision must be nonnegative"));
    }
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let row = get_book(&tx, id)?.ok_or(LibraryError::Missing("Book not found"))?;
    if row["owner_revision"].as_i64() != Some(expected_revision) {
        return Err(LibraryError::Conflict("Book owner revision mismatch"));
    }
    if row["owner_member_id"] == new_owner {
        tx.commit()?;
        return Ok(row);
    }
    let previous_owner = row["owner_member_id"].as_str();
    let updated = now()?;
    let next_revision = expected_revision + 1;
    tx.execute(
        "UPDATE books SET owner_member_id=?1,owner_revision=?2,updated_at=?3 WHERE id=?4",
        params![new_owner, next_revision, updated, id],
    )?;
    tx.execute(
        "INSERT INTO book_owner_events \
         (book_id,revision,previous_owner_member_id,new_owner_member_id,actor_id,changed_at) \
         VALUES(?1,?2,?3,?4,?5,?6)",
        params![id, next_revision, previous_owner, new_owner, actor, updated],
    )?;
    let response = get_book(&tx, id)?.ok_or(LibraryError::Missing("Book not found"))?;
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
             CREATE TABLE books(id TEXT PRIMARY KEY,title TEXT,owner_member_id TEXT,\
             owner_revision INTEGER NOT NULL DEFAULT 0,\
             glossary_json TEXT NOT NULL DEFAULT '{}',\
             glossary_versions_json TEXT NOT NULL DEFAULT '{}',\
             glossary_authority TEXT NOT NULL DEFAULT 'READY',\
             glossary_revision INTEGER NOT NULL DEFAULT 0,\
             created_at REAL,updated_at REAL);\
             CREATE TABLE book_owner_events(book_id TEXT,revision INTEGER,\
             previous_owner_member_id TEXT,new_owner_member_id TEXT,actor_id TEXT,\
             changed_at REAL,PRIMARY KEY(book_id,revision));\
             CREATE TABLE book_chapters(book_id TEXT,chapter_id TEXT,\
             accepted_revision INTEGER,accepted_source_revision INTEGER,source_revision INTEGER);",
        ).unwrap();
        db
    }

    #[test]
    fn create_replay_and_conflicting_name_do_not_duplicate_events() {
        let mut db = fixture();
        let first = create_book(&mut db, "bk", "Book", "mod", "mod").unwrap();
        assert_eq!(first["owner_revision"], 0);
        assert_eq!(create_book(&mut db, "bk", "Book", "mod", "mod").unwrap(), first);
        assert!(matches!(
            create_book(&mut db, "bk", "Different", "mod", "mod"),
            Err(LibraryError::Conflict(_))
        ));
        let count: i64 = db.query_row(
            "SELECT COUNT(*) FROM book_owner_events WHERE book_id='bk'",
            [],|r|r.get(0)
        ).unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn compare_and_swap_owner_with_immutable_event() {
        let mut db = fixture();
        create_book(&mut db, "bk", "Book", "mod", "mod").unwrap();
        assert!(matches!(
            transfer_owner(&mut db, "bk", "new", 1, "chief"),
            Err(LibraryError::Conflict(_))
        ));
        let changed = transfer_owner(&mut db, "bk", "new", 0, "chief").unwrap();
        assert_eq!(changed["owner_revision"], 1);
        assert_eq!(changed["owner_member_id"], "new");
        assert_eq!(transfer_owner(&mut db, "bk", "new", 1, "chief").unwrap(), changed);
        assert_eq!(list_books(&db).unwrap()["books"][0]["accepted_current_count"], 0);
        let event_count: i64=db.query_row(
            "SELECT COUNT(*) FROM book_owner_events WHERE book_id='bk'",
            [],|r|r.get(0)
        ).unwrap();
        assert_eq!(event_count,2);
    }
}
