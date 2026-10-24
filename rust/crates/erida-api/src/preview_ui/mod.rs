//! Explicitly limited preview of the real UI, served by the same Rust process.
use axum::{body::Body, extract::Path, http::{header, HeaderValue, StatusCode},
    response::Response, routing::get, Router};
include!(concat!(env!("OUT_DIR"), "/preview_assets.rs"));

const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'";
const LIMITED: &str = "Rust UI preview: source-site import, QA, quotas, paid translation and personal cabinet are not migrated. Only strict canonical TXT import is experimental. This is not production readiness.";

fn reply(code: StatusCode, mime: &'static str, body: impl Into<Body>) -> Response {
    let mut response = Response::new(body.into());
    *response.status_mut() = code;
    let headers = response.headers_mut();
    for (name, value) in [
        (header::CONTENT_TYPE, mime),
        (header::CACHE_CONTROL, "private, no-store"),
        (header::PRAGMA, "no-cache"),
        (header::REFERRER_POLICY, "no-referrer"),
        (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
        (header::X_FRAME_OPTIONS, "DENY"),
        (header::CONTENT_SECURITY_POLICY, CSP),
    ] { headers.insert(name, HeaderValue::from_static(value)); }
    headers.insert("x-readera-runtime", HeaderValue::from_static("rust-ui-preview"));
    response
}

fn render(page: &str) -> Option<String> {
    if !matches!(page, "overview" | "books" | "editor" | "team" | "import") { return None; }
    Some(SHELL.replace("{{ active_page }}", page))
}
async fn home() -> Response { page(Path("overview".to_owned())).await }
async fn page(Path(page): Path<String>) -> Response {
    if let Some(html) = render(&page) {
        reply(StatusCode::OK, "text/html; charset=utf-8", html)
    } else if matches!(page.as_str(), "jobs" | "cabinet") {
        reply(StatusCode::NOT_IMPLEMENTED, "text/plain; charset=utf-8", LIMITED)
    } else {
        reply(StatusCode::NOT_FOUND, "text/plain; charset=utf-8", "Page not found")
    }
}
async fn asset(Path(path): Path<String>) -> Response {
    // These two modules explicitly report missing capabilities. They do not
    // forge QA responses, alter fetch(), or bypass authentication.
    let special: Option<&'static [u8]> = match path.as_str() {
        "js/rust-preview.mjs" => Some(include_bytes!("runtime.mjs")),
        "js/qa-panel.mjs" => Some(include_bytes!("qa-unavailable.mjs")),
        _ => None,
    };
    if let Some(bytes) = special {
        return reply(StatusCode::OK, "text/javascript; charset=utf-8", bytes);
    }
    if let Some((_, mime, bytes)) = ASSETS.iter().find(|(key, _, _)| *key == path.as_str()) {
        return reply(StatusCode::OK, mime, *bytes);
    }
    reply(StatusCode::NOT_FOUND, "text/plain; charset=utf-8", "Asset not found")
}

pub fn router() -> Router {
    Router::new().route("/app", get(home)).route("/app/", get(home))
        .route("/app/{page}", get(page)).route("/app-static/{*path}", get(asset))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fixed_page_names_are_the_only_template_input() {
        for page in ["overview", "books", "editor", "team", "import"] {
            let html = render(page).unwrap();
            assert!(html.contains(&format!("data-initial-page=\"{page}\"")));
            assert!(html.contains("id=\"chapter-editor\""));
            assert!(html.contains("data-runtime=\"rust-preview\""));
            assert!(!html.contains("{{") && !html.contains("{%") && !html.contains("{#"));
        }
        for page in ["jobs", "cabinet", "<script>", "../../secret"] {
            assert!(render(page).is_none());
        }
    }
    #[test]
    fn static_asset_registry_excludes_tests_and_host_paths() {
        assert!(ASSETS.iter().any(|(key, _, _)| *key == "js/readera.mjs"));
        assert!(ASSETS.iter().any(|(key, _, _)| *key == "css/readera.css"));
        let mut seen = std::collections::BTreeSet::new();
        for (key, _, bytes) in ASSETS {
            assert!(!key.contains("..") && !key.starts_with('/') && !key.contains(".test."));
            assert!(!bytes.is_empty() && seen.insert(key));
        }
    }
}
