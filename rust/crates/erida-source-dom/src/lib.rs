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
    UnsupportedTree,
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

/// RUST-03e: derive Python _source_node_dom_path ordinal keys on a
/// read-only well-formed fragment or explicit body. For a fragment,
/// html5ever inserts html/head/body wrapper tags which BeautifulSoup
/// html.parser does not; only their generated wrappers are hidden.
///
/// The ordinal after a tag counts *all* sibling tags, even ignored scripts;
/// text()[N] counts all text siblings, including whitespace, but not comments.
/// No source policy, accepted translation or write path imports these keys.
pub fn source_text_paths(html: &str) -> Result<Vec<(String, String)>, DomError> {
    if html.len() > 2_000_000 {
        return Err(DomError::OversizedInput);
    }
    if html.is_empty() {
        return Ok(Vec::new());
    }
    let tree = Html::parse_document(html);
    // Scope excludes standalone html/head without a literal body.
    // Neither parser guarantees equal recovery of those malformed documents.
    let lowered = html.to_ascii_lowercase();
    let explicit_body = lowered.contains("<body");
    if !explicit_body && (lowered.contains("<html") || lowered.contains("<head")) {
        return Err(DomError::UnsupportedTree);
    }
    let root = tree.tree.root()
        .descendants()
        .find(|node| node.value().as_element()
            .is_some_and(|element| element.name() == "body"))
        .ok_or(DomError::UnsupportedTree)?;
    let label = if explicit_body { "body" } else { "[document]" };
    let mut values = Vec::new();
    for node in root.descendants() {
        let Node::Text(contents) = node.value() else {
            continue;
        };
        if contents.trim().is_empty() {
            continue;
        }
        if node.ancestors().any(|ancestor| {
            ancestor.value().as_element().is_some_and(|element| {
                matches!(element.name(), "script" | "style")
            })
        }) {
            continue;
        }
        if values.len() >= 5000 {
            return Err(DomError::TooManyTextNodes);
        }
        let parent = node.parent().ok_or(DomError::UnsupportedTree)?;
        let text_ordinal = parent.children()
            .filter(|sibling| matches!(sibling.value(), Node::Text(_)))
            .position(|sibling| sibling.id() == node.id())
            .ok_or(DomError::UnsupportedTree)?;
        let mut parts = Vec::new();
        let mut current = parent;
        loop {
            if current.id() == root.id() {
                parts.push(label.to_owned());
                break;
            }
            let outer = current.parent().ok_or(DomError::UnsupportedTree)?;
            let ordinal = outer.children()
                .filter(|sibling| sibling.value().as_element().is_some())
                .position(|sibling| sibling.id() == current.id())
                .ok_or(DomError::UnsupportedTree)?;
            let tag = current.value().as_element()
                .ok_or(DomError::UnsupportedTree)?
                .name();
            parts.push(format!("{tag}[{ordinal}]"));
            current = outer;
        }
        parts.reverse();
        let path = format!("/{}/text()[{text_ordinal}]", parts.join("/"));
        values.push((contents.to_string(), path));
    }
    Ok(values)
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
    fn python_ordinal_paths_for_fragment_and_explicit_body() {
        let short = "<p>A <b>bold</b> after</p><p>第二章。</p>";
        assert_eq!(
            source_text_paths(short).unwrap(),
            [
                ("A ".to_owned(), "/[document]/p[0]/text()[0]".to_owned()),
                ("bold".to_owned(), "/[document]/p[0]/b[0]/text()[0]".to_owned()),
                (" after".to_owned(), "/[document]/p[0]/text()[1]".to_owned()),
                ("第二章。".to_owned(), "/[document]/p[1]/text()[0]".to_owned()),
            ]
        );
        let full = "<html><head><title>Hidden</title></head><body><p>Visible</p></body></html>";
        assert_eq!(
            source_text_paths(full).unwrap(),
            [("Visible".to_owned(), "/body/p[0]/text()[0]".to_owned())]
        );
    }

    #[test]
    fn preserves_comment_and_whitespace_ordinals() {
        let html = "<div>  A<!-- comment -->B<i>X</i>   C</div>";
        assert_eq!(
            source_text_paths(html).unwrap(),
            [
                ("  A".to_owned(), "/[document]/div[0]/text()[0]".to_owned()),
                ("B".to_owned(), "/[document]/div[0]/text()[1]".to_owned()),
                ("X".to_owned(), "/[document]/div[0]/i[0]/text()[0]".to_owned()),
                ("   C".to_owned(), "/[document]/div[0]/text()[2]".to_owned()),
            ]
        );
    }

    #[test]
    fn bounded_input_fails_closed() {
        assert_eq!(
            visible_text_nodes(&"a".repeat(2_000_001)),
            Err(DomError::OversizedInput)
        );
    }
}
