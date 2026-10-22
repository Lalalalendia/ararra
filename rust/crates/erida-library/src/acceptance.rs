//! RUST-02e/M1l: source-revision-fenced accepted translation writes.
//! The exact safe textarea language is shared with export. Legacy proven
//! plain paragraphs remain supported; arbitrary HTML remains fail-closed.
use super::{get_chapter, now, LibraryError};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};

#[path = "editor_html.rs"]
pub(crate) mod editor_html;

fn visible_text_proven(html: &str) -> bool {
    if let Some(parsed) = editor_html::parse(html) {
        return parsed.has_text();
    }
    if !html.starts_with("<p>") || !html.ends_with("</p>") {
        return false;
    }
    let mut remaining = html;
    let mut visible_paragraphs = 0usize;
    while !remaining.is_empty() {
        if !remaining.starts_with("<p>") {
            return false;
        }
        let Some(end) = remaining.find("</p>") else {
            return false;
        };
        let text = &remaining[3..end];
        if text.contains('<') || text.contains('>') || text.contains('&')
            || text.chars().any(|ch| ch.is_control() && !matches!(ch, '\n' | '\r' | '\t'))
        {
            return false;
        }
        if !text.chars().any(char::is_alphanumeric) {
            return false;
        }
        visible_paragraphs += 1;
        remaining = &remaining[end + "</p>".len()..];
    }
    visible_paragraphs > 0
}

/// Preserve Python accept_translation transaction and immutable history.
/// The existing chapter source and glossary remain untouched. In particular,
/// entity decoding for validation must never rewrite the stored HTML bytes.
pub fn accept_translation_conservative(
    db: &mut Connection,
    book_id: &str,
    chapter_id: &str,
    translated_html: &str,
    expected_revision: i64,
    expected_source_revision: i64,
    actor: &str,
) -> Result<serde_json::Value, LibraryError> {
    if translated_html.trim().is_empty() || translated_html.chars().count() > 2_000_000
        || expected_revision < 0 || expected_source_revision < 1
    {
        return Err(LibraryError::Invalid("A nonempty accepted translation is required"));
    }
    if !visible_text_proven(translated_html) {
        return Err(LibraryError::UnsupportedTranslation);
    }
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let original: Option<(i64, i64, Option<String>, i64)> = tx
        .query_row(
            "SELECT source_revision,accepted_revision,accepted_html,accepted_source_revision \
             FROM book_chapters WHERE book_id=?1 AND chapter_id=?2",
            params![book_id, chapter_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?;
    let Some((source_revision, accepted_revision, previous_html, previous_source)) = original
    else {
        return Err(LibraryError::Missing("Chapter not found"));
    };
    if accepted_revision != expected_revision {
        return Err(LibraryError::Conflict(
            "Accepted translation revision mismatch",
        ));
    }
    if source_revision != expected_source_revision {
        return Err(LibraryError::Conflict(
            "Source revision changed before translation acceptance",
        ));
    }
    if previous_html.as_deref() == Some(translated_html) && previous_source == source_revision {
        let result = get_chapter(&tx, book_id, chapter_id)?
            .ok_or(LibraryError::Missing("Chapter not found"))?;
        tx.commit()?;
        return Ok(result);
    }
    let updated = now()?;
    tx.execute(
        "UPDATE book_chapters \
         SET accepted_html=?1,accepted_revision=?2,accepted_source_revision=?3,updated_at=?4 \
         WHERE book_id=?5 AND chapter_id=?6",
        params![translated_html, expected_revision + 1, source_revision, updated,
                book_id, chapter_id],
    )?;
    tx.execute(
        "INSERT INTO book_accepted_revisions \
         (book_id,chapter_id,revision,source_revision,translated_html,actor_id,created_at) \
         VALUES(?1,?2,?3,?4,?5,?6,?7)",
        params![book_id, chapter_id, expected_revision + 1, source_revision,
                translated_html, actor, updated],
    )?;
    let result = get_chapter(&tx, book_id, chapter_id)?
        .ok_or(LibraryError::Missing("Chapter not found"))?;
    tx.commit()?;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn db() -> Connection {
        let con = Connection::open_in_memory().unwrap();
        con.execute_batch(
            "PRAGMA foreign_keys=ON;\
             CREATE TABLE book_chapters(\
               book_id TEXT NOT NULL,chapter_id TEXT NOT NULL,chapter_index INTEGER,\
               chapter_path TEXT,source_html TEXT,source_hash TEXT,\
               source_revision INTEGER NOT NULL,accepted_html TEXT,\
               accepted_revision INTEGER NOT NULL DEFAULT 0,\
               accepted_source_revision INTEGER NOT NULL DEFAULT 0,updated_at REAL,\
               PRIMARY KEY(book_id,chapter_id));\
             CREATE TABLE book_accepted_revisions(\
               book_id TEXT,chapter_id TEXT,revision INTEGER,source_revision INTEGER,\
               translated_html TEXT,actor_id TEXT,created_at REAL,\
               PRIMARY KEY(book_id,chapter_id,revision),\
               FOREIGN KEY(book_id,chapter_id) REFERENCES book_chapters(book_id,chapter_id));\
             INSERT INTO book_chapters \
               (book_id,chapter_id,chapter_index,chapter_path,source_html,source_hash,\
                source_revision,updated_at) \
               VALUES('b','c',1,'1.xhtml','<p>source</p>','sha',1,0);",
        ).unwrap();
        con
    }

    #[test]
    fn accepted_revision_replay_and_audit_are_atomic() {
        let mut con = db();
        let html = "<p>Перевод первой главы.</p>";
        let first = accept_translation_conservative(
            &mut con, "b", "c", html, 0, 1, "mod",
        ).unwrap();
        assert_eq!(first["accepted_revision"], 1);
        assert_eq!(first["accepted_current"], true);
        assert_eq!(first["accepted_html"], html);
        assert_eq!(
            accept_translation_conservative(&mut con,"b","c",html,1,1,"mod").unwrap(),
            first
        );
        let count:i64 = con.query_row(
            "SELECT COUNT(*) FROM book_accepted_revisions",[],|row|row.get(0)
        ).unwrap();
        assert_eq!(count,1);
        assert!(matches!(
            accept_translation_conservative(&mut con,"b","c","<p>Другой перевод.</p>",0,1,"mod"),
            Err(LibraryError::Conflict("Accepted translation revision mismatch"))
        ));
    }

    #[test]
    fn stale_source_cannot_be_accepted_and_revision_history_survives() {
        let mut con=db();
        accept_translation_conservative(&mut con,"b","c","<p>Версия 1.</p>",0,1,"mod").unwrap();
        con.execute(
            "UPDATE book_chapters SET source_revision=2 WHERE chapter_id='c'",[]
        ).unwrap();
        assert!(matches!(
            accept_translation_conservative(&mut con,"b","c","<p>Версия 2.</p>",1,1,"mod"),
            Err(LibraryError::Conflict("Source revision changed before translation acceptance"))
        ));
        let latest = accept_translation_conservative(
            &mut con,"b","c","<p>Версия 2.</p>",1,2,"mod",
        ).unwrap();
        assert_eq!(latest["accepted_revision"],2);
        assert_eq!(latest["accepted_source_revision"],2);
        assert_eq!(latest["accepted_current"],true);
        let revisions:i64 = con.query_row(
            "SELECT COUNT(*) FROM book_accepted_revisions WHERE book_id='b'",[],
            |row|row.get(0)
        ).unwrap();
        assert_eq!(revisions,2);
    }

    #[test]
    fn unsupported_html_is_rejected_without_writing() {
        let mut con=db();
        for html in ["<p> </p>","<p>&unknown;</p>",
            "<div>Готово.</div>","<p>Готово <b>да</b></p>",
            "<p onclick='x'>Готово.</p>","<p>Текст<br onclick='x'>Далее</p>"
        ] {
            assert!(matches!(
                accept_translation_conservative(&mut con,"b","c",html,0,1,"mod"),
                Err(LibraryError::UnsupportedTranslation)
            ));
        }
        assert!(matches!(
            accept_translation_conservative(&mut con,"b","missing","<p>Да.</p>",0,1,"mod"),
            Err(LibraryError::Missing("Chapter not found"))
        ));
        let count:i64=con.query_row(
            "SELECT COUNT(*) FROM book_accepted_revisions",[],|row|row.get(0)
        ).unwrap();
        assert_eq!(count,0);
    }

    #[test]
    fn textarea_html_is_stored_and_audited_without_entity_rewriting() {
        let mut con = db();
        let raw = "<p>Кавычки &quot;да&quot; &amp; &#39;нет&#39;.</p>\n<p>Строка<br>Продолжение &lt;текст&gt;.</p>";
        let accepted = accept_translation_conservative(&mut con,"b","c",raw,0,1,"mod").unwrap();
        assert_eq!(accepted["accepted_html"], raw);
        let history: String = con.query_row(
            "SELECT translated_html FROM book_accepted_revisions WHERE revision=1",
            [], |row| row.get(0),
        ).unwrap();
        assert_eq!(history, raw);
        assert_eq!(accept_translation_conservative(&mut con,"b","c",raw,1,1,"mod").unwrap(), accepted);
        assert!(visible_text_proven("<p>&amp;</p>"));
        assert!(visible_text_proven("<p>!</p>"));
    }
}
