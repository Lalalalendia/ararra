//! RUST-03c: bounded, read-only plain-paragraph source cleaning.
//! The Python BeautifulSoup cleaner remains authoritative. This proof-only
//! subset is NOT an enabled source writer or arbitrary HTML serializer.
use erida_source_policy::{
    classify_source_text, is_source_boilerplate_text, is_source_tail_boilerplate_text,
};
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CleanError {
    UnsupportedHtml,
    OversizedSource,
    UnsupportedChapterContext,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CleanedSource {
    pub html: String,
    pub source_hash: String,
    pub removed_text_nodes: usize,
}

fn chapter_number_like(text: &str) -> bool {
    // Numeric headings affect Python's contextual next-chapter tail detection.
    // Until that branch is ported, reject any possible numeric heading.
    let lowered = text.to_lowercase();
    if ["chapter", "глава"].iter().any(|name| {
        lowered.match_indices(*name).any(|(pos, _)| {
            lowered[pos + name.len()..]
                .trim_start_matches(|c: char| c.is_whitespace()
                    || matches!(c, '#' | '№' | ':' | '：' | '-' | '_'))
                .chars().next().is_some_and(|c| c.is_ascii_digit())
        })
    }) {
        return true;
    }
    if let Some(position) = text.find('第') {
        return text[position + '第'.len_utf8()..]
            .trim_start().chars().next().is_some_and(|c| c.is_ascii_digit());
    }
    false
}

/// Independently testable cleaner for immediately adjacent, attribute-free
/// HTML paragraphs with exactly one literal text node each. Excludes nested
/// tags, entities, all controls, empty nodes, and numeric heading contexts.
pub fn clean_plain_paragraphs(
    html: &str,
    current_chapter_path: Option<&str>,
) -> Result<CleanedSource, CleanError> {
    if html.len() > 2_000_000 {
        return Err(CleanError::OversizedSource);
    }
    if let Some(path) = current_chapter_path {
        if path.chars().any(|c| c.is_ascii_digit()) || chapter_number_like(path) {
            return Err(CleanError::UnsupportedChapterContext);
        }
    }
    if !html.starts_with("<p>") || !html.ends_with("</p>") {
        return Err(CleanError::UnsupportedHtml);
    }
    let mut remaining = html;
    let mut texts: Vec<&str> = Vec::new();
    let mut blocks: Vec<&str> = Vec::new();
    while !remaining.is_empty() {
        if texts.len() >= 4000 || !remaining.starts_with("<p>") {
            return Err(CleanError::UnsupportedHtml);
        }
        let end = remaining.find("</p>").ok_or(CleanError::UnsupportedHtml)?;
        let text = &remaining[3..end];
        if text.trim().is_empty()
            || text.contains('<') || text.contains('>') || text.contains('&')
            || text.chars().any(|ch| ch.is_control()
                || matches!(ch, '\u{00a0}' | '\u{200b}'..='\u{200f}' | '\u{feff}'))
            || chapter_number_like(text)
        {
            return Err(CleanError::UnsupportedHtml);
        }
        // Rust lowercase != Python Unicode casefold for arbitrary scripts.
        // Restrict the alphabet to the independently tested Unicode ranges.
        if text.chars().any(|ch| {
            !(ch.is_ascii()
                || matches!(ch,
                    '\u{0400}'..='\u{052f}'
                    | '\u{3400}'..='\u{9fff}'
                    | '\u{f900}'..='\u{faff}'
                    | '\u{20000}'..='\u{2fa1f}'
                    | '\u{3000}'..='\u{303f}'
                    | '\u{ff00}'..='\u{ffef}'
                    | '«' | '»' | '—' | '…' | '−'
                    | '“' | '”' | '‘' | '’'))
        }) {
            return Err(CleanError::UnsupportedHtml);
        }
        let length = end + "</p>".len();
        blocks.push(&remaining[..length]);
        texts.push(text);
        remaining = &remaining[length..];
    }
    // Python only treats an explicit next chapter marker as a tail from
    // index 1 onward when there is more than one visible node.
    let start_tail = texts.iter().enumerate()
        .skip(if texts.len() == 1 { 0 } else { 1 })
        .find_map(|(index, text)| {
            (classify_source_text(text).kind == "NEXT_CHAPTER_BOUNDARY")
                .then_some(index)
        });
    let mut output = String::new();
    let mut removed = 0usize;
    for (index, (text, block)) in texts.iter().zip(blocks.iter()).enumerate() {
        let last = index + 1 == texts.len();
        let drop = start_tail.is_some_and(|start| index >= start)
            || (last && is_source_tail_boilerplate_text(text))
            || is_source_boilerplate_text(text);
        if drop {
            removed += 1;
        } else {
            output.push_str(block);
        }
    }
    if removed == 0 {
        output = html.to_owned(); // Python returns original when nothing removed.
    }
    Ok(CleanedSource {
        source_hash: hex::encode(Sha256::digest(output.as_bytes())),
        html: output,
        removed_text_nodes: removed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_short_narrative_and_removes_standalone_watermark() {
        let html = "<p>他走进了房间。</p><p>记住本站</p><p>故事还在继续。</p>";
        let result = clean_plain_paragraphs(html, Some("parts/story.xhtml")).unwrap();
        assert_eq!(result.html, "<p>他走进了房间。</p><p>故事还在继续。</p>");
        assert_eq!(result.removed_text_nodes, 1);
        assert_eq!(result.source_hash, hex::encode(Sha256::digest(result.html.as_bytes())));
    }

    #[test]
    fn strips_next_chapter_tail_with_all_following_paragraphs() {
        let result = clean_plain_paragraphs(
            "<p>故事开始。</p><p>下一章</p><p>新章节文本。</p>", None
        ).unwrap();
        assert_eq!(result.html, "<p>故事开始。</p>");
        assert_eq!(result.removed_text_nodes, 2);
    }

    #[test]
    fn tail_hint_is_position_dependent() {
        let middle = "<p>溫馨提示：</p><p>故事内容。</p>";
        assert_eq!(clean_plain_paragraphs(middle, None).unwrap().html, middle);
        let tail = "<p>故事内容。</p><p>溫馨提示：</p>";
        assert_eq!(clean_plain_paragraphs(tail, None).unwrap().html, "<p>故事内容。</p>");
    }

    #[test]
    fn unsupported_markup_and_chapter_numbers_fail_closed() {
        for html in [
            "<p>短句 <strong>粗体</strong></p>",
            "<p>表情 &amp; 相等。</p>",
            "<div>不是段落。</div>",
            "<p> </p>",
            "<p>Chapter 42</p>"
        ] {
            assert_eq!(clean_plain_paragraphs(html, None), Err(CleanError::UnsupportedHtml));
        }
        assert_eq!(
            clean_plain_paragraphs("<p>普通故事。</p>", Some("chapter-7.xhtml")),
            Err(CleanError::UnsupportedChapterContext)
        );
    }
}
