//! M1n: proven, read-only UTF-8 TXT preview subset of Python book_import.
//!
//! No staging, file extraction, filesystem access, network calls or DB writes.
//! Reject unproved heading grammars rather than misclassify/renumber chapters.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

const MAX_UPLOAD: usize = 12 * 1024 * 1024;
const MAX_CHAPTERS: usize = 2_000;
const MAX_CHARS: usize = 2_000_000;

#[derive(Debug, PartialEq, Eq)]
pub enum PreviewError {
    Invalid(&'static str),
    TooLarge,
}

fn sha(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

fn char_prefix(value: &str, count: usize) -> String {
    value.chars().take(count).collect()
}

/// Only heading-with-number and no suffix is proved against Python here.
/// "Глава 3. Название", CJK forms, Markdown headings and service entries
/// deliberately remain Python-only until their full classification is ported.
fn numbered_heading(line: &str) -> Option<i64> {
    let lower = line.to_lowercase();
    let tail = ["глава ", "chapter "].iter()
        .find_map(|prefix| lower.strip_prefix(prefix))?;
    if tail.is_empty() || tail.len() > 7 || !tail.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let number = tail.parse::<i64>().ok()?;
    (1..=9_999_999).contains(&number).then_some(number)
}

fn potentially_unproved_heading(line: &str) -> bool {
    let lower = line.to_lowercase();
    let trimmed = lower.trim_start();
    trimmed.starts_with("глава")
        || trimmed.starts_with("chapter")
        || trimmed.starts_with('#')
        || trimmed.starts_with('第')
        || trimmed.starts_with("番外")
        || trimmed.starts_with("上架感言")
        || trimmed.starts_with("请假")
        || trimmed.starts_with("新书已发")
}

fn plain_text(lines: &[String]) -> String {
    // Python _paragraphs_to_html escapes every line then BeautifulSoup
    // get_text(" ", strip=True) inserts separators between text nodes.
    // In this grammar every source line is plain text, not an HTML fragment.
    lines.iter().map(|s| s.trim()).filter(|s| !s.is_empty())
        .collect::<Vec<_>>().join(" ")
}

struct Entry {
    title: String,
    number: Option<i64>,
    lines: Vec<String>,
}

fn push_entry(
    entries: &mut Vec<Entry>,
    title: String,
    number: Option<i64>,
    lines: Vec<String>,
) -> Result<(), PreviewError> {
    if entries.len() >= MAX_CHAPTERS {
        return Err(PreviewError::Invalid("Book has no chapters or exceeds the chapter limit"));
    }
    entries.push(Entry { title, number, lines });
    Ok(())
}

pub fn preview_txt(filename: &str, data: &[u8]) -> Result<Value, PreviewError> {
    if data.is_empty() {
        return Err(PreviewError::Invalid("Uploaded file is empty"));
    }
    if data.len() > MAX_UPLOAD {
        return Err(PreviewError::TooLarge);
    }
    // Preserve Python's normalized basename for a bounded, unambiguous subset.
    if filename.trim() != filename || filename.is_empty()
        || filename.chars().count() > 255
        || filename.contains('/') || filename.contains('\\')
        || filename.chars().any(|c| c.is_control())
        || !filename.to_ascii_lowercase().ends_with(".txt")
        || filename.len() <= 4
    {
        return Err(PreviewError::Invalid("Unsupported filename or import format"));
    }
    let title: String = filename.chars().take(filename.chars().count() - 4).collect();
    if title.is_empty() {
        return Err(PreviewError::Invalid("Unsupported filename or import format"));
    }

    // Unlike Python, this slice never guesses a legacy code page or parses
    // UTF-16: those remain unsupported until byte-exact parity is established.
    let decoded = std::str::from_utf8(data)
        .map_err(|_| PreviewError::Invalid("Unsupported TXT encoding in Rust preview"))?;
    let decoded = decoded.strip_prefix('\u{feff}').unwrap_or(decoded);
    if decoded.contains('\0') || decoded.contains('\r') && !decoded.contains("\r\n")
        || decoded.chars().any(|c| matches!(c, '\u{0085}' | '\u{2028}' | '\u{2029}'))
    {
        return Err(PreviewError::Invalid("Unproved TXT line boundaries"));
    }
    let decoded = decoded.replace("\r\n", "\n");
    if decoded.contains('\r') {
        return Err(PreviewError::Invalid("Unproved TXT line boundaries"));
    }

    let mut entries: Vec<Entry> = Vec::new();
    let mut body: Vec<String> = Vec::new();
    let mut current_title: Option<String> = None;
    let mut current_number: Option<i64> = None;
    let mut prefix: Vec<String> = Vec::new();

    for raw in decoded.lines() {
        let line = raw.trim();
        if let Some(number) = numbered_heading(line) {
            if let Some(previous) = current_title.take() {
                push_entry(&mut entries, previous, current_number, std::mem::take(&mut body))?;
            } else {
                prefix = std::mem::take(&mut body);
            }
            current_title = Some(line.to_owned());
            current_number = Some(number);
        } else {
            if potentially_unproved_heading(line) {
                return Err(PreviewError::Invalid("Unproved TXT chapter heading"));
            }
            body.push(raw.to_owned());
        }
    }

    if let Some(last_title) = current_title {
        push_entry(&mut entries, last_title, current_number, body)?;
    } else {
        push_entry(&mut entries, title.clone(), None, body)?;
    }
    if entries.is_empty() {
        return Err(PreviewError::Invalid("Book has no chapters or exceeds the chapter limit"));
    }

    let mut chapters = Vec::with_capacity(entries.len());
    let mut warnings: Vec<String> = Vec::new();
    let mut seen_numbers = BTreeSet::new();
    let mut previous_bodies: BTreeMap<String, usize> = BTreeMap::new();
    let mut duplicated_bodies = Vec::new();

    for (index, entry) in entries.iter().enumerate() {
        let sequence = index + 1;
        let content = plain_text(&entry.lines);
        let characters = content.chars().count();
        if characters > MAX_CHARS {
            return Err(PreviewError::Invalid("Chapter exceeds per-chapter limit"));
        }
        if content.is_empty() {
            warnings.push(format!("Entry {sequence} has no readable body text"));
        }
        if let Some(number) = entry.number {
            if !seen_numbers.insert(number) {
                warnings.push(format!(
                    "Duplicate chapter number {number}; manual mapping required"
                ));
            }
        }
        let digest = sha(&content);
        if !content.is_empty() {
            if let Some(prior) = previous_bodies.get(&digest) {
                if duplicated_bodies.len() < 32 {
                    duplicated_bodies.push(json!({
                        "first_sequence": prior, "duplicate_sequence": sequence
                    }));
                }
            } else {
                previous_bodies.insert(digest.clone(), sequence);
            }
        }
        chapters.push(json!({
            "sequence": sequence, "catalog_ordinal": sequence,
            "detected_number": entry.number, "author_number": entry.number,
            "entry_type": "chapter",
            "suggested_index": entry.number.unwrap_or(sequence as i64),
            "title": char_prefix(&entry.title, 500),
            "source_path": char_prefix(filename, 1024),
            "characters": characters, "sha256": digest,
            "sample": char_prefix(&content, 180), "has_text": !content.is_empty(),
        }));
    }
    let numbers: Vec<i64> = seen_numbers.into_iter().collect();
    let gaps: Vec<Value> = numbers.windows(2)
        .filter(|pair| pair[1] > pair[0] + 1)
        .map(|pair| json!([pair[0] + 1, pair[1] - 1])).collect();

    if entries.iter().any(|entry| entry.number.is_none()) {
        warnings.push(
            "Some entries have no explicit author number: sequence numbers are suggestions only".to_owned()
        );
    }
    if !gaps.is_empty() {
        warnings.push("Internal gaps in observed author chapter numbers; verify source coverage".to_owned());
    }
    if !duplicated_bodies.is_empty() {
        warnings.push("Duplicate story bodies detected by SHA-256; review before import".to_owned());
    }
    let preface = plain_text(&prefix);
    if !preface.is_empty() {
        warnings.push("Text before the first chapter heading is unassigned and needs review".to_owned());
    }

    Ok(json!({
        "kind": "book", "format": "txt", "filename": filename,
        "title": char_prefix(&title, 500), "chapter_count": chapters.len(),
        "chapters": chapters, "story_chapter_count": chapters.len(),
        "extra_entry_count": 0, "service_entry_count": 0,
        "numbering_gaps": gaps, "duplicate_body_pairs": duplicated_bodies,
        "unassigned_prefix_characters": preface.chars().count(),
        "unassigned_prefix_sample": char_prefix(&preface, 180),
        "warnings": warnings, "stored": false,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn two_russian_chapters_and_unassigned_prefix_are_visible() {
        let data = "Предисловие.\n\nГлава 1\nПервая строка.\nВторая строка.\n\nГлава 3\nКонец.";
        let preview = preview_txt("Роман.txt", data.as_bytes()).unwrap();
        assert_eq!(preview["chapter_count"], 2);
        assert_eq!(preview["chapters"][0]["detected_number"], 1);
        assert_eq!(preview["chapters"][0]["sample"], "Первая строка. Вторая строка.");
        assert_eq!(preview["chapters"][1]["detected_number"], 3);
        assert_eq!(preview["numbering_gaps"], json!([[2, 2]]));
        assert!(preview["unassigned_prefix_characters"].as_u64().unwrap() > 0);
        assert_eq!(preview["stored"], false);
    }
    #[test]
    fn an_unnumbered_file_is_one_chapter() {
        let preview = preview_txt("untitled.txt", b"First paragraph\n\nSecond paragraph").unwrap();
        assert_eq!(preview["chapters"][0]["sample"], "First paragraph Second paragraph");
        assert!(preview["chapters"][0]["detected_number"].is_null());
    }
    #[test]
    fn duplicate_numbers_and_bodies_are_not_silently_reindexed() {
        let preview = preview_txt("dup.txt", "Глава 1\nОдин\nГлава 1\nОдин".as_bytes()).unwrap();
        assert_eq!(preview["chapters"][1]["suggested_index"], 1);
        assert_eq!(preview["duplicate_body_pairs"][0]["first_sequence"], 1);
        assert_eq!(preview["duplicate_body_pairs"][0]["duplicate_sequence"], 2);
        assert!(preview["warnings"].as_array().unwrap().iter()
            .any(|v| v.as_str().unwrap().contains("Duplicate chapter number 1")));
    }
    #[test]
    fn unknown_heading_types_and_unsafe_formats_are_rejected() {
        for input in ["Глава 1. Название\nТекст", "# Глава 1\nТекст",
                      "第一章\n文本", "Chapter 1: Title\nText",
                      "Глава 1\nA\nChapter 2 - finale\nB"] {
            assert_eq!(preview_txt("book.txt", input.as_bytes()).unwrap_err(),
                       PreviewError::Invalid("Unproved TXT chapter heading"));
        }
        assert!(matches!(preview_txt("book.epub", b"contents"), Err(PreviewError::Invalid(_))));
        assert_eq!(preview_txt("book.txt", &[0xff, 0xfe, 0x00, 0x00]).unwrap_err(),
                   PreviewError::Invalid("Unsupported TXT encoding in Rust preview"));
    }
}
