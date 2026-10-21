//! RUST-M1f: revision-pinned accepted-book export on an existing SQLite.
//!
//! Scope is intentionally conservative. The Python export accepts and sanitizes
//! a broad HTML grammar. Rust accepts only well-formed, attribute-free safe tags,
//! and marks everything else unsafe. It must NEVER silently export a fragment
//! for which exact safety and byte parity are not proved.
use super::LibraryError;
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

const MAX_CHAPTERS: i64 = 2_000;
const MAX_SOURCE_BYTES: usize = 32 * 1024 * 1024;
const MAX_OUTPUT_BYTES: usize = 36 * 1024 * 1024;

fn sha256(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

#[derive(Debug)]
struct Chapter {
    id: String,
    index: i64,
    path: String,
    source_html: String,
    source_hash: String,
    source_revision: i64,
    accepted_revision: i64,
    accepted_source_revision: i64,
    accepted_html: Option<String>,
}

pub struct ExportReport {
    pub preview: Value,
    chapters: Vec<(i64, String, String)>,
}

#[derive(Debug)]
pub enum ExportError {
    Library(LibraryError),
    NotReady(Value),
    Invalid(&'static str),
}
impl From<LibraryError> for ExportError {
    fn from(error: LibraryError) -> Self {
        Self::Library(error)
    }
}

pub struct ExportedFile {
    pub bytes: Vec<u8>,
    pub content_type: &'static str,
    pub extension: &'static str,
    pub manifest_sha256: String,
    pub content_sha256: String,
}

/// Strictly recognize simple "Глава N" / "Chapter N" as the first heading.
/// If it matches, Python BeautifulSoup removes the first heading element.
fn strip_heading(raw: &str, chapter_index: i64) -> &str {
    let input = raw.trim_start();
    for tag in ["h1", "h2", "h3", "p"] {
        let opening = format!("<{tag}>");
        let closing = format!("</{tag}>");
        if !input.starts_with(&opening) {
            continue;
        }
        let Some(offset) = input.find(&closing) else {
            return raw;
        };
        let text = &input[opening.len()..offset];
        if text.contains('<') || text.contains('&') {
            return raw;
        }
        // Match Python's ^(глава|chapter)\s+(\d+)\s*[.:—-]?\s*$:
        // at most ONE terminal punctuation mark, never "Глава 1..".
        let normalized = text.trim().to_lowercase();
        let candidate = normalized.strip_prefix("глава")
            .or_else(|| normalized.strip_prefix("chapter"));
        if let Some(after_prefix) = candidate {
            if !after_prefix.chars().next().is_some_and(char::is_whitespace) {
                return raw;
            }
            let number = after_prefix.trim_start();
            let end_digits = number.find(|ch: char| !ch.is_ascii_digit())
                .unwrap_or(number.len());
            if end_digits == 0 {
                return raw;
            }
            let (digits, tail) = number.split_at(end_digits);
            let suffix = tail.trim();
            if (suffix.is_empty() || matches!(suffix, "." | ":" | "—" | "-"))
                && digits.parse::<i64>().ok() == Some(chapter_index)
            {
                return input[offset + closing.len()..].trim_start();
            }
        }
        return raw;
    }
    raw
}

fn block_tag(name: &str) -> bool {
    matches!(name, "p" | "div" | "blockquote" | "ul" | "ol" |
                    "li" | "h1" | "h2" | "h3")
}

fn allowed_tag(name: &str) -> bool {
    block_tag(name) || matches!(name,
        "span" | "strong" | "b" | "em" | "i" | "u" | "br" | "hr")
}

/// Produce EXACT Python plain-text and sanitized HTML only for a proven
/// attribute-free subset. Unknown HTML, malformed nesting, entities, URLs,
/// editorial/service markup and source markers all fail closed.
fn validated_fragment(raw: &str, chapter_index: i64)
    -> Result<(String, String), &'static str>
{
    let lower = raw.to_ascii_lowercase();
    if lower.contains("tseg_") || lower.contains("tsrc_") {
        return Err("reserved_service_marker");
    }
    if lower.contains("http://") || lower.contains("https://") ||
        lower.contains("www.")
    {
        return Err("external_url_requires_editor_review");
    }
    if lower.contains("<!") || lower.contains("<?") {
        return Err("comment_or_declaration");
    }
    // HTML entity decoding and BeautifulSoup's malformed-tree recovery have
    // not been proved byte-equivalent in Rust. Reject, never treat as text.
    if raw.contains('&') || raw.chars().any(|ch|
        ch.is_control() && ch != '\n' && ch != '\t'
    ) {
        return Err("rust_unsupported_fragment");
    }

    let content = strip_heading(raw.trim(), chapter_index).trim();
    // Python drops the FIRST qualifying heading anywhere in the DOM,
    // including nested wrappers. Rust only strips a canonical leading
    // heading: reject any remaining chapter-heading candidate rather than
    // return a subtly different manuscript or manifest-compatible bytes.
    let lowered_content = content.to_lowercase();
    if ["<p>глава", "<h1>глава", "<h2>глава", "<h3>глава",
        "<p>chapter", "<h1>chapter", "<h2>chapter", "<h3>chapter"]
        .iter().any(|head| lowered_content.contains(head))
    {
        return Err("rust_unsupported_fragment");
    }
    if content.is_empty() {
        return Err("empty_after_heading");
    }
    let mut stack: Vec<&str> = Vec::new();
    let mut plain = String::new();
    let mut position = 0usize;
    let mut text_found = false;
    while position < content.len() {
        if content.as_bytes()[position] == b'<' {
            let offset = content[position..].find('>')
                .ok_or("rust_unsupported_fragment")?;
            let end = position + offset;
            let token = &content[position + 1..end];
            let closing = token.starts_with('/');
            let unclosed = token.strip_prefix('/').unwrap_or(token);
            let void_slash = unclosed.ends_with('/');
            let name = unclosed.trim_end_matches('/');
            if name.is_empty() || name.trim() != name {
                return Err("rust_unsupported_fragment");
            }
            if !allowed_tag(name) {
                // Attributes on allowed tags are conservatively unsupported;
                // executable or unknown tags are explicitly unsafe.
                if allowed_tag(name.split_whitespace().next().unwrap_or("")) {
                    return Err("rust_unsupported_fragment");
                }
                return Err("unsupported_tag");
            }
            if name == "br" || name == "hr" {
                // BeautifulSoup serializes HTML voids as <br/> and <hr/>.
                // Refuse a spelling we cannot preserve byte-for-byte.
                if !void_slash {
                    return Err("rust_unsupported_fragment");
                }
                if closing {
                    return Err("rust_unsupported_fragment");
                }
                if name == "br" {
                    plain.push('\n');
                }
            } else if closing {
                if void_slash || stack.pop() != Some(name) {
                    return Err("rust_unsupported_fragment");
                }
                if block_tag(name) {
                    plain.push('\n');
                }
            } else {
                if void_slash {
                    return Err("rust_unsupported_fragment");
                }
                // Disallow block tags nested within paragraphs/headings or
                // inline tags. Browsers/HTML parsers can normalize invalid
                // nesting differently; that grammar is not in the oracle.
                if block_tag(name) && stack.iter().any(|parent| {
                    matches!(*parent, "p" | "h1" | "h2" | "h3" |
                        "strong" | "b" | "em" | "i" | "u" | "span")
                }) {
                    return Err("rust_unsupported_fragment");
                }
                if block_tag(name) {
                    plain.push('\n');
                }
                stack.push(name);
            }
            position = end + 1;
        } else {
            let following = content[position..]
                .find('<').map_or(content.len(), |i| position + i);
            let value = &content[position..following];
            // BeautifulSoup normalizes whitespace-only siblings between
            // top-level nodes (for example " \n" to "\n"). Until this is
            // ported exactly, do not copy raw whitespace into an HTML export.
            if stack.is_empty() && !value.is_empty() {
                return Err("rust_unsupported_fragment");
            }
            if value.contains('>') || value.contains('\u{a0}') {
                return Err("rust_unsupported_fragment");
            }
            if !value.trim().is_empty() {
                text_found = true;
            }
            plain.push_str(value);
            position = following;
        }
    }
    if !stack.is_empty() {
        return Err("rust_unsupported_fragment");
    }
    if !text_found {
        return Err("empty_after_heading");
    }

    // Equivalent to Python's [ \t]+\n, \n[ \t]+, \n{3,} and strip()
    // for the conservatively admitted character/tag subset.
    let replaced = plain.replace('\u{a0}', " ");
    let joined = replaced.split('\n')
        .map(|line| line.trim_matches(|ch| ch == ' ' || ch == '\t'))
        .collect::<Vec<_>>().join("\n");
    let mut normalized = joined;
    while normalized.contains("\n\n\n") {
        normalized = normalized.replace("\n\n\n", "\n\n");
    }
    let normalized = normalized.trim().to_owned();
    if normalized.is_empty() {
        return Err("empty_text");
    }
    Ok((normalized, content.to_owned()))
}

/// Read an immutable-in-memory snapshot. The book and chapter rows are read
/// in a single SQLite read transaction, never from unrelated requests.
pub fn inspect_export(
    db: &Connection, book_id: &str, expected_last: i64,
) -> Result<ExportReport, LibraryError> {
    if !(1..=MAX_CHAPTERS).contains(&expected_last) {
        return Err(LibraryError::Invalid(
            "Declared last chapter must be between 1 and 2000",
        ));
    }

    db.execute_batch("BEGIN")?;
    let (title, glossary_revision): (String, i64) = db
        .query_row(
            "SELECT title,glossary_revision FROM books WHERE id=?1",
            params![book_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?
        .ok_or(LibraryError::Missing("Book not found"))?;

    let mut chapters = Vec::new();
    let mut total_bytes = 0usize;
    let mut over_limit = false;
    {
        let mut statement = db.prepare(
            "SELECT chapter_id,chapter_index,chapter_path,source_html, \
             source_hash,source_revision,accepted_revision, \
             accepted_source_revision,accepted_html \
             FROM book_chapters WHERE book_id=?1 \
             ORDER BY chapter_index,chapter_id",
        )?;
        let mut rows = statement.query(params![book_id])?;
        while let Some(row) = rows.next()? {
            let chapter = Chapter {
                id: row.get(0)?,
                index: row.get(1)?,
                path: row.get(2)?,
                source_html: row.get(3)?,
                source_hash: row.get(4)?,
                source_revision: row.get(5)?,
                accepted_revision: row.get(6)?,
                accepted_source_revision: row.get(7)?,
                accepted_html: row.get(8)?,
            };
            total_bytes += chapter.source_html.len() +
                chapter.accepted_html.as_ref().map_or(0, String::len);
            if total_bytes > MAX_SOURCE_BYTES || chapters.len() >= MAX_CHAPTERS as usize {
                over_limit = true;
                break;
            }
            chapters.push(chapter);
        }
    }
    db.execute_batch("COMMIT")?;

    let mut issues: Vec<Value> = Vec::new();
    if over_limit {
        issues.push(json!({"code": "export_size_or_count_limit"}));
    }
    let by_index: BTreeMap<i64, &Chapter> = chapters.iter()
        .map(|chapter| (chapter.index, chapter)).collect();
    let mut accepted = Vec::new();
    let mut manifest_rows = Vec::new();

    for index in 1..=expected_last {
        let Some(chapter) = by_index.get(&index) else {
            issues.push(json!({"code": "missing_source", "chapter_index": index}));
            continue;
        };
        let Some(raw) = chapter.accepted_html.as_deref() else {
            issues.push(json!({"code": "not_accepted", "chapter_index": index}));
            continue;
        };
        if raw.is_empty() || chapter.accepted_revision <= 0 {
            issues.push(json!({"code": "not_accepted", "chapter_index": index}));
            continue;
        }
        if chapter.accepted_source_revision != chapter.source_revision {
            issues.push(json!({"code": "stale_accepted", "chapter_index": index}));
            continue;
        }
        let (plain, safe_html) = match validated_fragment(raw, index) {
            Ok(result) => result,
            Err(reason) => {
                issues.push(json!({
                    "code": "unsafe_accepted_html",
                    "chapter_index": index,
                    "reason": reason,
                }));
                continue;
            }
        };
        accepted.push((index, plain, safe_html));
        manifest_rows.push(json!({
            "chapter_id": chapter.id,
            "chapter_index": index,
            "chapter_path": chapter.path,
            "source_revision": chapter.source_revision,
            "source_sha256": chapter.source_hash,
            "source_blob_sha256": sha256(chapter.source_html.as_bytes()),
            "accepted_revision": chapter.accepted_revision,
            "accepted_sha256": sha256(raw.as_bytes()),
        }));
    }
    for chapter in &chapters {
        if chapter.index > expected_last {
            issues.push(json!({
                "code": "source_beyond_declared_end",
                "chapter_index": chapter.index,
            }));
        }
    }

    // serde_json's default Map is sorted (BTreeMap), matching Python's
    // json.dumps(ensure_ascii=False, sort_keys=True, separators=(",",":")).
    let manifest = json!({
        "schema": "readera-accepted-export-v1",
        "book_id": book_id,
        "title": title,
        "expected_last_chapter": expected_last,
        "glossary_revision": glossary_revision,
        "chapters": manifest_rows,
    });
    let digest = if issues.is_empty() {
        let data = serde_json::to_vec(&manifest).map_err(|_| LibraryError::Malformed)?;
        Some(sha256(&data))
    } else {
        None
    };
    let issue_count = issues.len();
    issues.truncate(100);
    let preview = json!({
        "book_id": book_id,
        "title": title,
        "expected_last_chapter": expected_last,
        "source_chapter_count": chapters.len(),
        "ready_chapter_count": accepted.len(),
        "ready": issue_count == 0,
        "issues": issues,
        "issue_count": issue_count,
        "manifest_sha256": digest,
    });
    Ok(ExportReport { preview, chapters: accepted })
}

fn escape_title(title: &str) -> String {
    let mut escaped = String::new();
    for c in title.chars() {
        match c {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\'' => escaped.push_str("&#x27;"),
            _ => escaped.push(c),
        }
    }
    escaped
}

/// Recomputes every chapter from one snapshot on each download request.
/// Missing, stale, unsupported or modified content never yields partial bytes.
pub fn render_export(
    db: &Connection, book_id: &str, expected_last: i64,
    expected_manifest: &str, format: &str,
) -> Result<ExportedFile, ExportError> {
    if !matches!(format, "txt" | "html") {
        return Err(ExportError::Invalid("Unsupported export format"));
    }
    let report = inspect_export(db, book_id, expected_last)?;
    if report.preview["ready"] != true ||
        report.preview["manifest_sha256"].as_str() != Some(expected_manifest)
    {
        return Err(ExportError::NotReady(report.preview));
    }
    let (body, content_type, extension) = if format == "txt" {
        let chunks = report.chapters.iter()
            .map(|(index, text, _)| format!("Глава {index}\n\n{text}"))
            .collect::<Vec<_>>();
        (format!("{}\n", chunks.join("\n\n")),
         "text/plain; charset=utf-8", "txt")
    } else {
        let chunks = report.chapters.iter()
            .map(|(index, _, html)| format!(
                "<section data-chapter-index=\"{index}\"><h2>Глава {index}</h2>{html}</section>"
            ))
            .collect::<Vec<_>>();
        let body = format!(
            "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">\
             <title>{}</title></head><body><main>{}</main></body></html>",
            escape_title(report.preview["title"].as_str().unwrap_or("")),
            chunks.join("\n"),
        );
        (body, "text/html; charset=utf-8", "html")
    };
    let bytes = body.into_bytes();
    if bytes.len() > MAX_OUTPUT_BYTES {
        let mut invalid_preview = report.preview;
        invalid_preview["ready"] = json!(false);
        invalid_preview["manifest_sha256"] = Value::Null;
        invalid_preview["issues"] = json!([{"code": "export_output_too_large"}]);
        invalid_preview["issue_count"] = json!(1);
        return Err(ExportError::NotReady(invalid_preview));
    }
    Ok(ExportedFile {
        content_sha256: sha256(&bytes),
        manifest_sha256: expected_manifest.to_owned(),
        bytes,
        content_type,
        extension,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch(
            "CREATE TABLE books(id TEXT PRIMARY KEY,title TEXT NOT NULL,\
             glossary_revision INTEGER NOT NULL);\
             CREATE TABLE book_chapters(\
             book_id TEXT NOT NULL,chapter_id TEXT NOT NULL,chapter_index INTEGER NOT NULL,\
             chapter_path TEXT NOT NULL,source_html TEXT NOT NULL,source_hash TEXT NOT NULL,\
             source_revision INTEGER NOT NULL,accepted_revision INTEGER NOT NULL,\
             accepted_source_revision INTEGER NOT NULL,accepted_html TEXT);\
             INSERT INTO books VALUES('b','Книга <тест>',2);\
             INSERT INTO book_chapters VALUES\
             ('b','first',1,'a.xhtml','<p>source A</p>','shaA',1,1,1,'<p>Привет мир.</p>'),\
             ('b','second',2,'b.xhtml','<p>source B</p>','shaB',1,1,1,'<p>Второй текст.</p>');",
        ).unwrap();
        db
    }

    #[test]
    fn exact_preview_and_both_formats_are_pinned() {
        let db = fixture();
        let report = inspect_export(&db, "b", 2).unwrap();
        assert_eq!(report.preview["ready"], true);
        assert_eq!(report.preview["ready_chapter_count"], 2);
        let receipt = report.preview["manifest_sha256"].as_str().unwrap();
        assert_eq!(receipt.len(), 64);
        let txt = render_export(&db, "b", 2, receipt, "txt").unwrap();
        assert!(String::from_utf8(txt.bytes).unwrap().contains("Глава 2\n\nВторой текст."));
        let html = render_export(&db, "b", 2, receipt, "html").unwrap();
        assert!(String::from_utf8(html.bytes).unwrap().contains("<title>Книга &lt;тест&gt;</title>"));
        assert!(matches!(
            render_export(&db, "b", 2, &"0".repeat(64), "txt"),
            Err(ExportError::NotReady(_))
        ));
    }

    #[test]
    fn no_missing_chapter_or_stale_source_can_leak_export_bytes() {
        let db = fixture();
        assert!(inspect_export(&db, "b", 3).unwrap().preview["issues"]
            .as_array().unwrap().iter().any(|row| row["code"] == "missing_source"));
        let receipt = inspect_export(&db, "b", 2).unwrap()
            .preview["manifest_sha256"].as_str().unwrap().to_owned();
        db.execute("UPDATE book_chapters SET source_revision=2 WHERE chapter_index=2", [])
            .unwrap();
        assert_eq!(inspect_export(&db, "b", 2).unwrap().preview["ready"], false);
        assert!(matches!(render_export(&db, "b", 2, &receipt, "txt"),
            Err(ExportError::NotReady(_))));
    }

    #[test]
    fn strict_fragment_grammar_refuses_unknowns_and_removes_first_heading() {
        let (plain, html) = validated_fragment(
            "<h1>Глава 1</h1><p>Привет <strong>мир</strong>.</p>", 1,
        ).unwrap();
        assert_eq!(plain, "Привет мир.");
        assert_eq!(html, "<p>Привет <strong>мир</strong>.</p>");
        for dangerous in [
            "<p>Текст.</p> \n<p>Второй абзац.</p>",
            "<p><p>Неверное вложение.</p></p>",
            "<h1>Глава 1..</h1><p>Текст.</p>",
            "<div><p>Глава 1</p><p>Текст главы.</p></div>",
            "<p>Текст.</p><script>alert(1)</script>",
            "<p onclick=\"evil()\">Текст.</p>",
            "<p>Текст TSEG_12345</p>",
            "<p>Текст &amp; второй текст.</p>",
            "<p>Посетите https://test.example</p>",
        ] {
            assert!(validated_fragment(dangerous, 1).is_err(), "{dangerous}");
        }
    }
}
