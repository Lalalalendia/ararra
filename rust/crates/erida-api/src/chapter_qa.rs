//! M2c GET + M2d disposable-only reviewed QA finding CAS.
//! Python retains production ownership of QA scans and editorial decisions.
use super::{secured_read, write::secured_write, ApiError, AppState};
use axum::extract::{Path, State};
use axum::http::HeaderMap;
use axum::response::Response;
use axum::Json;
use erida_library::chapter_qa::{self, QaReadError};
use serde::Deserialize;

fn map_qa_error(error: QaReadError) -> ApiError {
    match error {
        QaReadError::IntegrityDrift => ApiError::BookConflict(
            "QA report fingerprints mismatch; revision integrity drift",
        ),
        QaReadError::Missing("Chapter not found") => ApiError::ChapterNotFound,
        QaReadError::Missing(message) => ApiError::QaMissing(message),
        QaReadError::Conflict(message) => ApiError::BookConflict(message),
        QaReadError::Invalid(message) => ApiError::BadRequest(message),
        QaReadError::Sqlite(_) | QaReadError::Malformed => ApiError::Unavailable,
    }
}

pub async fn current(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((book_id, chapter_id)): Path<(String, String)>,
) -> Result<Response, ApiError> {
    secured_read(state, headers, move |db, _, _| {
        chapter_qa::current(db, &book_id, &chapter_id)
            .map_err(map_qa_error)?
            .ok_or(ApiError::ChapterNotFound)
    }).await
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewInput {
    status: String,
    expected_review_revision: i64,
    expected_source_revision: i64,
    expected_accepted_revision: i64,
    expected_glossary_revision: i64,
    #[serde(default)]
    note: String,
}

pub async fn review(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((book_id, chapter_id, finding_id)): Path<(String, String, String)>,
    Json(body): Json<ReviewInput>,
) -> Result<Response, ApiError> {
    if !matches!(body.status.as_str(), "open" | "reviewed" | "waived")
        || body.expected_review_revision < 1
        || body.expected_source_revision < 1
        || body.expected_accepted_revision < 1
        || body.expected_glossary_revision < 0
        || body.note.chars().count() > 1000
    {
        return Err(ApiError::InvalidInput("Invalid QA review submission"));
    }
    secured_write(state, headers, move |db, actor, _| {
        chapter_qa::review(
            db, &book_id, &chapter_id, &finding_id, &body.status,
            body.expected_review_revision, body.expected_source_revision,
            body.expected_accepted_revision, body.expected_glossary_revision,
            actor, &body.note,
        ).map_err(map_qa_error)
    }).await
}


#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScanInput {
    expected_source_revision: i64,
    expected_accepted_revision: i64,
    expected_glossary_revision: i64,
}

/// Only disposable test-mode Rust SQLite writers can call this route.
/// A non-empty glossary or unsupported HTML is an explicit error, never PASS.
pub async fn scan(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((book_id, chapter_id)): Path<(String, String)>,
    Json(body): Json<ScanInput>,
) -> Result<Response, ApiError> {
    if body.expected_source_revision < 1
        || body.expected_accepted_revision < 1
        || body.expected_glossary_revision < 0
    {
        return Err(ApiError::InvalidInput("Invalid QA scan revisions"));
    }
    secured_write(state, headers, move |db, actor, _| {
        erida_library::qa_scan::scan_baseline(
            db, &book_id, &chapter_id, body.expected_source_revision,
            body.expected_accepted_revision, body.expected_glossary_revision,
            actor,
        ).map_err(map_qa_error)
    }).await
}
