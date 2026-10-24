//! M1o: actor-owned stage/read/cancel; writes require disposable temp DB gate.
use super::{secured_read, write::secured_write, ApiError, AppState};
use axum::extract::{Multipart, Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::Response;
use erida_library::import_stage::{self, StageError};

fn map_error(error: StageError) -> ApiError {
    match error {
        StageError::Invalid(message) => ApiError::InvalidInput(message),
        StageError::Conflict(message) => ApiError::BookConflict(message),
        StageError::TooLarge(message) => ApiError::ImportStageTooLarge(message),
        StageError::Missing => ApiError::ImportStageMissing,
        StageError::Expired => ApiError::ImportStageExpired,
        StageError::Sqlite(_) | StageError::Malformed => ApiError::Unavailable,
    }
}

pub async fn stage(
    State(state): State<AppState>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> Result<Response, ApiError> {
    if !state.test_writes_enabled {
        return Err(ApiError::Unavailable);
    }
    let idempotency = headers.get("idempotency-key")
        .map(|v| v.to_str().map(str::to_owned)
            .map_err(|_| ApiError::BadRequest("Invalid Idempotency-Key")))
        .transpose()?;
    if idempotency.as_ref().is_some_and(|s| s.trim().chars().count() > 128) {
        return Err(ApiError::BadRequest("Idempotency-Key exceeds 128 characters"));
    }
    let first = multipart.next_field().await.map_err(|error| {
        if error.status() == StatusCode::PAYLOAD_TOO_LARGE {
            ApiError::ImportTooLarge
        } else {
            ApiError::InvalidInput("Invalid import multipart data")
        }
    })?.ok_or(ApiError::InvalidInput("Missing import file"))?;
    if first.name() != Some("file") {
        return Err(ApiError::InvalidInput("Expected one multipart file field"));
    }
    let filename = first.file_name()
        .ok_or(ApiError::InvalidInput("Missing imported file name"))?.to_owned();
    let data = first.bytes().await.map_err(|error| {
        if error.status() == StatusCode::PAYLOAD_TOO_LARGE {
            ApiError::ImportTooLarge
        } else {
            ApiError::InvalidInput("Invalid import multipart data")
        }
    })?;
    if multipart.next_field().await.map_err(|error| {
        if error.status() == StatusCode::PAYLOAD_TOO_LARGE {
            ApiError::ImportTooLarge
        } else {
            ApiError::InvalidInput("Invalid import multipart data")
        }
    })?.is_some() {
        return Err(ApiError::InvalidInput("Expected one multipart file field"));
    }
    let mut response = secured_write(state, headers, move |db, actor, _| {
        import_stage::stage(db, actor, &filename, &data, idempotency.as_deref())
            .map_err(map_error)
    }).await?;
    *response.status_mut() = StatusCode::CREATED;
    Ok(response)
}

pub async fn read(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(stage_id): Path<String>,
) -> Result<Response, ApiError> {
    secured_read(state, headers, move |db, actor, _| {
        import_stage::get(db, actor, &stage_id).map_err(map_error)
    }).await
}

pub async fn cancel(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(stage_id): Path<String>,
) -> Result<Response, ApiError> {
    secured_write(state, headers, move |db, actor, _| {
        import_stage::cancel(db, actor, &stage_id).map_err(map_error)
    }).await
}
