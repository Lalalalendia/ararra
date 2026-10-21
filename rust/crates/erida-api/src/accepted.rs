//! RUST-02e: accepted translation CAS for disposable SQLite only.
//! The caller must have proved a current source revision and visible HTML.
use super::{write::secured_write, ApiError, AppState};
use axum::extract::{Path, State};
use axum::http::HeaderMap;
use axum::response::Response;
use axum::Json;
use erida_library::{self, LibraryError};
use serde::Deserialize;

#[derive(Deserialize)]
pub struct AcceptTranslation {
    translated_html: String,
    expected_revision: i64,
    expected_source_revision: i64,
}

pub async fn accept(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((book_id, chapter_id)): Path<(String, String)>,
    Json(body): Json<AcceptTranslation>,
) -> Result<Response, ApiError> {
    if body.translated_html.is_empty()
        || body.translated_html.chars().count() > 2_000_000
        || body.expected_revision < 0
        || body.expected_source_revision < 1
    {
        return Err(ApiError::InvalidInput("Invalid accepted translation submission"));
    }
    secured_write(state, headers, move |db, actor, _auth| {
        erida_library::accept_translation_conservative(
            db, &book_id, &chapter_id, &body.translated_html,
            body.expected_revision, body.expected_source_revision, actor,
        )
        .map_err(|error| match error {
            LibraryError::Conflict(message) => ApiError::BookConflict(message),
            LibraryError::GlossaryRuleConflict(message) => ApiError::DetailedGlossaryConflict(message),
            LibraryError::Missing(_) => ApiError::ChapterNotFound,
            LibraryError::UnsupportedTranslation => ApiError::InvalidInput(
                "Unsupported accepted translation HTML: visible text proof required",
            ),
            LibraryError::Invalid(message) => ApiError::BadRequest(message),
            LibraryError::Sqlite(_) | LibraryError::Malformed
            | LibraryError::UnsupportedSource => ApiError::Unavailable,
        })
    }).await
}
