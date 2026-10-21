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


/// RUST-03f: a narrowly validated, well-formed inline paragraph grammar.
/// This does not replace BeautifulSoup or enable any new database writer.
///
/// Allowed blocks: immediately adjacent <p>...</p>; allowed nested inline
/// tags: b, strong, i, em, span (no attributes/entities/comments). Python's
/// source analyzer classifies each visible *text node*, not concatenated p
/// text. A nested node that might be removed is rejected, because extracting
/// that node could change the remaining markup in ways not covered here.
pub fn clean_inline_paragraphs(
    html: &str,
    current_chapter_path: Option<&str>,
) -> Result<CleanedSource, CleanError> {
    if html.len() > 2_000_000 {
        return Err(CleanError::OversizedSource);
    }
    if let Some(path) = current_chapter_path {
        if path.chars().any(char::is_numeric)
            || chapter_number_like(path)
            || ["第", "章", "節", "节", "回"]
                .iter().any(|token| path.contains(token))
        {
            return Err(CleanError::UnsupportedChapterContext);
        }
    }
    if !html.starts_with("<p>") || !html.ends_with("</p>") {
        return Err(CleanError::UnsupportedHtml);
    }
    #[derive(Debug)]
    struct Paragraph<'a> {
        raw: &'a str,
        nodes: Vec<&'a str>,
        inline: bool,
    }
    let mut blocks = Vec::<Paragraph<'_>>::new();
    let mut rest = html;
    while !rest.is_empty() {
        if blocks.len() >= 4000 || !rest.starts_with("<p>") {
            return Err(CleanError::UnsupportedHtml);
        }
        let end = rest.find("</p>").ok_or(CleanError::UnsupportedHtml)?;
        let inside = &rest[3..end];
        let raw = &rest[..end + 4];
        let mut nodes = Vec::<&str>::new();
        let mut stack = Vec::<&str>::new();
        let mut cursor = 0;
        let mut inline = false;
        while cursor < inside.len() {
            let tail = &inside[cursor..];
            if tail.starts_with('<') {
                let closer = tail.find('>').ok_or(CleanError::UnsupportedHtml)?;
                let token = &tail[..=closer];
                match token {
                    "<b>" | "<strong>" | "<em>" | "<i>" | "<span>" => {
                        if stack.len() >= 16 {
                            return Err(CleanError::UnsupportedHtml);
                        }
                        stack.push(&token[1..token.len() - 1]);
                    }
                    "</b>" | "</strong>" | "</em>" | "</i>" | "</span>" => {
                        let name = &token[2..token.len() - 1];
                        if stack.pop() != Some(name) {
                            return Err(CleanError::UnsupportedHtml);
                        }
                    }
                    _ => return Err(CleanError::UnsupportedHtml),
                }
                inline = true;
                cursor += closer + 1;
                continue;
            }
            let length = tail.find('<').unwrap_or(tail.len());
            let text = &tail[..length];
            if length == 0
                || text.contains('>') || text.contains('&')
                || text.chars().any(|ch| {
                    ch.is_control()
                        || matches!(ch, '\u{00a0}' | '\u{200b}'..='\u{200f}' | '\u{feff}')
                })
                || chapter_number_like(text)
                || text.chars().any(|ch| {
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
                })
            {
                return Err(CleanError::UnsupportedHtml);
            }
            if !text.trim().is_empty() {
                nodes.push(text);
            }
            cursor += length;
        }
        if !stack.is_empty() || nodes.is_empty() {
            return Err(CleanError::UnsupportedHtml);
        }
        if inline && nodes.iter().any(|node| {
            is_source_boilerplate_text(node)
                || is_source_tail_boilerplate_text(node)
                || classify_source_text(node).kind == "NEXT_CHAPTER_BOUNDARY"
        }) {
            // Python may extract a nested text node instead of removing the
            // entire paragraph. Until equivalent mutation/serialization is
            // verified, do not guess the rewritten HTML or its SHA-256.
            return Err(CleanError::UnsupportedHtml);
        }
        blocks.push(Paragraph { raw, nodes, inline });
        rest = &rest[end + 4..];
    }
    if blocks.is_empty() {
        return Err(CleanError::UnsupportedHtml);
    }

    // Equivalent to the Python standalone explicit-tail condition: the
    // marker must occur after the FIRST visible text node. At a pure p,
    // the marker can be removed with its entire containing paragraph.
    let mut visible_before = 0usize;
    let mut tail_at = None;
    for (index, block) in blocks.iter().enumerate() {
        if !block.inline
            && visible_before > 0
            && classify_source_text(block.nodes[0]).kind == "NEXT_CHAPTER_BOUNDARY"
        {
            tail_at = Some(index);
            break;
        }
        visible_before += block.nodes.len();
    }
    let mut cleaned = String::new();
    let mut removed_nodes = 0usize;
    for (index, block) in blocks.iter().enumerate() {
        let last = index + 1 == blocks.len();
        let simple = !block.inline && block.nodes.len() == 1;
        let drop = tail_at.is_some_and(|start| index >= start)
            || (simple && is_source_boilerplate_text(block.nodes[0]))
            || (simple && last && is_source_tail_boilerplate_text(block.nodes[0]));
        if drop {
            removed_nodes += block.nodes.len();
        } else {
            cleaned.push_str(block.raw);
        }
    }
    // str(BeautifulSoup(...)) is only guaranteed to equal this concatenation
    // for the strict, no-attribute, no-entity balanced-tag grammar above.
    // The Python differential test is mandatory for every widening.
    if removed_nodes == 0 {
        cleaned = html.to_owned();
    }
    Ok(CleanedSource {
        source_hash: hex::encode(Sha256::digest(cleaned.as_bytes())),
        html: cleaned,
        removed_text_nodes: removed_nodes,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn nested_narrative_is_preserved_while_pure_watermark_is_removed() {
        let input = "<p>Привет <strong>друг</strong>, сказал он.</p><p>记住本站</p><p>故事 <em>继续</em>。</p>";
        let clean = clean_inline_paragraphs(input, Some("parts/story.xhtml")).unwrap();
        let expected = "<p>Привет <strong>друг</strong>, сказал он.</p><p>故事 <em>继续</em>。</p>";
        assert_eq!(clean.html, expected);
        assert_eq!(clean.removed_text_nodes, 1);
        assert_eq!(clean.source_hash, hex::encode(Sha256::digest(expected.as_bytes())));
    }

    #[test]
    fn removes_explicit_tail_without_discarding_inline_body() {
        let input = "<p>История <i>продолжается</i>.</p><p>下一章</p><p>Другой <b>текст</b>.</p>";
        let result = clean_inline_paragraphs(input, None).unwrap();
        assert_eq!(result.html, "<p>История <i>продолжается</i>.</p>");
        assert_eq!(result.removed_text_nodes, 3);
    }

    #[test]
    fn unsupported_nested_marker_and_unbalanced_tags_fail_closed() {
        for sample in [
            "<p>A <b>记住本站</b> B</p>",
            "<p>Before <span>下一章</span> after.</p>",
            "<p>text <b>unclosed</p>",
            "<p><b>wrong close</i></p>",
            "<p>a &amp; b</p>",
            "<p class='a'>hello</p>",
            "<p>Story <script>alert(1)</script></p>",
            "<p>Chapter 7 <i>content</i></p>",
        ] {
            assert_eq!(
                clean_inline_paragraphs(sample, Some("parts/story.xhtml")),
                Err(CleanError::UnsupportedHtml),
                "Unexpectedly accepted {sample}"
            );
        }
        assert_eq!(
            clean_inline_paragraphs("<p>A <b>B</b></p>", Some("chapters/7.xhtml")),
            Err(CleanError::UnsupportedChapterContext)
        );
    }


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
