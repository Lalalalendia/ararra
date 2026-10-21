//! JSONL oracle for exact Rust/Python text-unit boundaries (offline only).
use erida_source_split::split_source_text_node;
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut output = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let candidate: Value = serde_json::from_str(&line?)?;
        let text = candidate
            .get("text")
            .and_then(Value::as_str)
            .ok_or("text must be a string")?;
        let requested_max = candidate
            .get("max_chars")
            .and_then(Value::as_i64)
            .ok_or("max_chars must be an integer")?;
        let answer = if requested_max <= 0 {
            json!({"error": "invalid_max_chars"})
        } else {
            match split_source_text_node(text, requested_max as usize) {
                Ok(chunks) => json!({"chunks": chunks}),
                Err(_) => json!({"error": "unavailable"}),
            }
        };
        serde_json::to_writer(&mut output, &answer)?;
        output.write_all(b"\n")?;
    }
    output.flush()?;
    Ok(())
}
