//! RUST-04: byte-preserving Rust port of Python split_source_text_node.
//!
//! Split only at Unicode CODEPOINT positions, not UTF-8 bytes or graphemes.
//! A small (max_chars + 2) window of byte offsets avoids retaining an
//! O(input_characters) global offset index in memory.
//! This is a pure, isolated library; no Python API/worker is replaced yet.

pub const DEFAULT_MAX_SOURCE_UNIT_CHARS: usize = 480;
pub const MAX_INPUT_BYTES: usize = 2_000_000;

const SEPARATORS: [&str; 11] = [
    "\n\n", ". ", "! ", "? ", "… ", "。", "！", "？", "; ", ", ", " ",
];

#[derive(Debug, PartialEq, Eq)]
pub enum SplitError {
    InvalidMaxChars,
    InputTooLarge,
}

/// Exact UTF-8-preserving counterpart of source_units.split_source_text_node.
///
/// Python's text.rfind(sep, search_min, search_max + 1) returns the LAST
/// match, even when its candidate endpoint violates search_max; if that
/// happens the Python algorithm does NOT retry the previous separator hit.
/// This unusual edge case is intentionally preserved below.
pub fn split_source_text_node(text: &str, max_chars: usize) -> Result<Vec<String>, SplitError> {
    if max_chars == 0 {
        return Err(SplitError::InvalidMaxChars);
    }
    if text.len() > MAX_INPUT_BYTES {
        return Err(SplitError::InputTooLarge);
    }
    let mut remaining_chars = text.chars().count();
    if remaining_chars <= max_chars {
        return Ok(vec![text.to_owned()]);
    }

    let mut remaining = text;
    let mut pieces = Vec::new();
    while remaining_chars > max_chars {
        // This is a local-window equivalent of Python's global char-index
        // search bounds; both absolute bounds shift by the same start index.
        let search_min = (max_chars / 2).max(1);
        let search_max = max_chars;

        // Need indices [0..=max_chars+1] for Python's END-inclusive search
        // window. If only max_chars+1 characters remain, its final endpoint
        // is the byte length of the entire remaining text.
        let mut offsets: Vec<usize> = remaining
            .char_indices()
            .take(max_chars.saturating_add(2))
            .map(|(byte_offset, _)| byte_offset)
            .collect();
        if offsets.len() == max_chars + 1 {
            offsets.push(remaining.len());
        }
        if offsets.len() <= search_max {
            // Defensive guard: the count above already proved the presence.
            return Err(SplitError::InvalidMaxChars);
        }
        let scope_end = offsets[search_max + 1];
        let slice = &remaining[offsets[search_min]..scope_end];
        let mut boundary = 0_usize;
        for sep in SEPARATORS {
            if let Some(byte_start) = slice.rfind(sep) {
                let byte_start_absolute = offsets[search_min] + byte_start;
                // Rust's match and Python's str indices both begin at a
                // Unicode codepoint boundary for these separators.
                if let Ok(start_char) = offsets.binary_search(&byte_start_absolute) {
                    let candidate = start_char + sep.chars().count();
                    if candidate <= search_max {
                        boundary = boundary.max(candidate);
                    }
                }
            }
        }
        if boundary == 0 {
            boundary = search_max;
        }

        let end_byte = offsets[boundary];
        pieces.push(remaining[..end_byte].to_owned());
        remaining = &remaining[end_byte..];
        remaining_chars -= boundary;
    }
    pieces.push(remaining.to_owned());
    Ok(pieces)
}

pub fn split_source_text_default(text: &str) -> Result<Vec<String>, SplitError> {
    split_source_text_node(text, DEFAULT_MAX_SOURCE_UNIT_CHARS)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_all_unicode_codepoints_in_original_order() {
        let original = "你好。Привет. Hello 😊 𠀀雨！".repeat(60);
        for n in [1, 2, 3, 7, 13, 64, 480] {
            let pieces = split_source_text_node(&original, n).unwrap();
            assert_eq!(pieces.concat(), original);
            assert!(pieces.iter().all(|s| s.chars().count() <= n));
        }
    }

    #[test]
    fn preserves_python_boundary_precedence_and_fallback() {
        let text = "First sentence. And second! Third paragraph, with comma. ";
        assert_eq!(
            split_source_text_node(text, text.chars().count()).unwrap(),
            [text.to_string()]
        );
        assert_eq!(split_source_text_node("", 480).unwrap(), [String::new()]);
        let long = "这".repeat(1500);
        let chunks = split_source_text_node(&long, 480).unwrap();
        assert_eq!(chunks.len(), 4);
        assert_eq!(chunks.concat(), long);
        assert_eq!(chunks[0].chars().count(), 480);
        assert_eq!(chunks[3].chars().count(), 60);
    }

    #[test]
    fn limits_invalid_requests_without_allocating_unbounded_windows() {
        assert_eq!(split_source_text_node("abc", 0), Err(SplitError::InvalidMaxChars));
        assert_eq!(
            split_source_text_node(&"A".repeat(MAX_INPUT_BYTES + 1), 480),
            Err(SplitError::InputTooLarge)
        );
    }
}
