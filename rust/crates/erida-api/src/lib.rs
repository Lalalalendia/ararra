//! Experimental Rust read-only API slice. This cannot replace ReadEra yet:
//! all mutations, book routes and Gemini translation remain Python-owned.
use axum::extract::{Path, Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use erida_auth::{AuthConfig, AuthFailure};
use erida_store::{self, StoreError};
use rusqlite::Connection;
use serde::Deserialize;
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::Arc;

#[derive(Clone)]
pub struct AppState {
    db_path: Arc<PathBuf>,
    auth: Arc<AuthConfig>,
}

impl AppState {
    pub fn from_env() -> Result<Self, String> {
        let path = PathBuf::from(
            std::env::var("CHAPTERA_DB_PATH").unwrap_or_else(|_| "./data/jobs.sqlite3".into()),
        );
        if !path.is_file() {
            return Err("Existing SQLite database is required for Rust read-only mode".into());
        }
        let config = AuthConfig::from_env()?;
        let con = erida_store::open_read_only(&path)
            .map_err(|_| "Read-only SQLite connection unavailable")?;
        erida_store::check_schema(&con)
            .map_err(|_| "Existing ReadEra database schema is incompatible")?;
        Ok(Self {
            db_path: Arc::new(path),
            auth: Arc::new(config),
        })
    }
}

#[derive(Debug)]
pub enum ApiError {
    InvalidApiKey,
    InvalidSession,
    NotFound,
    Unavailable,
}

impl From<AuthFailure> for ApiError {
    fn from(reason: AuthFailure) -> Self {
        match reason {
            AuthFailure::InvalidApiKey => ApiError::InvalidApiKey,
            AuthFailure::InvalidSession => ApiError::InvalidSession,
            AuthFailure::Storage => ApiError::Unavailable,
        }
    }
}
impl From<StoreError> for ApiError {
    fn from(_: StoreError) -> Self {
        ApiError::Unavailable
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, detail) = match self {
            ApiError::InvalidApiKey => (StatusCode::UNAUTHORIZED, "Valid team API token required"),
            ApiError::InvalidSession => {
                (StatusCode::UNAUTHORIZED, "Valid team authentication required")
            }
            ApiError::NotFound => (StatusCode::NOT_FOUND, "Job not found"),
            ApiError::Unavailable => {
                (StatusCode::SERVICE_UNAVAILABLE, "Read-only datastore unavailable")
            }
        };
        (
            status,
            [(header::CACHE_CONTROL, "private, no-store")],
            Json(json!({"detail": detail})),
        )
            .into_response()
    }
}

fn private_response(value: Value) -> Response {
    (
        [
            (header::CACHE_CONTROL, "private, no-store"),
            (header::PRAGMA, "no-cache"),
            (header::REFERRER_POLICY, "no-referrer"),
            (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
        ],
        Json(value),
    )
        .into_response()
}

fn browser_token(headers: &HeaderMap) -> Option<String> {
    let raw = headers.get(header::COOKIE)?.to_str().ok()?;
    for field in raw.split(';') {
        if let Some((name, token)) = field.trim().split_once('=') {
            if name.trim() == "readera_session" {
                return Some(token.trim().to_owned());
            }
        }
    }
    None
}

async fn secured_read<F>(
    state: AppState,
    headers: HeaderMap,
    operation: F,
) -> Result<Response, ApiError>
where
    F: FnOnce(&Connection, &str, &AuthConfig) -> Result<Value, ApiError> + Send + 'static,
{
    let payload = tokio::task::spawn_blocking(move || {
        let db = erida_store::open_read_only(state.db_path.as_path())?;
        // Header existence, even an empty or invalid value, blocks cookie fallback.
        let key = headers
            .get("x-api-key")
            .map(|header| header.to_str().unwrap_or(""));
        let cookie = browser_token(&headers);
        let actor = erida_auth::authenticate(key, cookie.as_deref(), &db, &state.auth)?;
        operation(&db, &actor, &state.auth)
    })
    .await
    .map_err(|_| ApiError::Unavailable)??;
    Ok(private_response(payload))
}

async fn health() -> Json<Value> {
    Json(json!({"status": "ok"}))
}

async fn me(State(state): State<AppState>, headers: HeaderMap) -> Result<Response, ApiError> {
    secured_read(state, headers, |_, actor, auth| {
        Ok(json!({"member_id": actor, "role": auth.role(actor)}))
    })
    .await
}

#[derive(Deserialize, Default)]
struct JobQuery {
    limit: Option<i64>,
}

async fn jobs(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<JobQuery>,
) -> Result<Response, ApiError> {
    let limit = query.limit.unwrap_or(50);
    secured_read(state, headers, move |db, _, _| {
        erida_store::list_jobs(db, limit).map_err(Into::into)
    })
    .await
}

async fn job(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(job_id): Path<String>,
) -> Result<Response, ApiError> {
    secured_read(state, headers, move |db, _, _| {
        erida_store::get_job(db, &job_id)?
            .ok_or(ApiError::NotFound)
    })
    .await
}

async fn job_events(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(job_id): Path<String>,
) -> Result<Response, ApiError> {
    secured_read(state, headers, move |db, _, _| {
        if erida_store::get_job(db, &job_id)?.is_none() {
            return Err(ApiError::NotFound);
        }
        erida_store::events(db, &job_id).map_err(Into::into)
    })
    .await
}

pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(health))
        .route("/v1/me", get(me))
        .route("/v1/jobs", get(jobs))
        .route("/v1/jobs/{job_id}", get(job))
        .route("/v1/jobs/{job_id}/events", get(job_events))
        .with_state(state)
}
