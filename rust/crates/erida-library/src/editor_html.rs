//! M1l: the deliberately small language emitted by editor.mjs::textToHTML.
//! Parse structure BEFORE decoding the five editor entities, exactly once.
//! Decoded text is never interpreted as markup; export always escapes it again.
//! Not a general HTML sanitizer. Attributes, arbitrary tags/entities and broken
//! nesting remain unsupported. Acceptance stores the original bytes unchanged.

#[derive(Debug)]
struct Paragraph {
    leading_newline: bool,
    lines: Vec<String>,
}

#[derive(Debug)]
pub(crate) struct EditorHtml {
    paragraphs: Vec<Paragraph>,
}

fn finish_text(text: String) -> String {
    // BeautifulSoup html.parser collapses ASCII-whitespace-only data nodes.
    // Raw newlines are not in this language: the editor emits <br> instead.
    if !text.is_empty() && text.chars().all(|ch| ch == ' ' || ch == '\t') {
        " ".to_owned()
    } else {
        text
    }
}

pub(crate) fn parse(html: &str) -> Option<EditorHtml> {
    let mut rest = html;
    let mut paragraphs = Vec::new();
    let mut leading_newline = false;
    while !rest.is_empty() {
        rest = rest.strip_prefix("<p>")?;
        let mut lines = Vec::new();
        let mut text = String::new();
        loop {
            if let Some(tail) = rest.strip_prefix("</p>") {
                lines.push(finish_text(text));
                rest = tail;
                break;
            }
            if let Some(tail) = rest.strip_prefix("<br>") {
                lines.push(finish_text(text));
                text = String::new();
                rest = tail;
                continue;
            }
            if rest.starts_with('&') {
                // A fixed whitelist, NOT a recursive or general HTML decoder.
                let (entity, decoded) = [
                    ("&amp;", '&'), ("&lt;", '<'), ("&gt;", '>'),
                    ("&quot;", '"'), ("&#39;", '\''),
                ].into_iter().find(|(entity, _)| rest.starts_with(entity))?;
                text.push(decoded);
                rest = &rest[entity.len()..];
                continue;
            }
            let ch = rest.chars().next()?;
            if matches!(ch, '<' | '>') || (ch.is_control() && ch != '\t') {
                return None;
            }
            text.push(ch);
            rest = &rest[ch.len_utf8()..];
        }
        paragraphs.push(Paragraph { leading_newline, lines });
        leading_newline = false;
        if let Some(tail) = rest.strip_prefix('\n') {
            if tail.is_empty() { return None; }
            leading_newline = true;
            rest = tail;
        }
    }
    if paragraphs.is_empty() { None } else { Some(EditorHtml { paragraphs }) }
}

fn escape_text(text: &str, output: &mut String) {
    // BeautifulSoup's minimal HTML text serialization does NOT quote-escape.
    for ch in text.chars() {
        match ch {
            '&' => output.push_str("&amp;"),
            '<' => output.push_str("&lt;"),
            '>' => output.push_str("&gt;"),
            _ => output.push(ch),
        }
    }
}

fn is_heading(paragraph: &Paragraph, chapter_index: i64) -> Result<bool, &'static str> {
    // get_text(" ", strip=True) joins data nodes on both sides of <br>.
    let heading = paragraph.lines.iter().map(|text| text.trim())
        .filter(|text| !text.is_empty()).collect::<Vec<_>>().join(" ").to_lowercase();
    let Some(after) = heading.strip_prefix("глава").or_else(|| heading.strip_prefix("chapter"))
    else { return Ok(false); };
    if !after.chars().next().is_some_and(char::is_whitespace) { return Ok(false); }
    let number = after.trim_start();
    // Python's \d can match non-ASCII numerals. Do not falsely claim parity
    // for such chapter-heading candidates until their numeric mapping is tested.
    if number.chars().next().is_some_and(|ch| ch.is_numeric() && !ch.is_ascii_digit()) {
        return Err("rust_unsupported_fragment");
    }
    let end = number.find(|ch: char| !ch.is_ascii_digit()).unwrap_or(number.len());
    if end == 0 { return Ok(false); }
    let (digits, tail) = number.split_at(end);
    if !matches!(tail.trim(), "" | "." | ":" | "—" | "-") { return Ok(false); }
    // Avoid overflow for arbitrarily long headings, including leading zeroes.
    let digits = digits.trim_start_matches('0');
    Ok(digits == chapter_index.to_string())
}

fn normalize_plain(raw: &str) -> String {
    let spaces = raw.replace('\u{a0}', " ");
    let lines = spaces.split('\n')
        .map(|line| line.trim_matches(|ch| ch == ' ' || ch == '\t'));
    let joined = lines.collect::<Vec<_>>().join("\n");
    let mut result = String::with_capacity(joined.len());
    let mut breaks = 0;
    for ch in joined.trim().chars() {
        if ch == '\n' {
            breaks += 1;
            if breaks <= 2 { result.push(ch); }
        } else {
            breaks = 0;
            result.push(ch);
        }
    }
    result
}

impl EditorHtml {
    pub(crate) fn has_text(&self) -> bool {
        self.paragraphs.iter().any(|p| p.lines.iter().any(|line| !line.trim().is_empty()))
    }

    pub(crate) fn export(&self, chapter_index: i64) -> Result<(String, String), &'static str> {
        let skip = usize::from(is_heading(&self.paragraphs[0], chapter_index)?);
        let remaining = &self.paragraphs[skip..];
        if !remaining.iter().any(|p| p.lines.iter().any(|line| !line.trim().is_empty())) {
            return Err("empty_after_heading");
        }
        let mut plain = String::new();
        let mut html = String::new();
        for p in remaining {
            if p.leading_newline {
                plain.push('\n');
                html.push('\n');
            }
            plain.push('\n');
            html.push_str("<p>");
            for (index, text) in p.lines.iter().enumerate() {
                if index > 0 {
                    plain.push('\n');
                    html.push_str("<br/>");
                }
                plain.push_str(text);
                escape_text(text, &mut html);
            }
            plain.push('\n');
            html.push_str("</p>");
        }
        let plain = normalize_plain(&plain);
        if plain.is_empty() { return Err("empty_text"); }
        Ok((plain, html.trim().to_owned()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn editor_entities_breaks_and_paragraphs_match_python_serialization() {
        let raw = "<p>A &amp; B &quot;quote&quot; &#39;single&#39; &lt;tag&gt;</p>\n<p>C<br>D</p>";
        let value = parse(raw).unwrap();
        assert!(value.has_text());
        assert_eq!(value.export(1).unwrap(), (
            "A & B \"quote\" 'single' <tag>\n\nC\nD".to_owned(),
            "<p>A &amp; B \"quote\" 'single' &lt;tag&gt;</p>\n<p>C<br/>D</p>".to_owned(),
        ));
    }

    #[test]
    fn escaped_markup_and_double_entities_are_not_executable() {
        let raw = "<p>&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp;lt;b&amp;gt;</p>";
        let (plain, html) = parse(raw).unwrap().export(1).unwrap();
        assert!(plain.contains("<script>alert(\"x\")</script>"));
        assert!(plain.contains("&lt;b&gt;"));
        assert!(!html.contains("<script>"));
        assert!(html.contains("&lt;script&gt;"));
        assert!(html.contains("&amp;lt;b&amp;gt;"));
    }

    #[test]
    fn heading_is_removed_only_when_it_matches_the_current_chapter() {
        let value = parse("<p>Chapter<br>0001:</p>\n<p>Body.</p>").unwrap();
        assert_eq!(value.export(1).unwrap(), ("Body.".to_owned(), "<p>Body.</p>".to_owned()));
        assert!(value.export(2).unwrap().0.starts_with("Chapter\n0001:"));
        assert!(parse("<p>Chapter 1</p>").unwrap().export(1).is_err());
        assert_eq!(parse("<p>Chapter 1..</p>").unwrap().export(1).unwrap().0, "Chapter 1..");
    }

    #[test]
    fn invalid_structure_and_unknown_entities_are_not_admitted() {
        for raw in ["", "<p>Text &unknown;</p>", "<p>Text &amp</p>",
            "<p><b>Text</b></p>", "<p onload='x'>Text</p>", "<p><script>x</script></p>",
            "<p>Text<br onclick='x'>next</p>", "<p><p>nested</p></p>",
            "<p>Text</p> \n<p>More</p>", "<p>Text</p>\n", "<p>Text\u{0}</p>"] {
            assert!(parse(raw).is_none(), "{raw:?}");
        }
        assert!(!parse("<p> </p>").unwrap().has_text());
        assert!(parse("<p>!</p>").unwrap().has_text());
        assert!(parse("<p>&amp;</p>").unwrap().has_text());
    }

    #[test]
    fn whitespace_only_nodes_and_nbsp_follow_python_not_browser_guesswork() {
        assert_eq!(parse("<p>A\u{a0}B<br>\t </p>\n<p>Next</p>")
            .unwrap().export(1).unwrap(), (
                "A B\n\nNext".to_owned(),
                "<p>A\u{a0}B<br/> </p>\n<p>Next</p>".to_owned(),
            ));
    }
}
