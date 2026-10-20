//! Read-only book-chapter projections, including stale accepted text semantics.
//! The existing Python BookLibrary remains sole writer of chapter bodies.
use super::LibraryError;
use rusqlite::{params, Connection, OptionalExtension, Row};
use serde_json::{json, Value};

fn accepted_current(accepted: Option<&str>, accepted_source: i64, source: i64) -> bool {
    accepted.is_some_and(|html| !html.is_empty()) && accepted_source == source
}

fn chapter_projection(row: &Row<'_>) -> Result<Value, LibraryError> {
    let accepted: Option<String> = row.get("accepted_html")?;
    let source_revision = row.get::<_, i64>("source_revision")?;
    let accepted_source_revision = row.get::<_, i64>("accepted_source_revision")?;
    Ok(json!({
        "book_id": row.get::<_, String>("book_id")?,
        "chapter_id": row.get::<_, String>("chapter_id")?,
        "chapter_index": row.get::<_, i64>("chapter_index")?,
        "chapter_path": row.get::<_, String>("chapter_path")?,
        "source_html": row.get::<_, String>("source_html")?,
        "source_hash": row.get::<_, String>("source_hash")?,
        "source_revision": source_revision,
        "accepted_revision": row.get::<_, i64>("accepted_revision")?,
        "accepted_source_revision": accepted_source_revision,
        "accepted_current": accepted_current(accepted.as_deref(), accepted_source_revision, source_revision),
        "accepted_html": accepted,
    }))
}

pub fn get_chapter(
    db: &Connection,
    book_id: &str,
    chapter_id: &str,
) -> Result<Option<Value>, LibraryError> {
    let mut stmt = db.prepare(
        "SELECT * FROM book_chapters WHERE book_id=?1 AND chapter_id=?2",
    )?;
    let mut rows = stmt.query(params![book_id, chapter_id])?;
    match rows.next()? {
        Some(row) => Ok(Some(chapter_projection(row)?)),
        None => Ok(None),
    }
}

pub fn list_chapters(
    db: &Connection,
    book_id: &str,
) -> Result<Option<Value>, LibraryError> {
    let book_exists: Option<i64> = db
        .query_row("SELECT 1 FROM books WHERE id=?1", [book_id], |row| row.get(0))
        .optional()?;
    if book_exists.is_none() {
        return Ok(None);
    }
    // The directory deliberately does not project entire chapter bodies.
    let mut stmt = db.prepare(
        "SELECT chapter_id,chapter_index,chapter_path,source_revision, \
         accepted_revision,accepted_source_revision,accepted_html \
         FROM book_chapters WHERE book_id=?1 ORDER BY chapter_index,chapter_id",
    )?;
    let mut rows = stmt.query([book_id])?;
    let mut chapters = Vec::new();
    while let Some(row) = rows.next()? {
        let accepted: Option<String> = row.get("accepted_html")?;
        let source_revision = row.get::<_, i64>("source_revision")?;
        let accepted_source_revision = row.get::<_, i64>("accepted_source_revision")?;
        chapters.push(json!({
            "chapter_id": row.get::<_, String>("chapter_id")?,
            "chapter_index": row.get::<_, i64>("chapter_index")?,
            "chapter_path": row.get::<_, String>("chapter_path")?,
            "source_revision": source_revision,
            "accepted_revision": row.get::<_, i64>("accepted_revision")?,
            "accepted_source_revision": accepted_source_revision,
            "accepted_current": accepted_current(
                accepted.as_deref(), accepted_source_revision, source_revision
            ),
        }));
    }
    Ok(Some(json!({"book_id": book_id, "chapters": chapters})))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch(
            "CREATE TABLE books(id TEXT PRIMARY KEY);\
             CREATE TABLE book_chapters(\
             book_id TEXT,chapter_id TEXT,chapter_index INTEGER,chapter_path TEXT,\
             source_html TEXT,source_hash TEXT,source_revision INTEGER,\
             accepted_html TEXT,accepted_revision INTEGER,accepted_source_revision INTEGER); \
             INSERT INTO books(id) VALUES('b'); \
             INSERT INTO book_chapters VALUES('b','c',1,'1.xhtml','<p>source</p>',\
             'abcdef',2,'<p>accepted</p>',1,1);",
        ).unwrap();
        db
    }

    #[test]
    fn stale_acceptance_is_not_current() {
        let db = fixture();
        let detailed = get_chapter(&db, "b", "c").unwrap().unwrap();
        assert_eq!(detailed["accepted_current"], false);
        assert_eq!(detailed["accepted_html"], "<p>accepted</p>");
        let directory = list_chapters(&db, "b").unwrap().unwrap();
        assert_eq!(directory["chapters"][0]["accepted_current"], false);
        assert!(directory["chapters"][0].get("source_html").is_none());
    }

    #[test]
    fn current_revision_and_missing_are_correct() {
        let db = fixture();
        db.execute(
            "UPDATE book_chapters SET accepted_source_revision=source_revision",
            [],
        ).unwrap();
        assert_eq!(
            get_chapter(&db, "b", "c").unwrap().unwrap()["accepted_current"], true
        );
        assert_eq!(
            list_chapters(&db, "b").unwrap().unwrap()["chapters"][0]["accepted_current"], true
        );
        assert!(get_chapter(&db, "b", "absent").unwrap().is_none());
        assert!(list_chapters(&db, "absent").unwrap().is_none());
    }
}
