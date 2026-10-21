//! RUST-M1b: Python EditorialStore draft contract on a disposable SQLite.
//! Never write accepted translation, original source HTML, or any other
//! editorial records during a draft save.
use super::{now, LibraryError};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};

struct ChapterVersions {
    source: i64,
    accepted: i64,
}

struct Draft {
    revision: i64,
    source_revision: i64,
    accepted_revision: i64,
    text: String,
    actor: String,
    updated_at: f64,
}

fn versions(db: &Connection, book_id: &str, chapter_id: &str)
    -> Result<ChapterVersions, LibraryError>
{
    let row = db.query_row(
        "SELECT source_revision,accepted_revision FROM book_chapters \
         WHERE book_id=?1 AND chapter_id=?2",
        params![book_id, chapter_id],
        |r| Ok(ChapterVersions { source: r.get(0)?, accepted: r.get(1)? }),
    ).optional()?;
    row.ok_or(LibraryError::Missing("Chapter not found"))
}

fn draft_row(db: &Connection, book_id: &str, chapter_id: &str)
    -> Result<Option<Draft>, LibraryError>
{
    db.query_row(
        "SELECT revision,source_revision,accepted_revision,body_text,actor_id,updated_at \
         FROM editor_drafts WHERE book_id=?1 AND chapter_id=?2",
        params![book_id, chapter_id],
        |r| Ok(Draft {
            revision: r.get(0)?,
            source_revision: r.get(1)?,
            accepted_revision: r.get(2)?,
            text: r.get(3)?,
            actor: r.get(4)?,
            updated_at: r.get(5)?,
        }),
    ).optional().map_err(Into::into)
}

fn serialize(row: Option<&Draft>, chapter: &ChapterVersions) -> Value {
    match row {
        Some(row) => json!({
            "revision": row.revision,
            "source_revision": row.source_revision,
            "accepted_revision": row.accepted_revision,
            "body_text": row.text,
            "actor_id": row.actor,
            "updated_at": row.updated_at,
        }),
        None => json!({
            "revision": 0,
            "source_revision": chapter.source,
            "accepted_revision": chapter.accepted,
            "body_text": "",
            "actor_id": null,
            "updated_at": null,
        }),
    }
}

/// Same default value and privacy as Python EditorialStore.get_draft.
/// No draft is an empty response with the current chapter source/acceptance.
pub fn get_draft(
    db: &Connection, book_id: &str, chapter_id: &str,
) -> Result<Value, LibraryError> {
    let chapter = versions(db, book_id, chapter_id)?;
    let row = draft_row(db, book_id, chapter_id)?;
    Ok(serialize(row.as_ref(), &chapter))
}

/// Exact CAS, explicit rebase, idempotency and append-only draft history.
/// No-op replay does not modify actor, timestamp or audit history.
#[allow(clippy::too_many_arguments)]
pub fn save_draft(
    db: &mut Connection, book_id: &str, chapter_id: &str,
    body_text: &str, expected_revision: i64,
    expected_source_revision: i64, expected_accepted_revision: i64,
    rebase: bool, actor: &str,
) -> Result<Value, LibraryError> {
    if body_text.chars().count() > 1_000_000 ||
        expected_revision < 0 || expected_source_revision < 1 ||
        expected_accepted_revision < 0
    {
        return Err(LibraryError::Invalid("Invalid editor draft submission"));
    }
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let chapter = versions(&tx, book_id, chapter_id)?;
    if chapter.source != expected_source_revision ||
        chapter.accepted != expected_accepted_revision
    {
        return Err(LibraryError::Conflict(
            "Source or accepted translation changed; refresh required",
        ));
    }
    let previous = draft_row(&tx, book_id, chapter_id)?;
    let current = previous.as_ref().map_or(0, |row| row.revision);
    if current != expected_revision {
        return Err(LibraryError::Conflict(
            "Draft revision changed; load and compare before overwriting",
        ));
    }
    if let Some(row) = previous.as_ref() {
        if (row.source_revision != expected_source_revision ||
            row.accepted_revision != expected_accepted_revision) && !rebase
        {
            return Err(LibraryError::Conflict(
                "Draft was based on older revisions; explicit rebase required",
            ));
        }
        if row.text == body_text && !rebase {
            let answer = serialize(Some(row), &chapter);
            tx.commit()?;
            return Ok(answer);
        }
    }
    let next = expected_revision + 1;
    let stamp = now()?;
    tx.execute(
        "INSERT INTO editor_drafts \
         (book_id,chapter_id,revision,source_revision,accepted_revision,\
          body_text,actor_id,updated_at) \
         VALUES(?1,?2,?3,?4,?5,?6,?7,?8) \
         ON CONFLICT(book_id,chapter_id) DO UPDATE SET \
         revision=excluded.revision,\
         source_revision=excluded.source_revision,\
         accepted_revision=excluded.accepted_revision,\
         body_text=excluded.body_text,\
         actor_id=excluded.actor_id,\
         updated_at=excluded.updated_at",
        params![book_id, chapter_id, next, expected_source_revision,
            expected_accepted_revision, body_text, actor, stamp],
    )?;
    tx.execute(
        "INSERT INTO editor_draft_revisions \
         (book_id,chapter_id,revision,source_revision,accepted_revision,\
          body_text,actor_id,created_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",
        params![book_id, chapter_id, next, expected_source_revision,
            expected_accepted_revision, body_text, actor, stamp],
    )?;
    let saved = draft_row(&tx, book_id, chapter_id)?
        .ok_or(LibraryError::Missing("Chapter not found"))?;
    let result = serialize(Some(&saved), &chapter);
    tx.commit()?;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch(
            "PRAGMA foreign_keys=ON;\
             CREATE TABLE book_chapters( \
               book_id TEXT NOT NULL,chapter_id TEXT NOT NULL, \
               source_revision INTEGER NOT NULL,accepted_revision INTEGER NOT NULL, \
               PRIMARY KEY(book_id,chapter_id));\
             CREATE TABLE editor_drafts( \
               book_id TEXT NOT NULL,chapter_id TEXT NOT NULL,revision INTEGER NOT NULL, \
               source_revision INTEGER NOT NULL,accepted_revision INTEGER NOT NULL, \
               body_text TEXT NOT NULL,actor_id TEXT NOT NULL,updated_at REAL NOT NULL, \
               PRIMARY KEY(book_id,chapter_id), \
               FOREIGN KEY(book_id,chapter_id) REFERENCES book_chapters(book_id,chapter_id));\
             CREATE TABLE editor_draft_revisions( \
               book_id TEXT NOT NULL,chapter_id TEXT NOT NULL,revision INTEGER NOT NULL, \
               source_revision INTEGER NOT NULL,accepted_revision INTEGER NOT NULL, \
               body_text TEXT NOT NULL,actor_id TEXT NOT NULL,created_at REAL NOT NULL, \
               PRIMARY KEY(book_id,chapter_id,revision));\
             INSERT INTO book_chapters VALUES('b','c',1,0);",
        ).unwrap();
        db
    }

    #[test]
    fn absent_draft_write_replay_and_cas_history() {
        let mut db = fixture();
        let empty = get_draft(&db,"b","c").unwrap();
        assert_eq!(empty["revision"],0);
        assert_eq!(empty["actor_id"],Value::Null);
        let first=save_draft(&mut db,"b","c","Текст",0,1,0,false,"mod").unwrap();
        assert_eq!(first["revision"],1);
        assert_eq!(first["body_text"],"Текст");
        let replay=save_draft(&mut db,"b","c","Текст",1,1,0,false,"chief").unwrap();
        assert_eq!(first,replay);
        assert!(matches!(
            save_draft(&mut db,"b","c","Потеря",0,1,0,false,"chief"),
            Err(LibraryError::Conflict("Draft revision changed; load and compare before overwriting"))
        ));
        let count:i64 = db.query_row(
            "SELECT COUNT(*) FROM editor_draft_revisions",[],|r|r.get(0)
        ).unwrap();
        assert_eq!(count,1);
    }

    #[test]
    fn source_or_accepted_change_requires_explicit_rebase() {
        let mut db=fixture();
        save_draft(&mut db,"b","c","Исходный",0,1,0,false,"mod").unwrap();
        db.execute(
            "UPDATE book_chapters SET accepted_revision=1 WHERE chapter_id='c'",[]
        ).unwrap();
        assert!(matches!(
            save_draft(&mut db,"b","c","Потеря",1,1,0,false,"mod"),
            Err(LibraryError::Conflict("Source or accepted translation changed; refresh required"))
        ));
        assert!(matches!(
            save_draft(&mut db,"b","c","Новое",1,1,1,false,"mod"),
            Err(LibraryError::Conflict("Draft was based on older revisions; explicit rebase required"))
        ));
        let changed=save_draft(&mut db,"b","c","Новое",1,1,1,true,"chief").unwrap();
        assert_eq!(changed["revision"],2);
        assert_eq!(changed["accepted_revision"],1);
        assert_eq!(changed["actor_id"],"chief");
        assert!(matches!(
            get_draft(&db,"b","unknown"),
            Err(LibraryError::Missing("Chapter not found"))
        ));
    }
}
