//! M2c: read only, authenticated chapter QA snapshot.
//! Never execute a diagnostic scan, review finding or write any source text.
use super::{secured_read, ApiError, AppState};
use axum::extract::{Path, State};
use axum::http::HeaderMap;
use axum::response::Response;
use erida_library::chapter_qa::{self, QaReadError};

pub async fn current(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((book_id, chapter_id)): Path<(String, String)>,
) -> Result<Response, ApiError> {
    secured_read(state, headers, move |db, _, _| {
        chapter_qa::current(db, &book_id, &chapter_id)
            .map_err(|error| match error {
                QaReadError::IntegrityDrift => ApiError::BookConflict(
                    "QA report fingerprints mismatch; revision integrity drift",
                ),
                QaReadError::Sqlite(_) | QaReadError::Malformed => ApiError::Unavailable,
            })?
            .ok_or(ApiError::ChapterNotFound)
    }).await
}
