//! Read-only parity for ReadEra's existing team-key and browser-session auth.
//! Do not issue tokens, change sessions or relax revocation on the Rust side.
use rusqlite::{Connection, OptionalExtension};
use sha2::{Digest, Sha256};
use hmac::{Hmac, Mac};
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};
use subtle::ConstantTimeEq;

#[derive(Clone, Debug)]
pub struct AuthConfig {
    pub team_tokens: BTreeMap<String, String>,
    pub admin_member_id: String,
}

impl AuthConfig {
    pub fn new(
        team_tokens: BTreeMap<String, String>,
        admin_member_id: String,
    ) -> Result<Self, String> {
        if team_tokens
            .iter()
            .any(|(member, token)| member.trim().is_empty() || token.trim().is_empty())
        {
            return Err("Invalid team member configuration".to_owned());
        }
        if !admin_member_id.is_empty() && !team_tokens.contains_key(&admin_member_id) {
            return Err("Configured administrator does not exist".to_owned());
        }
        Ok(Self {
            team_tokens,
            admin_member_id,
        })
    }

    pub fn from_env() -> Result<Self, String> {
        let env_tokens = env::var("CHAPTERA_TEAM_TOKENS_JSON")
            .ok()
            .filter(|value| !value.is_empty());
        let protected_file = env::var("CREDENTIALS_DIRECTORY")
            .ok()
            .filter(|value| !value.is_empty())
            .map(|directory| Path::new(&directory).join("team_tokens_json"))
            .filter(|path| path.is_file());
        let raw = match (protected_file, env_tokens) {
            (Some(_), Some(_)) => return Err("Conflicting protected team credentials".into()),
            (Some(path), None) => fs::read_to_string(&path)
                .map_err(|_| "Cannot read protected team credentials")?,
            (None, Some(value)) => value,
            (None, None) => "{}".to_owned(),
        };
        let parsed: BTreeMap<String, String> =
            serde_json::from_str(&raw).map_err(|_| "Invalid team token JSON")?;
        Self::new(
            parsed,
            env::var("CHAPTERA_ADMIN_MEMBER_ID").unwrap_or_default(),
        )
    }

    pub fn role(&self, member: &str) -> &'static str {
        if !self.admin_member_id.is_empty() && member == self.admin_member_id {
            "admin"
        } else {
            "moderator"
        }
    }
}

#[derive(Debug)]
pub enum AuthFailure {
    InvalidApiKey,
    InvalidSession,
    Storage,
}

impl From<rusqlite::Error> for AuthFailure {
    fn from(_: rusqlite::Error) -> Self {
        Self::Storage
    }
}

/// Read-only administrator member projection. Mirrors Python
/// TeamCredentials.list_members exactly while excluding credential digests.
pub fn list_members(
    db: &Connection,
    cfg: &AuthConfig,
) -> Result<serde_json::Value, AuthFailure> {
    use serde_json::{json, Value};
    let mut members: BTreeMap<String, (bool, bool)> = BTreeMap::new();
    for member in cfg.team_tokens.keys() {
        members.insert(member.to_owned(), (true, false));
    }
    let mut stmt = db.prepare(
        "SELECT member_id,active,token_sha256 FROM team_access_credentials",
    )?;
    let mut rows = stmt.query([])?;
    while let Some(row) = rows.next()? {
        let member: String = row.get(0)?;
        let active: i64 = row.get(1)?;
        let token: Option<String> = row.get(2)?;
        members.insert(member, (active != 0, token.is_some_and(|s| !s.is_empty())));
    }
    let result: Vec<Value> = members.into_iter().map(|(member, (active, managed))| {
        let admin = !cfg.admin_member_id.is_empty() && member == cfg.admin_member_id;
        json!({
            "member_id": member,
            "role": if admin { "admin" } else { "moderator" },
            "status": if admin || active { "active" } else { "disabled" },
            "credential_source": if managed { "managed" } else { "configuration" },
        })
    }).collect();
    Ok(json!({"members": result}))
}

/// Purpose-separated CSRF proof must match the Python TeamSessions digest.
/// Mirrors TeamCredentials.is_active without revealing managed token hashes.
/// Moderator ownership is distinct from ordinary authentication.
pub fn is_active_moderator(
    member: &str, db: &Connection, cfg: &AuthConfig,
) -> Result<bool, AuthFailure> {
    if member.is_empty() || member == cfg.admin_member_id {
        return Ok(false);
    }
    let managed: Option<i64> = db
        .query_row(
            "SELECT active FROM team_access_credentials WHERE member_id=?1",
            [member],
            |row| row.get(0),
        )
        .optional()?;
    Ok(managed.map_or_else(|| cfg.team_tokens.contains_key(member), |active| active == 1))
}

pub fn csrf_matches(cookie: &str, provided: &str) -> bool {
    if cookie.is_empty() || provided.is_empty() {
        return false;
    }
    let Ok(mut mac) = Hmac::<Sha256>::new_from_slice(cookie.as_bytes()) else {
        return false;
    };
    mac.update(b"readera:browser-csrf:v1");
    let expected = hex::encode(mac.finalize().into_bytes());
    bool::from(expected.as_bytes().ct_eq(provided.as_bytes()))
}

fn sha256_hex(value: &str) -> String {
    hex::encode(Sha256::digest(value.as_bytes()))
}

fn constant_time_equal(a: &str, b: &str) -> bool {
    let x = Sha256::digest(a.as_bytes());
    let y = Sha256::digest(b.as_bytes());
    bool::from(x.ct_eq(&y))
}

/// X-API-Key always takes precedence over a cookie, including when invalid.
pub fn authenticate(
    api_key: Option<&str>,
    session_cookie: Option<&str>,
    db: &Connection,
    cfg: &AuthConfig,
) -> Result<String, AuthFailure> {
    if let Some(token) = api_key {
        return authenticate_key(token, db, cfg).ok_or(AuthFailure::InvalidApiKey);
    }
    authenticate_session(session_cookie.unwrap_or(""), db, cfg)
        .ok_or(AuthFailure::InvalidSession)
}

fn authenticate_key(token: &str, db: &Connection, cfg: &AuthConfig) -> Option<String> {
    if token.is_empty() || token.len() > 4096 {
        return None;
    }
    let mut static_id = None;
    for (member, configured) in &cfg.team_tokens {
        if constant_time_equal(token, configured) {
            static_id = Some(member.clone());
        }
    }
    if let Some(member) = static_id {
        if member == cfg.admin_member_id {
            return Some(member);
        }
        let override_present: Result<Option<i64>, _> = db
            .query_row(
                "SELECT active FROM team_access_credentials WHERE member_id=?1",
                [&member],
                |row| row.get(0),
            )
            .optional();
        match override_present {
            Ok(None) => return Some(member),
            Ok(Some(_)) => {} // The old static key was rotated or disabled.
            Err(_) => return None,
        }
    }
    let digest = sha256_hex(token);
    let managed: Option<(String, i64)> = db
        .query_row(
            "SELECT member_id,active FROM team_access_credentials WHERE token_sha256=?1",
            [&digest],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .ok()
        .flatten();
    managed.and_then(|(member, active)| if active == 1 { Some(member) } else { None })
}

fn authenticate_session(raw: &str, db: &Connection, cfg: &AuthConfig) -> Option<String> {
    if !raw.starts_with("rs_") || raw.len() > 128 {
        return None;
    }
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs_f64();
    let digest = sha256_hex(raw);
    let row: (String, String, String) = db
        .query_row(
            "SELECT member_id,credential_sha256,auth_kind FROM team_browser_sessions \
             WHERE token_sha256=?1 AND revoked_at IS NULL AND expires_at>?2",
            rusqlite::params![digest, now],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .ok()
        .flatten()?;
    let (member, issued_credential_digest, auth_kind) = row;
    if auth_kind != "team_key" || issued_credential_digest.is_empty() {
        return None;
    }
    let current = active_credential_digest(&member, db, cfg)?;
    if constant_time_equal(&current, &issued_credential_digest) {
        Some(member)
    } else {
        None
    }
}

fn active_credential_digest(member: &str, db: &Connection, cfg: &AuthConfig) -> Option<String> {
    if member == cfg.admin_member_id {
        return cfg.team_tokens.get(member).map(|token| sha256_hex(token));
    }
    let managed: Option<(Option<String>, i64)> = db
        .query_row(
            "SELECT token_sha256,active FROM team_access_credentials WHERE member_id=?1",
            [member],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .ok()?;
    if let Some((digest, active)) = managed {
        return if active == 1 { digest } else { None };
    }
    cfg.team_tokens.get(member).map(|token| sha256_hex(token))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn database() -> Connection {
        let db = Connection::open_in_memory().unwrap();
        db.execute_batch(
            "CREATE TABLE team_access_credentials(member_id TEXT PRIMARY KEY, \
             token_sha256 TEXT, active INTEGER NOT NULL);\
             CREATE TABLE team_browser_sessions(token_sha256 TEXT PRIMARY KEY, \
             member_id TEXT,credential_sha256 TEXT,auth_kind TEXT,expires_at REAL,revoked_at REAL);",
        )
        .unwrap();
        db
    }

    fn cfg() -> AuthConfig {
        AuthConfig::new(
            BTreeMap::from([
                ("chief".into(), "chief-secret".into()),
                ("mod".into(), "mod-secret".into()),
            ]),
            "chief".into(),
        )
        .unwrap()
    }

    #[test]
    fn static_keys_and_revocation_are_consistent() {
        let db = database();
        let cfg = cfg();
        assert_eq!(
            authenticate(Some("chief-secret"), None, &db, &cfg).unwrap(),
            "chief"
        );
        assert_eq!(
            authenticate(Some("mod-secret"), None, &db, &cfg).unwrap(),
            "mod"
        );
        let new_token = "re_new-test-token";
        db.execute(
            "INSERT INTO team_access_credentials(member_id,token_sha256,active) VALUES(?1,?2,1)",
            rusqlite::params!["mod", sha256_hex(new_token)],
        )
        .unwrap();
        assert!(authenticate(Some("mod-secret"), None, &db, &cfg).is_err());
        assert_eq!(
            authenticate(Some(new_token), None, &db, &cfg).unwrap(),
            "mod"
        );
        db.execute(
            "UPDATE team_access_credentials SET active=0 WHERE member_id='mod'",
            [],
        )
        .unwrap();
        assert!(authenticate(Some(new_token), None, &db, &cfg).is_err());
        assert_eq!(
            authenticate(Some("chief-secret"), None, &db, &cfg).unwrap(),
            "chief"
        );
    }

    #[test]
    fn browser_session_is_bound_to_current_credential_and_header_precedence() {
        let db = database();
        let cfg = cfg();
        let cookie = "rs_really-secret-browser-cookie";
        let expiry = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs_f64()
            + 3600.0;
        db.execute(
            "INSERT INTO team_browser_sessions VALUES(?1,?2,?3,'team_key',?4,NULL)",
            rusqlite::params![sha256_hex(cookie), "mod", sha256_hex("mod-secret"), expiry],
        )
        .unwrap();
        assert_eq!(authenticate(None, Some(cookie), &db, &cfg).unwrap(), "mod");
        assert!(matches!(
            authenticate(Some("invalid"), Some(cookie), &db, &cfg),
            Err(AuthFailure::InvalidApiKey)
        ));
        db.execute(
            "INSERT INTO team_access_credentials(member_id,token_sha256,active) VALUES('mod',?1,1)",
            [sha256_hex("re_rotated")],
        )
        .unwrap();
        assert!(authenticate(None, Some(cookie), &db, &cfg).is_err());
        assert!(authenticate(Some("re_rotated"), None, &db, &cfg).is_ok());
    }

    #[test]
    fn member_listing_matches_python_admin_static_managed_disabled_semantics() {
        let db = database();
        let cfg = cfg();
        let original = list_members(&db, &cfg).unwrap();
        assert_eq!(original["members"].as_array().unwrap().len(), 2);
        assert_eq!(original["members"][0]["member_id"], "chief");
        assert_eq!(original["members"][0]["role"], "admin");
        assert_eq!(original["members"][1]["credential_source"], "configuration");

        db.execute(
            "INSERT INTO team_access_credentials(member_id,active,token_sha256) VALUES('new_mod',1,'digest-only')",
            [],
        ).unwrap();
        db.execute(
            "INSERT INTO team_access_credentials(member_id,active,token_sha256) VALUES('mod',0,NULL)",
            [],
        ).unwrap();
        // A damaged override row must never disable the configured administrator.
        db.execute(
            "INSERT INTO team_access_credentials(member_id,active,token_sha256) VALUES('chief',0,NULL)",
            [],
        ).unwrap();
        let got = list_members(&db, &cfg).unwrap();
        assert_eq!(got["members"].as_array().unwrap().len(), 3);
        assert_eq!(got["members"][0]["role"], "admin");
        assert_eq!(got["members"][0]["status"], "active");
        assert_eq!(got["members"][1]["member_id"], "mod");
        assert_eq!(got["members"][1]["status"], "disabled");
        assert_eq!(got["members"][1]["credential_source"], "configuration");
        assert_eq!(got["members"][2]["member_id"], "new_mod");
        assert_eq!(got["members"][2]["credential_source"], "managed");
        let text = got.to_string();
        assert!(!text.contains("digest-only"), "Credentials must never leak");
    }

    #[test]
    fn empty_configuration_rejects_all_authentication() {
        let db = database();
        let empty = AuthConfig::new(BTreeMap::new(), String::new()).unwrap();
        assert!(authenticate(Some("x"), None, &db, &empty).is_err());
        assert!(authenticate(None, None, &db, &empty).is_err());
    }
}
