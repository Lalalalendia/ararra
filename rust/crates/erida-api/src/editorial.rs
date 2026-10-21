//! RUST-M1b editorial draft GET plus disposable-only PUT.
//! Never change original source HTML or accepted translation.
use super::{secured_read, write::secured_write, ApiError, AppState};
use axum::extract::{Path, State};
use axum::http::HeaderMap;
use axum::response::Response;
use axum::Json;
use erida_library::{self, LibraryError};
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SaveDraft {
    body_text: String,
    expected_revision: i64,
    expected_source_revision: i64,
    expected_accepted_revision: i64,
    #[serde(default)]
    rebase: bool,
}

fn map_library(error: LibraryError) -> ApiError {
    match error {
        LibraryError::Missing(_) => ApiError::ChapterNotFound,
        LibraryError::Conflict(message) => ApiError::BookConflict(message),
        LibraryError::Invalid(message) => ApiError::BadRequest(message),
        LibraryError::Sqlite(_) | LibraryError::Malformed
        | LibraryError::GlossaryRuleConflict(_)
        | LibraryError::UnsupportedSource | LibraryError::UnsupportedTranslation
            => ApiError::Unavailable,
    }
}

pub async fn read_draft(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((book_id, chapter_id)): Path<(String, String)>,
) -> Result<Response, ApiError> {
    secured_read(state, headers, move |db, _, _| {
        erida_library::get_editor_draft(db, &book_id, &chapter_id)
            .map_err(map_library)
    }).await
}

pub async fn save_draft(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((book_id, chapter_id)): Path<(String, String)>,
    Json(body): Json<SaveDraft>,
) -> Result<Response, ApiError> {
    if body.body_text.chars().count() > 1_000_000
        || body.expected_revision < 0
        || body.expected_source_revision < 1
        || body.expected_accepted_revision < 0
    {
        return Err(ApiError::InvalidInput("Invalid editor draft submission"));
    }
    secured_write(state, headers, move |db, actor, _auth| {
        erida_library::save_editor_draft(
            db, &book_id, &chapter_id, &body.body_text,
            body.expected_revision, body.expected_source_revision,
            body.expected_accepted_revision, body.rebase, actor,
        ).map_err(map_library)
    }).await
}
