//! M2f-1: read-only, manifest-owned Full Cycle App source ZIP verifier.
//!
//! No site crawler, chapter-number inference, SQLite, external requests,
//! credentials or paid API calls. Python remains the production ZIP importer.
//! This crate verifies *every referenced original and working byte* before
//! returning even a preview; a damaged member invalidates the whole archive.
use serde::de::{self, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::{json, Map, Number, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fmt;
use std::io::{Cursor, Read};
use url::Url;
use zip::{CompressionMethod, ZipArchive};

pub const MAX_UPLOAD: usize = 24 * 1024 * 1024;
const MAX_EXPANDED: u64 = 64 * 1024 * 1024;
const MAX_MANIFEST: usize = 2 * 1024 * 1024;
const MAX_MEMBER: usize = 10 * 1024 * 1024;
const MAX_CHAPTERS: usize = 2000;

#[derive(Debug)]
pub struct ArchiveError(&'static str);
impl fmt::Display for ArchiveError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for ArchiveError {}
type Result<T> = std::result::Result<T, ArchiveError>;
fn invalid() -> ArchiveError {
    ArchiveError("Invalid or unsupported Full Cycle App source ZIP")
}

/// Unlike Value's default JSON parser, reject duplicate keys at *any* depth.
/// No attacker may override a verified manifest path, digest or chapter index.
struct UniqueJson(Value);
impl<'de> Deserialize<'de> for UniqueJson {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct UniqueVisitor;
        impl<'de> Visitor<'de> for UniqueVisitor {
            type Value = UniqueJson;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("JSON without duplicate object keys")
            }
            fn visit_bool<E: de::Error>(self, b: bool) -> std::result::Result<Self::Value, E> {
                Ok(UniqueJson(Value::Bool(b)))
            }
            fn visit_i64<E: de::Error>(self, n: i64) -> std::result::Result<Self::Value, E> {
                Ok(UniqueJson(Value::Number(n.into())))
            }
            fn visit_u64<E: de::Error>(self, n: u64) -> std::result::Result<Self::Value, E> {
                Ok(UniqueJson(Value::Number(n.into())))
            }
            fn visit_f64<E: de::Error>(self, n: f64) -> std::result::Result<Self::Value, E> {
                Ok(UniqueJson(Value::Number(Number::from_f64(n).ok_or_else(
                    || E::custom("nonfinite JSON number")
                )?)))
            }
            fn visit_str<E: de::Error>(self, s: &str) -> std::result::Result<Self::Value, E> {
                Ok(UniqueJson(Value::String(s.to_owned())))
            }
            fn visit_string<E: de::Error>(self, s: String) -> std::result::Result<Self::Value, E> {
                Ok(UniqueJson(Value::String(s)))
            }
            fn visit_unit<E: de::Error>(self) -> std::result::Result<Self::Value, E> {
                Ok(UniqueJson(Value::Null))
            }
            fn visit_none<E: de::Error>(self) -> std::result::Result<Self::Value, E> {
                Ok(UniqueJson(Value::Null))
            }
            fn visit_some<D: Deserializer<'de>>(
                self, d: D
            ) -> std::result::Result<Self::Value, D::Error> {
                UniqueJson::deserialize(d)
            }
            fn visit_seq<S: SeqAccess<'de>>(
                self, mut seq: S
            ) -> std::result::Result<Self::Value, S::Error> {
                let mut values = Vec::new();
                while let Some(value) = seq.next_element::<UniqueJson>()? {
                    values.push(value.0);
                }
                Ok(UniqueJson(Value::Array(values)))
            }
            fn visit_map<M: MapAccess<'de>>(
                self, mut map: M
            ) -> std::result::Result<Self::Value, M::Error> {
                let mut values = Map::new();
                while let Some((key, value)) = map.next_entry::<String, UniqueJson>()? {
                    if values.contains_key(&key) {
                        return Err(de::Error::custom("duplicate JSON object key"));
                    }
                    values.insert(key, value.0);
                }
                Ok(UniqueJson(Value::Object(values)))
            }
        }
        deserializer.deserialize_any(UniqueVisitor)
    }
}

fn text_field(row: &Value, name: &str, max_chars: usize) -> Result<String> {
    let Some(value) = row.get(name) else {
        return Ok(String::new());
    };
    let s = value.as_str().ok_or_else(invalid)?;
    if s.chars().count() > max_chars || s.chars().any(|c| (c as u32) < 32) {
        return Err(invalid());
    }
    Ok(s.to_owned())
}

fn http_url(row: &Value, name: &str) -> Result<String> {
    let value = text_field(row, name, 2048)?;
    if value.is_empty() {
        return Ok(value);
    }
    let url = Url::parse(&value).map_err(|_| invalid())?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none()
        || !url.username().is_empty() || url.password().is_some()
    {
        return Err(invalid());
    }
    // Empty explicit user info (@host) is also disallowed by Python urlsplit.
    let authority = value.split_once("://").ok_or_else(invalid)?.1
        .split(['/', '?', '#']).next().unwrap_or("");
    if authority.contains('@') || authority.is_empty() || authority.contains('\\') {
        return Err(invalid());
    }
    // Preserve the exact original link bytes; Url canonicalization would
    // change manifest provenance even when the link is otherwise valid.
    Ok(value)
}

fn sha_field<'a>(row: &'a Value, name: &str) -> Result<&'a str> {
    let s = row.get(name).and_then(Value::as_str).ok_or_else(invalid)?;
    if s.len() != 64 || !s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
        return Err(invalid());
    }
    Ok(s)
}

fn allowed_file(name: &str) -> bool {
    if matches!(name, "manifest.json" | "README.txt") {
        return true;
    }
    let suffix = name.strip_prefix("chapters/")
        .or_else(|| name.strip_prefix("original/"));
    let Some(digits) = suffix.and_then(|s| s.strip_suffix(".txt")) else {
        return false;
    };
    (6..=10).contains(&digits.len()) && digits.bytes().all(|b| b.is_ascii_digit())
}

fn validate_zip(archive: &mut ZipArchive<Cursor<&[u8]>>) -> Result<HashSet<String>> {
    if archive.len() > 2 * MAX_CHAPTERS + 4 {
        return Err(invalid());
    }
    let mut names = HashSet::new();
    let mut total: u64 = 0;
    for i in 0..archive.len() {
        let file = archive.by_index(i).map_err(|_| invalid())?;
        let name = file.name();
        if !names.insert(name.to_owned())
            || name.is_empty() || name.starts_with('/') || name.contains('\\')
            || name.contains('\0')
            || name.trim_end_matches('/').split('/').any(|part| part.is_empty() || part == "." || part == "..")
        {
            return Err(invalid());
        }
        // Unix mode 0 can mean no type recorded; regular and directory only.
        if let Some(mode) = file.unix_mode() {
            if !matches!(mode & 0o170000, 0 | 0o100000 | 0o040000) {
                return Err(invalid());
            }
        }
        if !matches!(file.compression(), CompressionMethod::Stored | CompressionMethod::Deflated) {
            return Err(invalid());
        }
        if file.is_dir() {
            if !matches!(name, "chapters/" | "original/") {
                return Err(invalid());
            }
            continue;
        }
        if !allowed_file(name) {
            return Err(invalid());
        }
        let limit = if name == "manifest.json" {
            MAX_MANIFEST as u64
        } else if name == "README.txt" {
            1024 * 1024
        } else {
            MAX_MEMBER as u64
        };
        total = total.checked_add(file.size()).ok_or_else(invalid)?;
        let compressed_limit = file.compressed_size().max(1024).saturating_mul(250);
        if file.size() > limit || total > MAX_EXPANDED || file.size() > compressed_limit {
            return Err(invalid());
        }
    }
    if !names.contains("manifest.json") {
        return Err(invalid());
    }
    Ok(names)
}

fn read_member(
    archive: &mut ZipArchive<Cursor<&[u8]>>, name: &str, limit: usize
) -> Result<Vec<u8>> {
    let mut file = archive.by_name(name).map_err(|_| invalid())?;
    if file.size() > limit as u64 {
        return Err(invalid());
    }
    let expected = usize::try_from(file.size()).map_err(|_| invalid())?;
    let mut result = Vec::new();
    file.by_ref().take(limit as u64 + 1)
        .read_to_end(&mut result).map_err(|_| invalid())?;
    // Reading through EOF forces the ZIP implementation's CRC verification.
    if result.len() != expected || result.len() > limit {
        return Err(invalid());
    }
    Ok(result)
}

/// All outputs are verified and independent of the ZIP reader's borrow.
/// The structure intentionally mirrors Python load_full_cycle_source().
pub fn load_source(data: &[u8], fallback_title: &str) -> Result<Value> {
    if data.is_empty() || data.len() > MAX_UPLOAD {
        return Err(invalid());
    }
    let mut archive = ZipArchive::new(Cursor::new(data)).map_err(|_| invalid())?;
    let names = validate_zip(&mut archive)?;
    let manifest_bytes = read_member(&mut archive, "manifest.json", MAX_MANIFEST)?;
    let manifest = serde_json::from_slice::<UniqueJson>(&manifest_bytes)
        .map_err(|_| invalid())?.0;
    if manifest["format"] != "full_cycle_app.chapter_exchange"
        || manifest["version"].as_u64() != Some(1)
        || manifest["kind"] != "source"
    {
        return Err(invalid());
    }
    let chapters = manifest.get("chapters").and_then(Value::as_array).ok_or_else(invalid)?;
    if chapters.is_empty() || chapters.len() > MAX_CHAPTERS
        || manifest["chapter_count"].as_u64() != Some(chapters.len() as u64)
    {
        return Err(invalid());
    }
    let project = manifest.get("project").filter(|x| x.is_object()).ok_or_else(invalid)?;
    let source = manifest.get("source").filter(|x| x.is_object()).ok_or_else(invalid)?;
    let title = text_field(project, "title", 500)?;
    let title = if title.trim().is_empty() {
        fallback_title.to_owned()
    } else {
        title.trim().to_owned()
    };
    let source_url = http_url(source, "source_url")?;
    let source_domain = text_field(source, "domain", 255)?;
    let external_book_id = text_field(source, "external_book_id", 255)?;
    let mut used_indices = HashSet::new();
    let mut used_files: HashSet<String> = ["manifest.json", "README.txt"]
        .into_iter().map(str::to_owned).collect();
    let mut first_urls: HashMap<String, u64> = HashMap::new();
    let mut url_pairs = Vec::new();
    let mut duplicate_url_count: u64 = 0;
    let mut previous_index = 0;
    let mut nonmonotonic = false;
    let mut parsed = Vec::with_capacity(chapters.len());
    for row in chapters {
        if !row.is_object() {
            return Err(invalid());
        }
        let index = row.get("chapter_index").and_then(Value::as_u64).ok_or_else(invalid)?;
        if !(1..=10_000_000).contains(&index) || !used_indices.insert(index) {
            return Err(invalid());
        }
        let raw_title = text_field(row, "chapter_title", 500)?;
        let chapter_title = if raw_title.trim().is_empty() {
            format!("Без названия (позиция {index})")
        } else {
            raw_title.trim().to_owned()
        };
        let chapter_url = http_url(row, "chapter_url")?;
        if !chapter_url.is_empty() {
            if let Some(&first) = first_urls.get(&chapter_url) {
                duplicate_url_count += 1;
                if url_pairs.len() < 32 {
                    url_pairs.push(json!([first, index]));
                }
            } else {
                first_urls.insert(chapter_url.clone(), index);
            }
        }
        if previous_index > 0 && index < previous_index {
            nonmonotonic = true;
        }
        previous_index = index;
        let working_file = format!("chapters/{index:06}.txt");
        let original_file = format!("original/{index:06}.txt");
        if row.get("working_file").and_then(Value::as_str) != Some(working_file.as_str())
            || row.get("original_file").and_then(Value::as_str) != Some(original_file.as_str())
        {
            return Err(invalid());
        }
        for path in [&working_file, &original_file] {
            if !names.contains(path) || !used_files.insert(path.clone()) {
                return Err(invalid());
            }
        }
        let working_sha = sha_field(row, "working_sha256")?.to_owned();
        let original_sha = sha_field(row, "original_sha256")?.to_owned();
        let working = read_member(&mut archive, &working_file, MAX_MEMBER)?;
        let original = read_member(&mut archive, &original_file, MAX_MEMBER)?;
        if hex::encode(Sha256::digest(&working)) != working_sha
            || hex::encode(Sha256::digest(&original)) != original_sha
        {
            return Err(invalid());
        }
        let working_text = std::str::from_utf8(&working).map_err(|_| invalid())?;
        let original_text = std::str::from_utf8(&original).map_err(|_| invalid())?;
        if working_text.trim().is_empty() || working_text.contains('\0')
            || original_text.contains('\0') || working_text.chars().count() > 2_000_000
            || original_text.chars().count() > 2_000_000
        {
            return Err(invalid());
        }
        parsed.push(json!({
            "chapter_index": index,
            "chapter_title": chapter_title,
            "source_url": chapter_url,
            "working_text": working_text,
            "original_text": original_text,
            "working_sha256": working_sha,
            "original_sha256": original_sha
        }));
    }
    if names.iter().any(|name| !used_files.contains(name)
        && name != "chapters/" && name != "original/")
    {
        return Err(invalid());
    }
    let mut indices: Vec<_> = used_indices.into_iter().collect();
    indices.sort_unstable();
    let missing_positions: Vec<_> = indices.windows(2)
        .filter(|pair| pair[1] > pair[0] + 1)
        .map(|pair| json!([pair[0] + 1, pair[1] - 1]))
        .collect();
    Ok(json!({
        "format": "full_cycle_app.chapter_exchange",
        "version": 1,
        "title": title,
        "source_url": source_url,
        "source_domain": source_domain,
        "external_book_id": external_book_id,
        "missing_positions": missing_positions,
        "repeated_source_url_count": duplicate_url_count,
        "repeated_source_url_pairs": url_pairs,
        "manifest_order_not_monotonic": nonmonotonic,
        "chapters": parsed
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_member_names_only() {
        for good in ["manifest.json", "README.txt", "chapters/000007.txt",
                     "original/1234567890.txt"] {
            assert!(allowed_file(good), "{good}");
        }
        for bad in ["chapters/7.txt", "chapters/000007.xhtml", "../evil",
                    "original/000007.txt/extra", "other/000007.txt",
                    "chapters/000007.TXT"] {
            assert!(!allowed_file(bad), "{bad}");
        }
    }
    #[test]
    fn strict_json_rejects_duplicate_keys_at_any_depth() {
        assert!(serde_json::from_slice::<UniqueJson>(
            br#"{"format":"a","format":"b"}"#
        ).is_err());
        assert!(serde_json::from_slice::<UniqueJson>(
            br#"{"chapters":[{"chapter_index":7,"chapter_index":9}]}"#
        ).is_err());
        assert!(serde_json::from_slice::<UniqueJson>(
            br#"{"format":"full_cycle_app.chapter_exchange"}"#
        ).is_ok());
    }
    #[test]
    fn no_invalid_sha_or_strings() {
        let row = json!({"working_sha256": "A".repeat(64), "title": "bad\nline"});
        assert!(sha_field(&row, "working_sha256").is_err());
        assert!(text_field(&row, "title", 500).is_err());
    }
}
