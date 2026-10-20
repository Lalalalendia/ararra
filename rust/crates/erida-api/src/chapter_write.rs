//! Experimental revisioned chapter source writes, test SQLite only.
//! Full BeautifulSoup/source-unit stripping is NOT yet implemented in Rust.
use super::{write::secured_write, ApiError, AppState};
use axum::extract::{Path, State};
use axum::http::HeaderMap;
use axum::response::Response;
use axum::Json;
use erida_library::{self, LibraryError};
use serde::Deserialize;

#[derive(Deserialize)]
pub struct SaveChapter {
    chapter_index: i64,
    chapter_path: String,
    source_html: String,
    expected_revision: i64,
}

fn map_error(error: LibraryError) -> ApiError {
    match error {
        LibraryError::Conflict(reason) => ApiError::BookConflict(reason),
        LibraryError::Missing(_) => ApiError::BookNotFound,
        LibraryError::UnsupportedSource => ApiError::InvalidInput(
            "Unsupported source HTML: Python source-policy normalization required",
        ),
        LibraryError::Invalid(reason) => ApiError::BadRequest(reason),
        LibraryError::Sqlite(_) | LibraryError::Malformed => ApiError::Unavailable,
    }
}

pub async fn save_chapter(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((book_id, chapter_id)): Path<(String, String)>,
    Json(body): Json<SaveChapter>,
) -> Result<Response, ApiError> {
    // Follow Pydantic SaveChapter field bounds before entering the SQLite
    // transaction; Python BookLibrary handles path semantics below.
    if body.chapter_index < 1
        || body.expected_revision < 0
        || body.chapter_path.chars().count() < 1
        || body.chapter_path.chars().count() > 1024
        || body.source_html.is_empty()
        || body.source_html.chars().count() > 2_000_000
    {
        return Err(ApiError::InvalidInput("Invalid chapter submission"));
    }
    secured_write(state, headers, move |db, _actor, _auth| {
        erida_library::put_chapter_conservative(
            db,
            &book_id,
            &chapter_id,
            body.chapter_index,
            &body.chapter_path,
            &body.source_html,
            body.expected_revision,
        )
        .map_err(map_error)
    })
    .await
}
