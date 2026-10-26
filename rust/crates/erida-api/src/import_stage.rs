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

/// Read preserved book prefaces even when experimental Rust writes are disabled.
pub async fn prefixes(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(book_id): Path<String>,
) -> Result<Response, ApiError> {
    secured_read(state, headers, move |db, _, _| {
        match import_stage::prefixes(db, &book_id) {
            Ok(result) => Ok(result),
            Err(StageError::Missing) => Err(ApiError::BookNotFound),
            Err(error) => Err(map_error(error)),
        }
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

/// M1p: guarded native batch confirmation. The existing Python schema is
/// authoritative; only the proved TXT H1/paragraph subset is writeable.
pub async fn confirm(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(stage_id): Path<String>,
    axum::Json(body): axum::Json<serde_json::Value>,
) -> Result<Response, ApiError> {
    secured_write(state, headers, move |db, actor, auth| {
        let mut body = body;
        if body.get("book_mode").and_then(serde_json::Value::as_str) == Some("create") {
            let owner = body.get("owner_member_id")
                .and_then(serde_json::Value::as_str)
                .filter(|s| !s.is_empty()).unwrap_or(actor).to_owned();
            if !erida_auth::is_active_moderator(&owner, db, auth)? {
                return Err(ApiError::InvalidOwner);
            }
            if auth.role(actor) != "admin" && owner != actor {
                return Err(ApiError::ForbiddenAssignment);
            }
            body.as_object_mut().ok_or(ApiError::InvalidInput(
                "Invalid import confirmation body"
            ))?.insert("owner_member_id".into(), serde_json::json!(owner));
        } else if body.get("book_mode").and_then(serde_json::Value::as_str) == Some("existing")
            && body.get("owner_member_id").is_some_and(|v| !v.is_null())
        {
            return Err(ApiError::InvalidInput(
                "Existing book owner cannot change during import"
            ));
        }
        erida_library::import_confirm::confirm(db, actor, &stage_id, body)
            .map_err(map_error)
    }).await
}
