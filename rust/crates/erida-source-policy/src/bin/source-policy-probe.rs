//! CI-only JSONL oracle probe; does not read files, sockets, or credentials.
use erida_source_policy::{classify_source_text, is_source_tail_boilerplate_text};
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let line = line?;
        let candidate: Value = serde_json::from_str(&line)?;
        let source = candidate.as_str().ok_or("Each input line must be a JSON string")?;
        if source.chars().count() > 5000 {
            return Err("Source policy probe input exceeds 5000 characters".into());
        }
        let classification = classify_source_text(source);
        serde_json::to_writer(&mut stdout, &json!({
            "kind": classification.kind,
            "reason": classification.reason,
            "tail_hint": is_source_tail_boilerplate_text(source),
        }))?;
        stdout.write_all(b"\n")?;
    }
    stdout.flush()?;
    Ok(())
}
