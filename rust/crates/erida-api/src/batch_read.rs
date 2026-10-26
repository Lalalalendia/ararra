//! M2b: authenticated, read-only status for Python-created book batches.
//! No enqueue, paid provider, Drive I/O, SQLite schema creation or mutations.
use super::{secured_read, ApiError, AppState};
use axum::extract::{Path, Query, State};
use axum::http::HeaderMap;
use axum::response::Response;
use erida_library::translation_batches;
use serde::Deserialize;

#[derive(Deserialize)]
pub struct BatchQuery {
    limit: Option<i64>,
}

pub async fn list(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(book_id): Path<String>,
    Query(query): Query<BatchQuery>,
) -> Result<Response, ApiError> {
    let limit = query.limit.unwrap_or(20);
    if !(1..=50).contains(&limit) {
        return Err(ApiError::InvalidInput("Batch list limit must be in 1..50"));
    }
    secured_read(state, headers, move |db, _, _| {
        translation_batches::list(db, &book_id, limit)
            .map_err(|_| ApiError::Unavailable)?
            .ok_or(ApiError::BookNotFound)
    }).await
}

pub async fn get(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((book_id,batch_id)): Path<(String,String)>,
) -> Result<Response, ApiError> {
    secured_read(state, headers, move |db, _, _| {
        translation_batches::get(db, &book_id, &batch_id)
            .map_err(|_| ApiError::Unavailable)?
            .ok_or(ApiError::TranslationBatchNotFound)
    }).await
}
