//! RUST-02b book directory with moderator ownership and revision-fenced transfer.
use super::{private_response, secured_read, write::secured_write, ApiError, AppState};
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::Response;
use axum::Json;
use erida_library::{self, LibraryError};
use serde::Deserialize;

#[derive(Deserialize)]
pub struct CreateBook {
    book_id: String,
    title: String,
    owner_member_id: Option<String>,
}

#[derive(Deserialize)]
pub struct ReassignBookOwner {
    owner_member_id: String,
    expected_owner_revision: i64,
}

fn length_valid(value: &str, min: usize, max: usize) -> bool {
    let size = value.chars().count();
    size >= min && size <= max
}

fn map_library(error: LibraryError) -> ApiError {
    match error {
        LibraryError::Conflict(reason) => ApiError::BookConflict(reason),
        LibraryError::Missing(_) => ApiError::BookNotFound,
        LibraryError::Invalid(reason) => ApiError::BadRequest(reason),
        LibraryError::Sqlite(_) | LibraryError::Malformed => ApiError::Unavailable,
    }
}

pub async fn books(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    secured_read(state, headers, |db, _, _| {
        erida_library::list_books(db).map_err(map_library)
    }).await
}

pub async fn book(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(book_id): Path<String>,
) -> Result<Response, ApiError> {
    secured_read(state, headers, move |db, _, _| {
        erida_library::get_book(db, &book_id)
            .map_err(map_library)?
            .ok_or(ApiError::BookNotFound)
    }).await
}

pub async fn chapters(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(book_id): Path<String>,
) -> Result<Response, ApiError> {
    secured_read(state, headers, move |db, _, _| {
        erida_library::list_chapters(db, &book_id)
            .map_err(map_library)?
            .ok_or(ApiError::BookNotFound)
    }).await
}

pub async fn chapter(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((book_id, chapter_id)): Path<(String, String)>,
) -> Result<Response, ApiError> {
    secured_read(state, headers, move |db, _, _| {
        erida_library::get_chapter(db, &book_id, &chapter_id)
            .map_err(map_library)?
            .ok_or(ApiError::ChapterNotFound)
    }).await
}

pub async fn create(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<CreateBook>,
) -> Result<Response, ApiError> {
    if !length_valid(&body.book_id, 1, 128)
        || !length_valid(&body.title, 1, 500)
        || body.owner_member_id.as_deref().is_some_and(|v| !length_valid(v, 0, 128))
    {
        return Err(ApiError::InvalidInput("Invalid book submission"));
    }
    let mut response = secured_write(state, headers, move |db, actor, auth| {
        let owner = body.owner_member_id.as_deref().filter(|s| !s.is_empty()).unwrap_or(actor);
        if !erida_auth::is_active_moderator(owner, db, auth)? {
            return Err(ApiError::InvalidOwner);
        }
        if auth.role(actor) != "admin" && owner != actor {
            return Err(ApiError::ForbiddenAssignment);
        }
        erida_library::create_book(db, &body.book_id, &body.title, owner, actor)
            .map_err(map_library)
    }).await?;
    *response.status_mut() = StatusCode::CREATED;
    Ok(response)
}

pub async fn reassign(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(book_id): Path<String>,
    Json(body): Json<ReassignBookOwner>,
) -> Result<Response, ApiError> {
    if !length_valid(&body.owner_member_id, 1, 128) || body.expected_owner_revision < 0 {
        return Err(ApiError::InvalidInput("Invalid book ownership request"));
    }
    secured_write(state, headers, move |db, actor, auth| {
        if auth.role(actor) != "admin" {
            return Err(ApiError::ForbiddenAdmin);
        }
        if !erida_auth::is_active_moderator(&body.owner_member_id, db, auth)? {
            return Err(ApiError::InvalidOwner);
        }
        erida_library::transfer_owner(
            db, &book_id, &body.owner_member_id, body.expected_owner_revision, actor,
        ).map_err(map_library)
    }).await
}
