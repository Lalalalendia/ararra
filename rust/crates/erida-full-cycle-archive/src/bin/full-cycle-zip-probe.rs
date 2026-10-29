//! Offline CLI oracle probe for Python-vs-Rust Full Cycle source ZIP parity.
//! This binary is not the Erida HTTP server and has no database write access.
use erida_full_cycle_archive::load_source;
use serde_json::json;

fn main() {
    let mut arguments = std::env::args_os().skip(1);
    let (Some(file), Some(fallback)) = (arguments.next(), arguments.next()) else {
        eprintln!("usage: full-cycle-zip-probe SOURCE_ZIP FALLBACK_TITLE");
        std::process::exit(2);
    };
    if arguments.next().is_some() {
        eprintln!("expected exactly one ZIP and a fallback title");
        std::process::exit(2);
    }
    let fallback = fallback.to_string_lossy();
    let response = match std::fs::read(file) {
        Ok(bytes) => match load_source(&bytes, &fallback) {
            Ok(verified) => json!({"status": "ok", "source": verified}),
            Err(_) => json!({"status": "rejected"}),
        },
        Err(_) => json!({"status": "rejected"}),
    };
    println!("{}", response);
}
