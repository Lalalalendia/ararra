//! M1p-HASH read-only Python differential probe. No SQLite, network or writer.
use erida_source_clean::import_txt::clean_numbered_import_txt;
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let input: Value = serde_json::from_str(&line?)?;
        let html = input.get("html").and_then(Value::as_str)
            .ok_or("html must be a string")?;
        let path = input.get("chapter_path").and_then(Value::as_str)
            .ok_or("chapter_path must be a string")?;
        let result = match clean_numbered_import_txt(html, path) {
            Ok(value) => json!({
                "status": "ok",
                "cleaned_html": value.html,
                "source_hash": value.source_hash,
                "removed_nodes": value.removed_text_nodes,
            }),
            Err(_) => json!({"status": "unsupported"}),
        };
        serde_json::to_writer(&mut stdout, &result)?;
        stdout.write_all(b"\n")?;
    }
    stdout.flush()?;
    Ok(())
}
