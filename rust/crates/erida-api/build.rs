//! Embed the existing UI; never serve arbitrary files from the host filesystem.
use std::{env, fs, path::{Path, PathBuf}};

fn uncomment(mut text: String) -> String {
    while let Some(start) = text.find("{#") {
        let end = text[start + 2..].find("#}").expect("Unclosed Jinja comment");
        text.replace_range(start..start + 2 + end + 2, "");
    }
    text
}

fn walk(root: &Path, files: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(root).expect("UI asset directory missing") {
        let entry = entry.expect("Cannot inspect UI asset");
        let kind = entry.file_type().expect("Missing asset type");
        assert!(!kind.is_symlink(), "Do not embed symlinked UI assets");
        if kind.is_dir() { walk(&entry.path(), files); }
        else if kind.is_file() { files.push(entry.path()); }
    }
}

fn main() {
    let manifest = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let root = manifest.join("../../../src/chaptera_server/web_ui")
        .canonicalize().expect("Canonical UI source directory missing");
    println!("cargo:rerun-if-changed={}", root.display());
    let mut shell = fs::read_to_string(root.join("templates/shell.html")).unwrap();
    let include = "{% include \"partials/chapter_editor.html\" %}";
    assert_eq!(shell.matches(include).count(), 1, "Review changed template includes");
    let partial = fs::read_to_string(root.join("templates/partials/chapter_editor.html")).unwrap();
    shell = uncomment(shell.replace(include, &partial));
    // Rust native UI preview is always full-mode. Explicitly render the only
    // Python archive-only conditional as its full-mode branch before embedding.
    // Reject accidental changes instead of silently stripping new Jinja syntax.
    let intro = "<p>{% if archive_only %}Загрузите ZIP из Full Cycle App или файл с главами · без запуска перевода.{% else %}Из файла или по ссылке на книгу · без запуска перевода.{% endif %}</p>";
    assert_eq!(shell.matches(intro).count(), 1, "Review archive-only import intro");
    shell = shell.replace(intro, "<p>Из файла или по ссылке на книгу · без запуска перевода.</p>");
    let source_open = "            {% if not archive_only %}\\n";
    let source_close = "            {% endif %}\\n";
    assert_eq!(shell.matches(source_open).count(), 1, "Review archive-only source opening");
    assert_eq!(shell.matches(source_close).count(), 1, "Review archive-only source closing");
    shell = shell.replace(source_open, "").replace(source_close, "");
    assert_eq!(shell.matches("{{ active_page }}").count(), 1);
    let checked = shell.replace("{{ active_page }}", "overview");
    assert!(!["{{", "{%", "{#"].iter().any(|tag| checked.contains(*tag)),
        "New Jinja directives require explicit native renderer support");
    let body = "<body data-initial-page=";
    assert_eq!(shell.matches(body).count(), 1);
    shell = shell.replace(body, "<body data-runtime=\"rust-preview\" data-initial-page=");
    let script = "  <script type=\"module\" src=\"/app-static/js/readera.mjs\"></script>";
    assert_eq!(shell.matches(script).count(), 1);
    shell = shell.replace(script, &format!(
        "  <script type=\"module\" src=\"/app-static/js/rust-preview.mjs\"></script>\n{script}"));
    let static_root = root.join("static");
    let mut files = Vec::new();
    walk(&static_root, &mut files);
    files.sort();
    let mut generated = format!("pub const SHELL: &str = {:?};\n", shell);
    generated.push_str("pub const ASSETS: &[(&str, &str, &[u8])] = &[\n");
    for file in files {
        let name = file.strip_prefix(&static_root).unwrap().to_str().unwrap().replace('\\', "/");
        if name.contains(".test.") { continue; }
        let mime = match file.extension().and_then(|v| v.to_str()) {
            Some("css") => "text/css; charset=utf-8",
            Some("mjs" | "js") => "text/javascript; charset=utf-8",
            _ => continue,
        };
        generated.push_str(&format!("({:?}, {:?}, include_bytes!({:?})),\n", name, mime, file));
    }
    generated.push_str("];\n");
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    fs::write(out.join("preview_assets.rs"), generated).unwrap();
}
