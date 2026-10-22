//! M1g: browser key login and logout on disposable SQLite, plus cookie session GET.
//! TCP peer is used for throttling. Forwarded-IP headers are never trusted.
//! Nginx/client-IP parity remains a release blocker: writes are test-only.
use super::{browser_token, ApiError, AppState};
use axum::body::Body;
use axum::extract::{ConnectInfo, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::net::SocketAddr;
use std::time::{SystemTime, UNIX_EPOCH};

const WINDOW: f64 = 900.0;
const LOCK: f64 = 900.0;
const MAX_ATTEMPTS: i64 = 30;
const LOCK_FAILURES: i64 = 8;

#[derive(Deserialize)]
pub struct BrowserLogin {
    api_key: String,
}

fn now() -> Result<f64, ApiError> {
    SystemTime::now().duration_since(UNIX_EPOCH)
        .map(|v| v.as_secs_f64()).map_err(|_| ApiError::Unavailable)
}
fn sha(input: &str) -> String {
    hex::encode(Sha256::digest(input.as_bytes()))
}
fn throttle_key(peer: &str) -> String {
    sha(&format!("readera:login:ip:{peer}"))
}

fn throttle_begin(db: &mut Connection, peer: &str) -> Result<(), ApiError> {
    let stamp = now()?;
    let key = throttle_key(peer);
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| ApiError::Unavailable)?;
    tx.execute("DELETE FROM browser_login_rate WHERE updated_at<?1",
               params![stamp - 86400.0]).map_err(|_| ApiError::Unavailable)?;
    let previous: Option<(f64, i64, i64, f64)> = tx.query_row(
        "SELECT window_started,attempts,failures,locked_until FROM browser_login_rate \
         WHERE peer_sha256=?1", params![key],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
    ).optional().map_err(|_| ApiError::Unavailable)?;
    if let Some((_, _, _, until)) = previous {
        if until > stamp {
            return Err(ApiError::LoginRateLimit((until - stamp).ceil().max(1.0) as u64));
        }
    }
    let current_window = previous.is_some_and(|r| stamp - r.0 < WINDOW);
    let started = if current_window { previous.map_or(stamp, |r| r.0) } else { stamp };
    let attempts = if current_window { previous.map_or(0, |r| r.1) } else { 0 };
    let failures = if current_window { previous.map_or(0, |r| r.2) } else { 0 };
    if attempts >= MAX_ATTEMPTS {
        return Err(ApiError::LoginRateLimit(
            (WINDOW - (stamp - started)).ceil().max(1.0) as u64,
        ));
    }
    tx.execute(
        "INSERT INTO browser_login_rate \
         (peer_sha256,window_started,attempts,failures,locked_until,updated_at) \
         VALUES(?1,?2,?3,?4,0,?5) ON CONFLICT(peer_sha256) DO UPDATE SET \
         window_started=excluded.window_started,attempts=excluded.attempts,\
         failures=excluded.failures,locked_until=0,updated_at=excluded.updated_at",
        params![key, started, attempts + 1, failures, stamp],
    ).map_err(|_| ApiError::Unavailable)?;
    tx.commit().map_err(|_| ApiError::Unavailable)?;
    Ok(())
}

fn throttle_result(db: &mut Connection, peer: &str, valid: bool)
    -> Result<(), ApiError>
{
    let stamp = now()?;
    let key = throttle_key(peer);
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| ApiError::Unavailable)?;
    if valid {
        tx.execute(
            "UPDATE browser_login_rate SET failures=0,locked_until=0,updated_at=?1 \
             WHERE peer_sha256=?2",
            params![stamp, key],
        ).map_err(|_| ApiError::Unavailable)?;
    } else {
        let count: i64 = tx.query_row(
            "SELECT failures FROM browser_login_rate WHERE peer_sha256=?1",
            params![key], |row| row.get(0),
        ).map_err(|_| ApiError::Unavailable)?;
        let next = count + 1;
        tx.execute(
            "UPDATE browser_login_rate SET failures=?1,locked_until=?2,updated_at=?3 \
             WHERE peer_sha256=?4",
            params![next, if next >= LOCK_FAILURES { stamp + LOCK } else { 0.0 },
                    stamp, key],
        ).map_err(|_| ApiError::Unavailable)?;
    }
    tx.commit().map_err(|_| ApiError::Unavailable)?;
    Ok(())
}

fn issue(db: &mut Connection, member: &str, key: &str, ttl: i64)
    -> Result<String, ApiError>
{
    let mut secret = [0u8; 32];
    getrandom::fill(&mut secret).map_err(|_| ApiError::Unavailable)?;
    let raw = format!("rs_{}", URL_SAFE_NO_PAD.encode(secret));
    let stamp = now()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| ApiError::Unavailable)?;
    tx.execute(
        "DELETE FROM team_browser_sessions WHERE expires_at<=?1 OR revoked_at IS NOT NULL",
        params![stamp],
    ).map_err(|_| ApiError::Unavailable)?;
    tx.execute(
        "INSERT INTO team_browser_sessions \
         (token_sha256,member_id,credential_sha256,auth_kind,created_at,expires_at) \
         VALUES(?1,?2,?3,'team_key',?4,?5)",
        params![sha(&raw), member, sha(key), stamp, stamp + ttl as f64],
    ).map_err(|_| ApiError::Unavailable)?;
    tx.commit().map_err(|_| ApiError::Unavailable)?;
    Ok(raw)
}

fn security_headers(response: &mut Response) {
    let h = response.headers_mut();
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("private, no-store"));
    h.insert(header::PRAGMA, HeaderValue::from_static("no-cache"));
    h.insert(header::REFERRER_POLICY, HeaderValue::from_static("no-referrer"));
}

fn payload(member: &str, role: &str, token: &str) -> Result<Value, ApiError> {
    let csrf = erida_auth::csrf_for_cookie(token).ok_or(ApiError::Unavailable)?;
    Ok(json!({
        "member_id": member, "auth_kind": "team_key",
        "must_change_password": false, "role": role, "csrf_token": csrf,
    }))
}
fn cookie(token: &str, ttl: i64, secure: bool) -> String {
    format!("readera_session={token}; Max-Age={ttl}; Path=/; SameSite=strict; HttpOnly{}",
            if secure { "; Secure" } else { "" })
}
fn deleted_cookie(secure: bool) -> String {
    format!(
        "readera_session=\"\"; expires=Thu, 01 Jan 1970 00:00:00 GMT; \
         Max-Age=0; Path=/; SameSite=strict; HttpOnly{}",
        if secure { "; Secure" } else { "" },
    )
}

/// GET /v1/session authorizes only the cookie, even if an API key is present.
pub async fn read(
    State(state): State<AppState>, headers: HeaderMap,
) -> Result<Response, ApiError> {
    let raw = browser_token(&headers).unwrap_or_default();
    let body = tokio::task::spawn_blocking(move || {
        let db = erida_store::open_read_only(state.db_path.as_path())
            .map_err(|_| ApiError::Unavailable)?;
        let member = erida_auth::authenticate(None, Some(&raw), &db, &state.auth)
            .map_err(|error| match error {
                erida_auth::AuthFailure::Storage => ApiError::Unavailable,
                _ => ApiError::SessionRequired,
            })?;
        payload(&member, state.auth.role(&member), &raw)
    }).await.map_err(|_| ApiError::Unavailable)??;
    let mut response = (StatusCode::OK, Json(body)).into_response();
    security_headers(&mut response);
    Ok(response)
}

/// POST login is unavailable unless the existing double disposable-write gate
/// is enabled. Per-peer SQLite throttle is charged before token validation.
pub async fn login(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap, Json(body): Json<BrowserLogin>,
) -> Result<Response, ApiError> {
    if !state.test_writes_enabled { return Err(ApiError::Unavailable); }
    if body.api_key.is_empty() || body.api_key.chars().count() > 512 {
        return Err(ApiError::InvalidInput("Invalid browser login key"));
    }
    if headers.get("sec-fetch-site").and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.eq_ignore_ascii_case("cross-site"))
    {
        return Err(ApiError::CrossSiteLogin);
    }
    // For reverse-proxied login, require an explicitly trusted loopback
    // proxy header before any key validation or SQLite throttle mutation.
    let remote = super::peer_identity::login_client_ip(
        peer, &headers, state.trust_loopback_proxy_client_ip,
    )?;
    let (raw, details, ttl, secure) = tokio::task::spawn_blocking(move || {
        let mut db = erida_store::open_disposable_writer(state.db_path.as_path())
            .map_err(|_| ApiError::Unavailable)?;
        throttle_begin(&mut db, &remote)?;
        let member = match erida_auth::authenticate(
            Some(&body.api_key), None, &db, &state.auth,
        ) {
            Ok(member) => member,
            Err(erida_auth::AuthFailure::InvalidApiKey) => {
                throttle_result(&mut db, &remote, false)?;
                return Err(ApiError::LoginInvalidKey);
            }
            Err(_) => return Err(ApiError::Unavailable),
        };
        throttle_result(&mut db, &remote, true)?;
        let raw = issue(
            &mut db, &member, &body.api_key, state.browser_session_ttl_seconds,
        )?;
        let details = payload(&member, state.auth.role(&member), &raw)?;
        Ok((raw, details, state.browser_session_ttl_seconds,
            state.browser_session_secure))
    }).await.map_err(|_| ApiError::Unavailable)??;
    let mut response = (StatusCode::CREATED, Json(details)).into_response();
    security_headers(&mut response);
    response.headers_mut().insert(header::SET_COOKIE,
        HeaderValue::from_str(&cookie(&raw, ttl, secure))
            .map_err(|_| ApiError::Unavailable)?);
    Ok(response)
}

/// Logout is idempotent for expired/missing cookies. An active cookie needs
/// a correct session-derived CSRF proof; all writes are test-disposable only.
pub async fn logout(
    State(state): State<AppState>, headers: HeaderMap,
) -> Result<Response, ApiError> {
    if !state.test_writes_enabled { return Err(ApiError::Unavailable); }
    let raw = browser_token(&headers).unwrap_or_default();
    let proof = headers.get("x-readera-csrf")
        .and_then(|v| v.to_str().ok()).unwrap_or("").to_owned();
    let secure = state.browser_session_secure;
    tokio::task::spawn_blocking(move || {
        let db = erida_store::open_disposable_writer(state.db_path.as_path())
            .map_err(|_| ApiError::Unavailable)?;
        match erida_auth::authenticate(None, Some(&raw), &db, &state.auth) {
            Ok(_) if !erida_auth::csrf_matches(&raw, &proof) => {
                return Err(ApiError::Forbidden);
            }
            Err(erida_auth::AuthFailure::Storage) => {
                return Err(ApiError::Unavailable);
            }
            _ => {}
        }
        if raw.starts_with("rs_") && raw.len() <= 128 {
            db.execute(
                "UPDATE team_browser_sessions SET revoked_at=?1 WHERE token_sha256=?2",
                params![now()?, sha(&raw)],
            ).map_err(|_| ApiError::Unavailable)?;
        }
        Ok(())
    }).await.map_err(|_| ApiError::Unavailable)??;
    let mut response = Response::new(Body::empty());
    *response.status_mut() = StatusCode::NO_CONTENT;
    security_headers(&mut response);
    response.headers_mut().insert(header::SET_COOKIE,
        HeaderValue::from_str(&deleted_cookie(secure))
            .map_err(|_| ApiError::Unavailable)?);
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn throttle_key_is_domain_separated() {
        let key = throttle_key("127.0.0.1");
        assert_eq!(key.len(), 64);
        assert!(!key.contains("127.0.0.1"));
        assert_ne!(key, sha("127.0.0.1"));
    }
    #[test]
    fn cookies_are_http_only() {
        let raw = cookie("rs_example", 43200, true);
        assert!(raw.contains("HttpOnly"));
        assert!(raw.contains("SameSite=strict"));
        assert!(raw.contains("; Secure"));
        assert!(!deleted_cookie(false).contains("; Secure"));
        assert_eq!(erida_auth::csrf_for_cookie("rs_example").unwrap().len(), 64);
    }
}
