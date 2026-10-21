//! Offline JSONL oracle: strict nested inline paragraph cleaner only.
use erida_source_clean::clean_inline_paragraphs;
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut output = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let value: Value = serde_json::from_str(&line?)?;
        let html = value.get("html").and_then(Value::as_str)
            .ok_or("html must be string")?;
        let chapter_path = value.get("chapter_path").and_then(Value::as_str);
        let answer = match clean_inline_paragraphs(html, chapter_path) {
            Ok(result) => json!({
                "status": "ok",
                "cleaned_html": result.html,
                "source_hash": result.source_hash,
                "removed_nodes": result.removed_text_nodes,
            }),
            Err(_) => json!({"status": "unsupported"}),
        };
        serde_json::to_writer(&mut output, &answer)?;
        output.write_all(b"\n")?;
    }
    output.flush()?;
    Ok(())
}
