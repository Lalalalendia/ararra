//! RUST-02d: revision-fenced chapter source writes for a PROVEN no-op
//! subset of Python's source policy. Unsupported HTML is rejected, never
//! silently fingerprinted as though strip_source_boilerplate() were identity.
use super::{get_chapter, LibraryError};
use rusqlite::{params, Connection, ErrorCode, OptionalExtension, TransactionBehavior};
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;
use std::time::{SystemTime, UNIX_EPOCH};

fn source_hash_with_verified_policy(html: &str, chapter_path: &str) -> Result<String, LibraryError> {
    // RUST-03g: a second Python-oracle-tested, tightly bounded inline grammar
    // is available only if plain-paragraph cleaning rejects the input.
    // Neither path performs best-effort BeautifulSoup serialization.
    // Contextual chapter number/title logic has not been ported, so this
    // narrow branch explicitly forbids contexts likely to require it.
    let lowered_path = chapter_path.to_lowercase();
    let neutral_context = !chapter_path.chars().any(char::is_numeric)
        && !["chapter", "глава", "第", "章", "節", "节", "回"]
            .iter().any(|token| lowered_path.contains(*token));
    let compact_digits = html.chars()
        .filter(|ch| ch.is_ascii_digit())
        .collect::<String>();
    let no_unverified_heading = !html.contains("第");
    let no_unverified_domain = !compact_digits.contains("101");
    if neutral_context && no_unverified_heading && no_unverified_domain {
        if let Ok(cleaned) = erida_source_clean::clean_plain_paragraphs(
            html, Some(chapter_path),
        ).or_else(|_| erida_source_clean::clean_inline_paragraphs(
            html, Some(chapter_path),
        )) {
            // A chapter containing only a removed watermark/tail is not
            // meaningfully usable; it remains Python-owned until complete
            // per-chapter context and Partial/Needs Review are implemented.
            if !cleaned.html.trim().is_empty() {
                return Ok(cleaned.source_hash);
            }
        }
    }
    // Previous RUST-02d rule remains as conservative compatibility fallback.
    // Unsupported input stays rejected BEFORE opening any SQLite transaction.
    // Python BookLibrary source_hash = SHA256(strip_source_boilerplate(html)).
    // Its source_units analyzer uses BeautifulSoup and removes proven scraper
    // boilerplate and next-chapter tails. We cannot hash arbitrary HTML until
    // an exact Rust port of that policy is proved.
    //
    // Proof for this deliberately narrow subset:
    // * adjacent, attribute-free <p>text</p>, each exactly one visible node;
    // * no markup, entities or control sequences inside that node;
    // * >260 NFKC alphanumeric symbols PER node (so the <=220 navigation
    //   heuristic and <=260 standalone promotion heuristic cannot trigger);
    // * no 101... domain token even after digit/space deobfuscation;
    // * no decorative-only pseudo text that normalizes to "本章完".
    //
    // In this subset Python removes zero nodes, so it hashes RAW source_html.
    // Reject everything else: never misreport a source revision/hash.
    if !html.starts_with("<p>") || !html.ends_with("</p>") {
        return Err(LibraryError::UnsupportedSource);
    }
    let mut remaining = html;
    let mut paragraphs = 0usize;
    while !remaining.is_empty() {
        if !remaining.starts_with("<p>") {
            return Err(LibraryError::UnsupportedSource);
        }
        let end = remaining.find("</p>").ok_or(LibraryError::UnsupportedSource)?;
        let text = &remaining[3..end];
        if text.is_empty()
            || text.contains('<') || text.contains('>') || text.contains('&')
            || text.chars().any(|character| {
                character.is_control() && !matches!(character, '\n' | '\t' | '\r')
            })
        {
            return Err(LibraryError::UnsupportedSource);
        }
        let normalized = text.nfkc().collect::<String>();
        // '丨' is considered an ideograph by Unicode but Python explicitly
        // strips it as a decorative separator. Never count it as prose.
        let visible_alnum = normalized
            .chars()
            .filter(|ch| ch.is_alphanumeric() && *ch != '丨')
            .count();
        if visible_alnum <= 260 {
            return Err(LibraryError::UnsupportedSource);
        }
        // Python detects domains after compacting whitespace/decorative chars.
        // Reject a conservative *superset* of such matches.
        let digits = normalized
            .chars()
            .filter(|ch| ch.is_ascii_digit())
            .collect::<String>();
        if digits.contains("101") {
            return Err(LibraryError::UnsupportedSource);
        }
        paragraphs += 1;
        remaining = &remaining[end + "</p>".len()..];
    }
    if paragraphs == 0 {
        return Err(LibraryError::UnsupportedSource);
    }
    Ok(hex::encode(Sha256::digest(html.as_bytes())))
}

fn normalized_chapter_path(input: &str) -> Result<String, LibraryError> {
    // Match chaptera_server.library._normalized_path exactly.
    let path = input.trim().replace('\\', "/");
    if path.is_empty() || path.starts_with('/') || path.split('/').any(|p| p == "..") {
        return Err(LibraryError::Invalid(
            "chapter_path must be a non-empty relative path without '..'",
        ));
    }
    Ok(path)
}

fn now() -> Result<f64, LibraryError> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|t| t.as_secs_f64())
        .map_err(|_| LibraryError::Invalid("System clock unavailable"))
}

/// Save a chapter using the exact Python BEGIN IMMEDIATE + expected_revision
/// transaction shape. This intentionally has no book-ownership restriction:
/// the Python endpoint requires team membership but not ownership.
pub fn put_chapter_conservative(
    db: &mut Connection,
    book_id: &str,
    chapter_id: &str,
    chapter_index: i64,
    chapter_path: &str,
    source_html: &str,
    expected_revision: i64,
) -> Result<serde_json::Value, LibraryError> {
    let id = chapter_id.trim();
    let path = normalized_chapter_path(chapter_path)?;
    if id.is_empty() || id.chars().count() > 255 || chapter_index <= 0
        || source_html.trim().is_empty() || source_html.chars().count() > 2_000_000
        || expected_revision < 0
    {
        return Err(LibraryError::Invalid(
            "Valid chapter id, index, path and source HTML are required",
        ));
    }
    // Validate and hash BEFORE obtaining SQLite's write lock.
    let hash = source_hash_with_verified_policy(source_html, &path)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let exists: Option<i64> = tx
        .query_row("SELECT 1 FROM books WHERE id=?1", [book_id], |row| row.get(0))
        .optional()?;
    if exists.is_none() {
        return Err(LibraryError::Missing("Book not found"));
    }
    let current: Option<(i64, i64, String, String)> = tx
        .query_row(
            "SELECT source_revision,chapter_index,chapter_path,source_html \
             FROM book_chapters WHERE book_id=?1 AND chapter_id=?2",
            params![book_id, id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()?;
    let previous_revision = current.as_ref().map_or(0, |row| row.0);
    if previous_revision != expected_revision {
        return Err(LibraryError::Conflict("Chapter source revision mismatch"));
    }
    if current.as_ref().is_some_and(|row| {
        row.1 == chapter_index && row.2 == path && row.3 == source_html
    }) {
        let result = get_chapter(&tx, book_id, id)?
            .ok_or(LibraryError::Missing("Chapter not found"))?;
        tx.commit()?;
        return Ok(result);
    }
    let stamp = now()?;
    let result = if current.is_some() {
        tx.execute(
            "UPDATE book_chapters SET chapter_index=?1,chapter_path=?2, \
             source_html=?3,source_hash=?4,source_revision=?5,updated_at=?6 \
             WHERE book_id=?7 AND chapter_id=?8",
            params![chapter_index, path, source_html, hash,
                previous_revision + 1, stamp, book_id, id],
        )
    } else {
        tx.execute(
            "INSERT INTO book_chapters \
             (book_id,chapter_id,chapter_index,chapter_path,source_html, \
              source_hash,source_revision,updated_at) \
             VALUES(?1,?2,?3,?4,?5,?6,1,?7)",
            params![book_id, id, chapter_index, path, source_html, hash, stamp],
        )
    };
    match result {
        Ok(_) => {}
        Err(rusqlite::Error::SqliteFailure(err, _))
            if err.code == ErrorCode::ConstraintViolation =>
        {
            return Err(LibraryError::Conflict(
                "Chapter index or path already belongs to another chapter",
            ));
        }
        Err(other) => return Err(LibraryError::Sqlite(other)),
    }
    let result = get_chapter(&tx, book_id, id)?
        .ok_or(LibraryError::Missing("Chapter not found"))?;
    tx.commit()?;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn db() -> Connection {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch(
            "PRAGMA foreign_keys=ON;\
             CREATE TABLE books(id TEXT PRIMARY KEY);\
             INSERT INTO books(id) VALUES('b');\
             CREATE TABLE book_chapters(\
               book_id TEXT NOT NULL REFERENCES books(id),\
               chapter_id TEXT NOT NULL,chapter_index INTEGER NOT NULL,\
               chapter_path TEXT NOT NULL,source_html TEXT NOT NULL,\
               source_hash TEXT NOT NULL,source_revision INTEGER NOT NULL,\
               accepted_html TEXT,accepted_revision INTEGER NOT NULL DEFAULT 0,\
               accepted_source_revision INTEGER NOT NULL DEFAULT 0,\
               updated_at REAL NOT NULL,\
               PRIMARY KEY(book_id,chapter_id),\
               UNIQUE(book_id,chapter_index),UNIQUE(book_id,chapter_path));",
        ).unwrap();
        db
    }

    fn safe(ch: &str) -> String {
        format!("<p>{}</p>", ch.repeat(300))
    }

    #[test]
    fn safe_unicode_is_raw_hash_and_replays() {
        let mut con = db();
        let html = safe("汉");
        let first = put_chapter_conservative(&mut con,"b","a",1,"a\\b.xhtml",&html,0).unwrap();
        assert_eq!(first["source_revision"],1);
        assert_eq!(first["source_html"],html);
        assert_eq!(first["chapter_path"],"a/b.xhtml");
        assert_eq!(first["source_hash"],hex::encode(Sha256::digest(html.as_bytes())));
        let same = put_chapter_conservative(&mut con,"b","a",1,"a/b.xhtml",&html,1).unwrap();
        assert_eq!(first,same);
        assert!(matches!(
            put_chapter_conservative(&mut con,"b","a",1,"a/b.xhtml",&safe("学"),0),
            Err(LibraryError::Conflict("Chapter source revision mismatch"))
        ));
    }

    #[test]
    fn source_mutation_keeps_old_acceptance_but_marks_stale() {
        let mut con = db();
        let a=safe("林");
        let b=safe("火");
        put_chapter_conservative(&mut con,"b","a",1,"a.xhtml",&a,0).unwrap();
        con.execute(
            "UPDATE book_chapters SET accepted_html='<p>Перевод</p>', \
             accepted_revision=1, accepted_source_revision=1 WHERE chapter_id='a'",[],
        ).unwrap();
        let mutated = put_chapter_conservative(&mut con,"b","a",1,"a.xhtml",&b,1).unwrap();
        assert_eq!(mutated["source_revision"],2);
        assert_eq!(mutated["accepted_revision"],1);
        assert_eq!(mutated["accepted_current"],false);
        assert_eq!(mutated["accepted_html"],"<p>Перевод</p>");
    }

    #[test]
    fn ambiguous_source_is_rejected_without_writing() {
        let mut con=db();
        for html in [
            "<p>Short <strong>记住本站</strong> chapter.</p>".to_owned(),
            safe("甲").replace("甲", "101kanshu甲"),
            format!("<div>{}</div>", "林".repeat(300)),
            format!("<p>{}&amp;</p>", "木".repeat(300)),
        ] {
            assert!(matches!(
                put_chapter_conservative(&mut con,"b","a",1,"a.xhtml",&html,0),
                Err(LibraryError::UnsupportedSource)
            ));
        }
        let count: i64=con.query_row("SELECT COUNT(*) FROM book_chapters",[],|row|row.get(0)).unwrap();
        assert_eq!(count,0);
    }

    #[test]
    fn short_plain_chapter_watermark_hash_matches_cleaned_bytes() {
        let mut db = db();
        let input = "<p>故事开始。</p><p>记住本站</p><p>故事后来继续。</p>";
        let expected_clean = "<p>故事开始。</p><p>故事后来继续。</p>";
        let result = put_chapter_conservative(
            &mut db, "b", "short", 3, "parts/story.xhtml", input, 0,
        ).unwrap();
        assert_eq!(result["source_revision"], 1);
        assert_eq!(result["source_html"], input);
        assert_eq!(
            result["source_hash"],
            hex::encode(Sha256::digest(expected_clean.as_bytes()))
        );
        assert_eq!(
            put_chapter_conservative(
                &mut db, "b", "short", 3, "parts/story.xhtml", input, 1
            ).unwrap(),
            result
        );
    }

    #[test]
    fn nested_inline_writer_preserves_raw_and_hashes_python_cleaned_html() {
        let mut db = db();
        let input = "<p>Привет <strong>друг</strong>, сказал он.</p><p>记住本站</p><p>故事 <em>继续</em>。</p>";
        let cleaned = "<p>Привет <strong>друг</strong>, сказал он.</p><p>故事 <em>继续</em>。</p>";
        let first = put_chapter_conservative(
            &mut db, "b", "inline", 8, "parts/story.xhtml", input, 0,
        ).unwrap();
        assert_eq!(first["source_html"], input);
        assert_eq!(first["source_hash"], hex::encode(Sha256::digest(cleaned.as_bytes())));
        assert_eq!(first["source_revision"], 1);
        let replay = put_chapter_conservative(
            &mut db, "b", "inline", 8, "parts/story.xhtml", input, 1,
        ).unwrap();
        assert_eq!(replay, first);
    }

    #[test]
    fn nested_promotion_and_entities_reject_without_mutation() {
        let mut db = db();
        for (i, html) in [
            "<p>Привет <b>记住本站</b> друг.</p>",
            "<p>До <span>下一章</span> после.</p>",
            "<p>История <em>продолжается</em> &amp; конец.</p>",
            "<p>Он <b class='marked'>прочитал</b> книгу.</p>",
            "<p>Герой <strong>закончил</em> путь.</p>",
        ].iter().enumerate() {
            assert!(matches!(
                put_chapter_conservative(
                    &mut db, "b", &format!("invalid-{i}"), i as i64 + 10,
                    "parts/story.xhtml", html, 0,
                ),
                Err(LibraryError::UnsupportedSource)
            ), "Unexpectedly accepted {html}");
        }
        let count: i64 = db.query_row(
            "SELECT COUNT(*) FROM book_chapters", [], |row| row.get(0),
        ).unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn unsupported_context_and_all_removed_source_never_create_chapter() {
        let mut db = db();
        let short = "<p>Небольшой исходный текст.</p>";
        for (id, index, path, html) in [
            ("numeric", 3, "chapters/7.xhtml", short),
            ("heading", 4, "parts/story.xhtml", "<p>第七章</p>"),
            ("domain", 5, "parts/story.xhtml", "<p>101kanshu.com</p>"),
            ("empty", 6, "parts/story.xhtml", "<p>本章完</p>"),
            ("nested-marker", 7, "parts/story.xhtml", "<p>Привет <b>记住本站</b>.</p>"),
        ] {
            assert!(matches!(
                put_chapter_conservative(
                    &mut db, "b", id, index, path, html, 0
                ),
                Err(LibraryError::UnsupportedSource)
            ), "Unexpectedly accepted {id}");
        }
        let rows: i64 = db.query_row(
            "SELECT COUNT(*) FROM book_chapters", [], |row| row.get(0)
        ).unwrap();
        assert_eq!(rows, 0);
    }

    #[test]
    fn unique_index_and_source_revision_are_atomic() {
        let mut con=db();
        let original=safe("文");
        let other=safe("书");
        put_chapter_conservative(&mut con,"b","a",1,"a.xhtml",&original,0).unwrap();
        assert!(matches!(
            put_chapter_conservative(&mut con,"b","b",1,"b.xhtml",&other,0),
            Err(LibraryError::Conflict("Chapter index or path already belongs to another chapter"))
        ));
        let first = get_chapter(&con,"b","a").unwrap().unwrap();
        assert_eq!(first["source_hash"],Value::from(hex::encode(Sha256::digest(original.as_bytes()))));
        assert!(get_chapter(&con,"b","b").unwrap().is_none());
    }
}
