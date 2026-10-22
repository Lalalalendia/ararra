//! M1h administrator key issuance, rotation and disable on disposable SQLite.
//! Existing Python contract is the oracle; no raw secrets are logged or read back.
use super::{write::secured_write, ApiError, AppState};
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::Response;
use axum::Json;
use erida_auth::management::{self, ManageError};
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InviteModerator {
    member_id: String,
}

fn map_error(error: ManageError) -> ApiError {
    match error {
        ManageError::Invalid(reason) => ApiError::InvalidInput(reason),
        ManageError::Conflict(reason) => ApiError::BookConflict(reason),
        ManageError::Missing("Moderator not found") => ApiError::ModeratorNotFound,
        ManageError::Missing(_) => ApiError::Unavailable,
        ManageError::Forbidden => ApiError::ForbiddenAdmin,
        ManageError::Storage(_) | ManageError::Entropy => ApiError::Unavailable,
    }
}

pub async fn issue(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<InviteModerator>,
) -> Result<Response, ApiError> {
    if body.member_id.is_empty() || body.member_id.chars().count() > 64 {
        return Err(ApiError::InvalidInput("Invalid moderator member ID"));
    }
    let mut response = secured_write(state, headers, move |db, actor, cfg| {
        if cfg.admin_member_id.is_empty() || actor != cfg.admin_member_id {
            return Err(ApiError::ForbiddenAdmin);
        }
        management::issue(db, &body.member_id, actor, cfg).map_err(map_error)
    }).await?;
    *response.status_mut() = StatusCode::CREATED;
    Ok(response)
}

pub async fn rotate(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(member_id): Path<String>,
) -> Result<Response, ApiError> {
    secured_write(state, headers, move |db, actor, cfg| {
        if cfg.admin_member_id.is_empty() || actor != cfg.admin_member_id {
            return Err(ApiError::ForbiddenAdmin);
        }
        management::rotate(db, &member_id, actor, cfg).map_err(map_error)
    }).await
}

pub async fn disable(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(member_id): Path<String>,
) -> Result<Response, ApiError> {
    secured_write(state, headers, move |db, actor, cfg| {
        if cfg.admin_member_id.is_empty() || actor != cfg.admin_member_id {
            return Err(ApiError::ForbiddenAdmin);
        }
        management::disable(db, &member_id, actor, cfg).map_err(map_error)
    }).await
}
