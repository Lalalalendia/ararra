//! Experimental Rust read-only API slice. This cannot replace ReadEra yet:
//! all mutations, book routes and Gemini translation remain Python-owned.
use axum::extract::{DefaultBodyLimit, Path, Query, State};

mod write;
mod book;
mod chapter_write;
mod accepted;
mod editorial;
mod session;
mod peer_identity;
mod admin_credentials;
mod book_export;
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
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
    test_writes_enabled: bool,
    browser_session_ttl_seconds: i64,
    browser_session_secure: bool,
    trust_loopback_proxy_client_ip: bool,
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
        let browser_session_ttl_seconds: i64 = std::env::var(
            "CHAPTERA_BROWSER_SESSION_TTL_SECONDS"
        ).unwrap_or_else(|_| "43200".to_owned()).parse()
            .map_err(|_| "Invalid browser session TTL")?;
        if !(300..=604800).contains(&browser_session_ttl_seconds) {
            return Err("Invalid browser session TTL".into());
        }
        let browser_session_secure = matches!(
            std::env::var("CHAPTERA_BROWSER_SESSION_SECURE")
                .unwrap_or_else(|_| "1".to_owned()).trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        );
        // Explicit opt-in: the immediate peer must still be loopback, and
        // Nginx must overwrite X-ReadEra-Client-IP (verified separately).
        let trust_loopback_proxy_client_ip = match std::env::var(
            "ERIDA_RUST_TRUST_LOOPBACK_PROXY_CLIENT_IP"
        ) {
            Ok(value) if value == "1" => true,
            Ok(value) if value == "0" => false,
            Err(std::env::VarError::NotPresent) => false,
            _ => return Err("Invalid Rust trusted loopback proxy setting".into()),
        };
        let test_writes_enabled = std::env::var("ERIDA_RUST_TEST_WRITES")
            .is_ok_and(|value| value == "1");
        if test_writes_enabled {
            // Two independent explicit gates; do not permit a symlinked or
            // production SQLite DB to be used by an experimental writer.
            let declared_root = std::env::var("ERIDA_RUST_DISPOSABLE_ROOT")
                .map_err(|_| "Disposable root must be explicitly configured")?;
            let root = PathBuf::from(declared_root).canonicalize()
                .map_err(|_| "Disposable root cannot be resolved")?;
            let temp_root = std::env::temp_dir().canonicalize()
                .map_err(|_| "System temporary root unavailable")?;
            let actual = path.canonicalize()
                .map_err(|_| "Disposable SQLite path cannot be resolved")?;
            if !root.starts_with(&temp_root)
                || actual.parent() != Some(root.as_path())
                || !actual.is_file()
            {
                return Err("Rust job writer requires a disposable SQLite DB".into());
            }
        }
        let con = erida_store::open_read_only(&path)
            .map_err(|_| "Read-only SQLite connection unavailable")?;
        erida_store::check_schema(&con)
            .map_err(|_| "Existing ReadEra database schema is incompatible")?;
        Ok(Self {
            db_path: Arc::new(path),
            auth: Arc::new(config),
            test_writes_enabled,
            browser_session_ttl_seconds,
            browser_session_secure,
            trust_loopback_proxy_client_ip,
        })
    }
}

#[derive(Debug)]
pub enum ApiError {
    InvalidApiKey,
    InvalidSession,
    NotFound,
    Unavailable,
    Forbidden,
    Conflict,
    GlossaryConflict,
    DetailedGlossaryConflict(String),
    InvalidInput(&'static str),
    BadRequest(&'static str),
    BookNotFound,
    ChapterNotFound,
    AcceptedRevisionNotFound,
    ExportNotReady(Value),
    SessionRequired,
    LoginInvalidKey,
    CrossSiteLogin,
    LoginRateLimit(u64),
    TrustedClientIpUnavailable,
    ReviewNoteNotFound,
    ModeratorNotFound,
    BookConflict(&'static str),
    InvalidOwner,
    ForbiddenAssignment,
    ForbiddenAdmin,
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
        if let ApiError::LoginRateLimit(after) = &self {
            let mut response = (
                StatusCode::TOO_MANY_REQUESTS,
                [(header::CACHE_CONTROL, "private, no-store"),
                 (header::PRAGMA, "no-cache"),
                 (header::REFERRER_POLICY, "no-referrer")],
                Json(json!({"detail": "Слишком много попыток входа"})),
            ).into_response();
            response.headers_mut().insert(
                header::RETRY_AFTER,
                axum::http::HeaderValue::from_str(&after.to_string())
                    .unwrap_or(axum::http::HeaderValue::from_static("900")),
            );
            return response;
        }
        if let ApiError::ExportNotReady(ref preview) = self {
            return (
                StatusCode::CONFLICT,
                [(header::CACHE_CONTROL, "no-store, private")],
                Json(json!({
                    "detail": {
                        "message": "Book export is incomplete or changed after preview",
                        "preview": preview,
                    },
                })),
            ).into_response();
        }
        let (status, detail): (StatusCode, &str) = match &self {
            ApiError::InvalidApiKey => (StatusCode::UNAUTHORIZED, "Valid team API token required"),
            ApiError::InvalidSession => {
                (StatusCode::UNAUTHORIZED, "Valid team authentication required")
            }
            ApiError::NotFound => (StatusCode::NOT_FOUND, "Job not found"),
            ApiError::Unavailable => {
                (StatusCode::SERVICE_UNAVAILABLE, "Datastore unavailable")
            }
            ApiError::Forbidden => (StatusCode::FORBIDDEN, "CSRF proof required"),
            ApiError::Conflict => (StatusCode::CONFLICT, "Idempotency key refers to different input"),
            ApiError::GlossaryConflict => (StatusCode::UNPROCESSABLE_ENTITY, "Conflicting glossary versions"),
            ApiError::DetailedGlossaryConflict(message) => (StatusCode::UNPROCESSABLE_ENTITY, message.as_str()),
            ApiError::InvalidInput(reason) => (StatusCode::UNPROCESSABLE_ENTITY, *reason),
            ApiError::BadRequest(reason) => (StatusCode::BAD_REQUEST, *reason),
            ApiError::BookNotFound => (StatusCode::NOT_FOUND, "Book not found"),
            ApiError::ChapterNotFound => (StatusCode::NOT_FOUND, "Chapter not found"),
            ApiError::AcceptedRevisionNotFound => (StatusCode::NOT_FOUND, "Accepted revision not found"),
            ApiError::ReviewNoteNotFound => (StatusCode::NOT_FOUND, "Review note not found"),
            ApiError::ModeratorNotFound => (StatusCode::NOT_FOUND, "Moderator not found"),
            ApiError::SessionRequired => (StatusCode::UNAUTHORIZED, "Действующая сессия необходима"),
            ApiError::LoginInvalidKey => (StatusCode::UNAUTHORIZED, "Неверный ключ участника"),
            ApiError::CrossSiteLogin => (StatusCode::FORBIDDEN, "Cross-site login forbidden"),
            ApiError::LoginRateLimit(_) => (StatusCode::TOO_MANY_REQUESTS, "Слишком много попыток входа"),
            ApiError::TrustedClientIpUnavailable => (StatusCode::BAD_REQUEST, "Trusted client IP unavailable"),
            // Early-response branch above handles its structured body; keep match exhaustive.
            ApiError::ExportNotReady(_) => (StatusCode::CONFLICT, "Book export is incomplete or changed after preview"),
            ApiError::BookConflict(reason) => (StatusCode::CONFLICT, *reason),
            ApiError::InvalidOwner => (StatusCode::UNPROCESSABLE_ENTITY, "Book owner must be an active moderator"),
            ApiError::ForbiddenAssignment => (StatusCode::FORBIDDEN, "Only administrator can assign another owner"),
            ApiError::ForbiddenAdmin => (StatusCode::FORBIDDEN, "Administrator role required"),
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

/// Reuse the existing per-request member authentication for both JSON reads
/// and binary book downloads. Never infer trust from prior preview receipts.
async fn authenticated_read<T, F>(
    state: AppState,
    headers: HeaderMap,
    operation: F,
) -> Result<T, ApiError>
where
    T: Send + 'static,
    F: FnOnce(&Connection, &str, &AuthConfig) -> Result<T, ApiError> + Send + 'static,
{
    tokio::task::spawn_blocking(move || {
        let db = erida_store::open_read_only(state.db_path.as_path())?;
        // Header existence, even an empty/invalid value, blocks cookie fallback.
        let key = headers
            .get("x-api-key")
            .map(|header| header.to_str().unwrap_or(""));
        let cookie = browser_token(&headers);
        let actor = erida_auth::authenticate(key, cookie.as_deref(), &db, &state.auth)?;
        operation(&db, &actor, &state.auth)
    })
    .await
    .map_err(|_| ApiError::Unavailable)?
}

async fn secured_read<F>(
    state: AppState,
    headers: HeaderMap,
    operation: F,
) -> Result<Response, ApiError>
where
    F: FnOnce(&Connection, &str, &AuthConfig) -> Result<Value, ApiError> + Send + 'static,
{
    let payload = authenticated_read(state, headers, operation).await?;
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

/// Administrator-only read of team membership; no token or digest values.
async fn admin_members(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    secured_read(state, headers, |db, actor, auth| {
        if auth.admin_member_id.is_empty() || actor != auth.admin_member_id {
            return Err(ApiError::ForbiddenAdmin);
        }
        erida_auth::list_members(db, auth).map_err(|_| ApiError::Unavailable)
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
    let mut app = Router::new()
        .route("/healthz", get(health))
        .route("/v1/me", get(me))
        .route("/v1/session", get(session::read))
        .route("/v1/admin/members", get(admin_members))
        .route("/v1/jobs", get(jobs))
        .route("/v1/jobs/{job_id}", get(job))
        .route("/v1/jobs/{job_id}/events", get(job_events))
        .route("/v1/books", get(book::books))
        .route("/v1/books/{book_id}", get(book::book))
        .route("/v1/books/{book_id}/export/preview", get(book_export::preview))
        .route("/v1/books/{book_id}/export", get(book_export::download))
        .route("/v1/books/{book_id}/chapters", get(book::chapters))
        .route("/v1/books/{book_id}/chapters/{chapter_id}", get(book::chapter))
        .route("/v1/books/{book_id}/chapters/{chapter_id}/draft", get(editorial::read_draft))
        .route("/v1/books/{book_id}/chapters/{chapter_id}/notes", get(editorial::read_notes))
        .route("/v1/books/{book_id}/chapters/{chapter_id}/accepted-revisions", get(editorial::read_accepted_history))
        .route("/v1/books/{book_id}/chapters/{chapter_id}/accepted-revisions/{revision}", get(editorial::read_accepted_version));
    if state.test_writes_enabled {
        app = app
            .route("/v1/session", post(session::login))
            .route("/v1/session/logout", post(session::logout))
            .route("/v1/admin/members", post(admin_credentials::issue))
            .route("/v1/admin/members/{member_id}/rotate", post(admin_credentials::rotate))
            .route("/v1/admin/members/{member_id}/disable", post(admin_credentials::disable))
            .route("/v1/jobs", post(write::submit))
            .route("/v1/jobs/{job_id}/cancel", post(write::cancel))
            .route("/v1/books", post(book::create))
            .route("/v1/books/{book_id}/owner", axum::routing::put(book::reassign))
            .route("/v1/books/{book_id}/glossary", axum::routing::put(book::save_glossary))
            .route("/v1/books/{book_id}/chapters/{chapter_id}",
                   axum::routing::put(chapter_write::save_chapter))
            .route("/v1/books/{book_id}/chapters/{chapter_id}/accepted-translation",
                   axum::routing::put(accepted::accept))
            .route("/v1/books/{book_id}/chapters/{chapter_id}/draft",
                   axum::routing::put(editorial::save_draft))
            .route("/v1/books/{book_id}/chapters/{chapter_id}/restore-accepted",
                   post(editorial::restore_accepted))
            .route("/v1/books/{book_id}/chapters/{chapter_id}/notes",
                   post(editorial::create_note))
            .route("/v1/books/{book_id}/chapters/{chapter_id}/notes/{note_id}",
                   axum::routing::patch(editorial::update_note))
            .layer(DefaultBodyLimit::max(8 * 1024 * 1024));
    }
    app.with_state(state)
}
