//! M1p-HASH (proof only): bounded candidate for Python's
//! strip_source_boilerplate() on the canonical M1n/M1o TXT stage HTML.
//!
//! This is deliberately NOT called by any writer. A Python differential
//! oracle is mandatory before broadening or using it during book import.
use crate::{clean_plain_paragraphs, CleanError, CleanedSource};
use sha2::{Digest, Sha256};

/// A stage's numbered TXT entry is serialized exactly as
/// <h1>Глава N</h1>\n<p>...narrative...</p>[...].
/// Python strips the heading DOM node, and BeautifulSoup preserves the
/// separating newline. We only return a hash when *every other* source text
/// node is proven to survive unchanged under the existing conservative
/// Rust paragraph cleaner, so no unverified DOM serialization is needed.
pub fn clean_numbered_import_txt(
    html: &str,
    chapter_path: &str,
) -> Result<CleanedSource, CleanError> {
    if html.len() > 2_000_000 {
        return Err(CleanError::OversizedSource);
    }
    // Never parse a path, use it as a URL or resolve it to local filesystem.
    // The Python reference also receives this value for numeric context.
    if chapter_path.is_empty() || chapter_path.trim() != chapter_path
        || chapter_path.starts_with('/') || chapter_path.contains('\0')
        || chapter_path.replace('\\', "/").split('/').any(|s| s == "..")
    {
        return Err(CleanError::UnsupportedChapterContext);
    }
    let (heading, body) = html.split_once("</h1>\n")
        .ok_or(CleanError::UnsupportedHtml)?;
    let heading = heading.strip_prefix("<h1>")
        .ok_or(CleanError::UnsupportedHtml)?;
    let number = heading.strip_prefix("Глава ")
        .or_else(|| heading.strip_prefix("Chapter "))
        .ok_or(CleanError::UnsupportedHtml)?;
    if number.is_empty() || number.len() > 7
        || !number.bytes().all(|b| b.is_ascii_digit())
        || number.parse::<u32>().ok().is_none_or(|n| n == 0 || n > 9_999_999)
    {
        return Err(CleanError::UnsupportedHtml);
    }
    if body.is_empty() || !body.starts_with("<p>") || !body.ends_with("</p>") {
        return Err(CleanError::UnsupportedHtml);
    }
    // The Python stage HTML serializer emits newlines only between complete
    // paragraphs. The Rust paragraph cleaner accepts adjacent blocks; remove
    // ONLY those proven inter-block separators for *validation*.
    let compact = body.replace("</p>\n<p>", "</p><p>");
    if compact.contains('\n') || compact.contains('\r') {
        return Err(CleanError::UnsupportedHtml);
    }
    let checked = clean_plain_paragraphs(&compact, None)?;
    if checked.removed_text_nodes != 0 || checked.html != compact {
        return Err(CleanError::UnsupportedHtml);
    }
    // H1 is a standalone source-title text node; no other node may be
    // modified. Python soup.decompose() leaves precisely this newline before
    // the stage's escaped paragraph body. This assumption is asserted in the
    // differential test against the *real* Python source cleaner.
    let cleaned = format!("\n{body}");
    Ok(CleanedSource {
        source_hash: hex::encode(Sha256::digest(cleaned.as_bytes())),
        html: cleaned,
        removed_text_nodes: 1,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numbered_heading_is_removed_and_newline_is_included_in_hash() {
        let narrative = "События развиваются по дороге между городами. ".repeat(12);
        let html = format!("<h1>Глава 12</h1>\n<p>{narrative}</p>");
        let result = clean_numbered_import_txt(&html, "chapters/12.xhtml").unwrap();
        let expected = format!("\n<p>{narrative}</p>");
        assert_eq!(result.html, expected);
        assert_eq!(result.removed_text_nodes, 1);
        assert_eq!(
            result.source_hash,
            hex::encode(Sha256::digest(expected.as_bytes()))
        );
    }

    #[test]
    fn two_adjacent_story_paragraphs_keep_original_separator() {
        let html = format!(
            "<h1>Chapter 9</h1>\n<p>{}</p>\n<p>{}</p>",
            "A long ordinary narrative paragraph without any markup. ".repeat(9),
            "A second long narrative paragraph continues the story. ".repeat(9)
        );
        let result = clean_numbered_import_txt(&html, "volumes/9.xhtml").unwrap();
        assert_eq!(result.html, html.split_once("</h1>").unwrap().1);
    }

    #[test]
    fn unproved_grammar_fails_closed() {
        for sample in [
            "<h1>Глава 1. Часть</h1>\n<p>Текст.</p>",
            "<h1>Глава 1</h1>\n<p>A &amp; B</p>",
            "<h1>Chapter 2</h1>\n<p>Первая<br/>вторая</p>",
            "<h1>Chapter 3</h1>\n<p><script>x</script></p>",
            "<h1>Chapter 4</h1>\n<p>content</p>\ntext outside",
            "<h1>Глава 1</h1>\n<p>下一章</p>",
            "<h1>Глава 1</h1>\n",
            "<h1>Глава 0</h1>\n<p>Content</p>",
            "<h2>Глава 1</h2>\n<p>Content</p>",
        ] {
            assert!(clean_numbered_import_txt(sample, "chapters/1.xhtml").is_err(),
                    "Unexpectedly accepted {sample}");
        }
        assert!(clean_numbered_import_txt(
            "<h1>Глава 1</h1>\n<p>Текст.</p>", "../unsafe.xhtml"
        ).is_err());
    }
}
