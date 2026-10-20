//! Experimental HTTP job writes; can only run against an explicitly scoped
//! disposable test SQLite database, never automatically on a production DB.
use super::{browser_token, private_response, ApiError, AppState};
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::Response;
use axum::Json;
use erida_store::{self, WriteError};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};

fn default_source_language() -> String {
    "auto".into()
}
fn default_target_language() -> String {
    "Russian".into()
}
fn default_attempts() -> i64 {
    3
}
fn default_authority() -> String {
    "READY".into()
}
fn default_transport() -> String {
    "html_v0".into()
}

#[derive(Deserialize)]
pub struct SubmitChapter {
    chapter_id: String,
    source_html: String,
    #[serde(default = "default_source_language")]
    source_language: String,
    #[serde(default = "default_target_language")]
    target_language: String,
    #[serde(default)]
    glossary: BTreeMap<String, Value>,
    #[serde(default)]
    context: String,
    #[serde(default)]
    chapter_path: String,
    #[serde(default = "default_attempts")]
    max_attempts: i64,
    #[serde(default)]
    glossary_versions: BTreeMap<String, Vec<BTreeMap<String, Value>>>,
    #[serde(default = "default_authority")]
    glossary_authority: String,
    #[serde(default)]
    chapter_context_snapshot: Option<Map<String, Value>>,
    #[serde(default)]
    predecessor: Option<Map<String, Value>>,
    #[serde(default)]
    style_instructions: String,
    #[serde(default = "default_transport")]
    transport: String,
}

fn valid_length(value: &str, min: usize, max: usize) -> bool {
    let len = value.chars().count();
    len >= min && len <= max
}

impl SubmitChapter {
    /// Translate validated request fields into the EXACT QueueStore.JobSpec
    /// normalized shape including the internal legacy defaults.
    fn normalize(self) -> Result<Value, ApiError> {
        for (value, min, max) in [
            (&self.chapter_id, 1, 255),
            (&self.source_html, 1, 2_000_000),
            (&self.source_language, 0, 64),
            (&self.target_language, 1, 64),
            (&self.context, 0, 100_000),
            (&self.chapter_path, 0, 1024),
            (&self.style_instructions, 0, 10_000),
        ] {
            if !valid_length(value, min, max) {
                return Err(ApiError::InvalidInput("Invalid chapter submission"));
            }
        }
        if !(1..=10).contains(&self.max_attempts)
            || !matches!(self.transport.as_str(), "html_v0" | "atoms_v1")
            || !matches!(
                self.glossary_authority.as_str(),
                "READY" | "READY_WITH_WARNINGS" | "EMPTY_EXPECTED" | "UNAVAILABLE" | "DISABLED"
            )
        {
            return Err(ApiError::InvalidInput("Invalid chapter submission"));
        }
        if self.glossary.values().any(|value| !(value.is_string() || value.is_object())) {
            return Err(ApiError::InvalidInput("Invalid glossary entry"));
        }
        if self.chapter_id.trim().is_empty() || self.source_html.trim().is_empty() {
            return Err(ApiError::BadRequest(
                "actor_id, chapter_id and nonempty source_html required",
            ));
        }
        validate_scoped_glossary(
            &self.glossary_versions,
            &self.chapter_path,
            &self.glossary_authority,
        )?;
        Ok(json!({
            "chapter_id": self.chapter_id,
            "source_html": self.source_html,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "glossary": self.glossary,
            "context": self.context,
            "chapter_path": self.chapter_path,
            "max_attempts": self.max_attempts,
            "glossary_versions": self.glossary_versions,
            "glossary_authority": self.glossary_authority,
            "chapter_context_snapshot": self.chapter_context_snapshot,
            "predecessor": self.predecessor,
            "style_instructions": self.style_instructions,
            "book_id": "",
            "book_chapter_revision": 0,
            "book_glossary_revision": 0,
            "transport": self.transport,
        }))
    }
}

/// Equivalent conflict detection to Python scoped_glossary: compare only
/// rus/note fields for exact chapter scope, never silently choose an override.
fn validate_scoped_glossary(
    versions: &BTreeMap<String, Vec<BTreeMap<String, Value>>>,
    chapter_path: &str,
    authority: &str,
) -> Result<(), ApiError> {
    if !matches!(authority, "READY" | "READY_WITH_WARNINGS") || chapter_path.is_empty() {
        return Ok(());
    }
    let path = chapter_path.replace('\\', "/");
    for rules in versions.values() {
        let mut distinct = BTreeSet::new();
        for rule in rules {
            let is_matching = rule.get("scope").and_then(Value::as_array).is_some_and(|paths| {
                paths.iter().any(|path_item| {
                    path_item
                        .as_str()
                        .is_some_and(|item| item.replace('\\', "/") == path)
                })
            });
            if !is_matching {
                continue;
            }
            if let Some(map) = rule.get("override").and_then(Value::as_object) {
                let reduced: BTreeMap<_, _> = map
                    .iter()
                    .filter(|(key, _)| *key == "rus" || *key == "note")
                    .collect();
                let canonical = serde_json::to_string(&reduced)
                    .map_err(|_| ApiError::InvalidInput("Invalid glossary overrides"))?;
                distinct.insert(canonical);
            }
        }
        if distinct.len() > 1 {
            return Err(ApiError::GlossaryConflict);
        }
    }
    Ok(())
}

fn map_write_failure(error: WriteError) -> ApiError {
    match error {
        WriteError::IdempotencyConflict => ApiError::Conflict,
        WriteError::InvalidInput(message) => ApiError::BadRequest(message),
        WriteError::Sqlite(_) | WriteError::Storage(_) => ApiError::Unavailable,
    }
}

pub(super) async fn secured_write<F>(
    state: AppState,
    headers: HeaderMap,
    operation: F,
) -> Result<Response, ApiError>
where
    F: FnOnce(&mut rusqlite::Connection, &str) -> Result<Value, ApiError> + Send + 'static,
{
    if !state.test_writes_enabled {
        return Err(ApiError::Unavailable);
    }
    let output = tokio::task::spawn_blocking(move || {
        let mut db = erida_store::open_disposable_writer(state.db_path.as_path())
            .map_err(map_write_failure)?;
        let key = headers
            .get("x-api-key")
            .map(|value| value.to_str().unwrap_or(""));
        let token = browser_token(&headers);
        let actor = erida_auth::authenticate(key, token.as_deref(), &db, &state.auth)?;
        // Unlike GET, cookie-authenticated mutation requires the same HMAC
        // CSRF proof that FastAPI verifies on every non-safe method.
        if key.is_none() {
            let proof = headers
                .get("x-readera-csrf")
                .and_then(|value| value.to_str().ok())
                .unwrap_or("");
            if !token.as_deref().is_some_and(|cookie| erida_auth::csrf_matches(cookie, proof)) {
                return Err(ApiError::Forbidden);
            }
        }
        operation(&mut db, &actor)
    })
    .await
    .map_err(|_| ApiError::Unavailable)??;
    Ok(private_response(output))
}

pub async fn submit(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<SubmitChapter>,
) -> Result<Response, ApiError> {
    let normalized = body.normalize()?;
    let key = headers
        .get("idempotency-key")
        .map(|value| value.to_str().unwrap_or("").to_owned());
    let mut response = secured_write(state, headers, move |db, actor| {
        erida_store::enqueue(db, actor, &normalized, key.as_deref()).map_err(map_write_failure)
    })
    .await?;
    *response.status_mut() = StatusCode::CREATED;
    Ok(response)
}

pub async fn cancel(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(job_id): Path<String>,
) -> Result<Response, ApiError> {
    secured_write(state, headers, move |db, _actor| {
        erida_store::request_cancel(db, &job_id)
            .map_err(map_write_failure)?
            .ok_or(ApiError::NotFound)
    })
    .await
}
