//! M1f: accepted-book preview and download, read-only Rust HTTP.
//! No Python fallback, no export on unsafe/unproven HTML, no SQLite writes.
use super::{authenticated_read, private_response, ApiError, AppState};
use axum::body::Body;
use axum::extract::{rejection::QueryRejection, Path, Query, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::Response;
use erida_library::book_export::{self, ExportError};
use erida_library::LibraryError;
use serde::Deserialize;

#[derive(Deserialize)]
pub struct PreviewQuery {
    expected_last_chapter: i64,
}

#[derive(Deserialize)]
pub struct DownloadQuery {
    expected_last_chapter: i64,
    expected_manifest_sha256: String,
    format: String,
}

fn export_error(error: ExportError) -> ApiError {
    match error {
        ExportError::NotReady(preview) => ApiError::ExportNotReady(preview),
        ExportError::Invalid(message) => ApiError::InvalidInput(message),
        ExportError::Library(error) => library_error(error),
    }
}

fn library_error(error: LibraryError) -> ApiError {
    match error {
        LibraryError::Missing(_) => ApiError::BookNotFound,
        LibraryError::Invalid(message) => ApiError::InvalidInput(message),
        _ => ApiError::Unavailable,
    }
}

pub async fn preview(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(book_id): Path<String>,
    query: Result<Query<PreviewQuery>, QueryRejection>,
) -> Result<Response, ApiError> {
    let Query(query) = query
        .map_err(|_| ApiError::InvalidInput("Invalid export preview query"))?;
    if !(1..=2_000).contains(&query.expected_last_chapter) {
        return Err(ApiError::InvalidInput("Invalid declared final chapter"));
    }
    let output = authenticated_read(state, headers, move |db, _, _| {
        let result = book_export::inspect_export(
            db, &book_id, query.expected_last_chapter,
        ).map_err(library_error)?;
        Ok(result.preview)
    }).await?;
    Ok(private_response(output))
}

pub async fn download(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(book_id): Path<String>,
    query: Result<Query<DownloadQuery>, QueryRejection>,
) -> Result<Response, ApiError> {
    let Query(query) = query
        .map_err(|_| ApiError::InvalidInput("Invalid export download query"))?;
    if !(1..=2_000).contains(&query.expected_last_chapter) ||
        query.expected_manifest_sha256.len() != 64 ||
        !query.expected_manifest_sha256.bytes().all(|c|
            c.is_ascii_digit() || (b'a'..=b'f').contains(&c)
        ) ||
        !matches!(query.format.as_str(), "txt" | "html")
    {
        return Err(ApiError::InvalidInput("Invalid export download query"));
    }
    let safe_name = {
        let mut cleaned = book_id.chars()
            .map(|ch| if ch.is_ascii_alphanumeric() ||
                matches!(ch, '_' | '-') { ch } else { '_' })
            .take(50)
            .collect::<String>();
        if cleaned.is_empty() {
            cleaned = "book".to_owned();
        }
        cleaned
    };
    let file = authenticated_read(state, headers, move |db, _, _| {
        book_export::render_export(
            db, &book_id, query.expected_last_chapter,
            &query.expected_manifest_sha256, &query.format,
        ).map_err(export_error)
    }).await?;

    let mut response = Response::new(Body::from(file.bytes));
    *response.status_mut() = StatusCode::OK;
    let headers = response.headers_mut();
    headers.insert(header::CONTENT_TYPE, HeaderValue::from_static(file.content_type));
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store, private"));
    headers.insert(header::X_CONTENT_TYPE_OPTIONS, HeaderValue::from_static("nosniff"));
    let name = format!("attachment; filename=\"readera-{safe_name}.{}\"", file.extension);
    headers.insert(header::CONTENT_DISPOSITION,
        HeaderValue::from_str(&name).map_err(|_| ApiError::Unavailable)?);
    headers.insert("x-readera-manifest-sha256",
        HeaderValue::from_str(&file.manifest_sha256).map_err(|_| ApiError::Unavailable)?);
    headers.insert("x-readera-content-sha256",
        HeaderValue::from_str(&file.content_sha256).map_err(|_| ApiError::Unavailable)?);
    headers.insert(header::ETAG,
        HeaderValue::from_str(&format!("\"{}\"", file.content_sha256))
            .map_err(|_| ApiError::Unavailable)?);
    Ok(response)
}
