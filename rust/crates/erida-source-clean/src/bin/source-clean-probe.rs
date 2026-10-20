//! Offline JSONL source-cleaning oracle. No SQLite or networking.
use erida_source_clean::clean_plain_paragraphs;
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut output = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let value: Value = serde_json::from_str(&line?)?;
        let html = value
            .get("html")
            .and_then(Value::as_str)
            .ok_or("Input html must be a string")?;
        let path = value.get("chapter_path").and_then(Value::as_str);
        let answer = match clean_plain_paragraphs(html, path) {
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
