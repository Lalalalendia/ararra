//! RUST-03b: bounded, READ-ONLY DOM text-node extraction.
//!
//! Python's BeautifulSoup(html, "html.parser") is still the authority.
//! This crate uses html5ever through scraper, which may differ on malformed
//! markup; its output is never used to calculate source hashes or mutate
//! chapters. Test fixture parity is evidence ONLY for the tested HTML shapes.
use scraper::{Html, Node};

#[derive(Debug, PartialEq, Eq)]
pub enum DomError {
    OversizedInput,
    TooManyTextNodes,
}

/// Mirror the ordered source text-node list on safely parsed HTML only.
/// Exclude comments, whitespace-only nodes, script/style ancestors and
/// <head> content when a literal <body> root was explicitly supplied.
///
/// Callers must NOT use this as a substitute for source_hash or the Python
/// parser without a separate full-markup differential acceptance gate.
pub fn visible_text_nodes(html: &str) -> Result<Vec<String>, DomError> {
    if html.len() > 2_000_000 {
        return Err(DomError::OversizedInput);
    }
    if html.is_empty() {
        return Ok(Vec::new());
    }
    let tree = Html::parse_document(html);
    let explicitly_body = html.to_ascii_lowercase().contains("<body");
    let root = if explicitly_body {
        tree.tree
            .root()
            .descendants()
            .find(|node| node.value().as_element().is_some_and(|value| value.name() == "body"))
            .unwrap_or(tree.tree.root())
    } else {
        tree.tree.root()
    };
    let mut result = Vec::new();
    for node in root.descendants() {
        let Node::Text(contents) = node.value() else {
            continue;
        };
        if contents.trim().is_empty() {
            continue;
        }
        if node.ancestors().any(|ancestor| {
            ancestor
                .value()
                .as_element()
                .is_some_and(|element| matches!(element.name(), "script" | "style"))
        }) {
            continue;
        }
        if result.len() >= 5000 {
            return Err(DomError::TooManyTextNodes);
        }
        result.push(contents.to_string());
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_separate_ordered_text_nodes() {
        let html = "<h1>Chapter 1</h1><p>A <b>bold</b> end</p><p>第二段。</p>";
        assert_eq!(
            visible_text_nodes(html).unwrap(),
            ["Chapter 1", "A ", "bold", " end", "第二段。"]
        );
    }

    #[test]
    fn ignores_comments_scripts_styles_and_empty_whitespace() {
        let html = "<!-- hidden --><p>一</p><script>bad()</script><style>x{color:red}</style><p>二</p>";
        assert_eq!(visible_text_nodes(html).unwrap(), ["一", "二"]);
        assert_eq!(visible_text_nodes("").unwrap(), Vec::<String>::new());
        assert_eq!(visible_text_nodes("  ").unwrap(), Vec::<String>::new());
    }

    #[test]
    fn honors_explicit_body_root() {
        let html = "<html><head><title>Not source</title></head><body><h1>Source title</h1><p>Body</p></body></html>";
        assert_eq!(visible_text_nodes(html).unwrap(), ["Source title", "Body"]);
    }

    #[test]
    fn bounded_input_fails_closed() {
        assert_eq!(
            visible_text_nodes(&"a".repeat(2_000_001)),
            Err(DomError::OversizedInput)
        );
    }
}
