//! CI-only, read-only JSONL differential HTML text-node probe.
use erida_source_dom::{source_text_paths, visible_text_nodes};
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let line = line?;
        let html: Value = serde_json::from_str(&line)?;
        let input = html.as_str().ok_or("Each input line must be a JSON HTML string")?;
        let nodes = visible_text_nodes(input).map_err(|_| "Invalid bounded HTML input")?;
        let text_paths = source_text_paths(input).ok().map(|entries| {
            entries.into_iter()
                .map(|(text, dom_path)| json!({"text": text, "dom_path": dom_path}))
                .collect::<Vec<_>>()
        });
        serde_json::to_writer(
            &mut stdout,
            &json!({"nodes": nodes, "text_paths": text_paths}),
        )?;
        stdout.write_all(b"\n")?;
    }
    stdout.flush()?;
    Ok(())
}
