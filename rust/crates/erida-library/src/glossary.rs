//! M1: source-preserving glossary CAS and audit, disposable SQLite only.
//! Python BookLibrary.put_glossary is the reference semantics.
use super::{get_book, now, LibraryError};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{Map, Value};
use std::collections::BTreeSet;

/// Match Python json.dumps(value, ensure_ascii=False, sort_keys=True).
/// serde_json::to_string uses compact separators; the actual Python database
/// stores spaced separators. Exact bytes matter to cross-runtime replay.
fn python_json(value: &Value) -> Result<String, LibraryError> {
    match value {
        Value::Array(values) => {
            let parts = values.iter().map(python_json).collect::<Result<Vec<_>, _>>()?;
            Ok(format!("[{}]", parts.join(", ")))
        }
        Value::Object(map) => {
            let mut keys: Vec<_> = map.keys().collect();
            keys.sort();
            let mut parts = Vec::with_capacity(keys.len());
            for key in keys {
                let escaped = serde_json::to_string(key).map_err(|_| LibraryError::Malformed)?;
                parts.push(format!("{}: {}", escaped, python_json(&map[key])?));
            }
            Ok(format!("{{{}}}", parts.join(", ")))
        }
        scalar => serde_json::to_string(scalar).map_err(|_| LibraryError::Malformed),
    }
}

fn python_repr(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut escaped = s.replace('\\', "\\\\")
        .replace('\n', "\\n").replace('\r', "\\r").replace('\t', "\\t");
    if q == '\'' { escaped = escaped.replace('\'', "\\'"); }
    else { escaped = escaped.replace('"', "\\\""); }
    format!("{q}{escaped}{q}")
}

fn validate_scope_conflicts(
    versions: &Map<String, Value>,
    chapter_path: &str,
    authority: &str,
) -> Result<(), LibraryError> {
    if !matches!(authority, "READY" | "READY_WITH_WARNINGS") { return Ok(()); }
    let path = chapter_path.replace('\\', "/");
    if path.is_empty() { return Ok(()); }
    for (source, rules) in versions {
        if source.trim().is_empty() { continue; }
        let Some(rules) = rules.as_array() else { continue; };
        let mut unique = BTreeSet::new();
        for rule in rules {
            let Some(fields) = rule.as_object() else { continue; };
            let matches = fields.get("scope").and_then(Value::as_array)
                .is_some_and(|items| items.iter().any(|item| {
                    item.as_str().is_some_and(|value| value.replace('\\', "/") == path)
                }));
            if !matches { continue; }
            let Some(override_fields) = fields.get("override").and_then(Value::as_object) else {
                continue;
            };
            let mut reduced = Map::new();
            for key in ("rus", "note") {
                if let Some(value) = override_fields.get(key) {
                    reduced.insert(key.to_string(), value.clone());
                }
            }
            unique.insert(python_json(&Value::Object(reduced))?);
        }
        if unique.len() > 1 {
            return Err(LibraryError::GlossaryRuleConflict(format!(
                "Conflicting glossary versions for term {} in chapter {}",
                python_repr(source),
                python_repr(&path),
            )));
        }
    }
    Ok(())
}

fn validate_payload(
    glossary: &Value, versions: &Value, authority: &str, expected_revision: i64,
) -> Result<(), LibraryError> {
    if expected_revision < 0 {
        return Err(LibraryError::Invalid("Glossary revision must be nonnegative"));
    }
    if !matches!(authority,
        "READY" | "READY_WITH_WARNINGS" | "EMPTY_EXPECTED" | "UNAVAILABLE" | "DISABLED"
    ) {
        return Err(LibraryError::Invalid("Invalid glossary authority"));
    }
    let Some(entries) = glossary.as_object() else {
        return Err(LibraryError::Invalid("Glossary and versions must be objects"));
    };
    if entries.values().any(|v| !(v.is_string() || v.is_object())) {
        return Err(LibraryError::Invalid("Glossary entries must be strings or objects"));
    }
    let Some(rules) = versions.as_object() else {
        return Err(LibraryError::Invalid("Glossary and versions must be objects"));
    };
    if rules.values().any(|v|
        !v.as_array().is_some_and(|rows| rows.iter().all(Value::is_object))
    ) {
        return Err(LibraryError::Invalid(
            "Versioned glossary rules must be arrays keyed by source term",
        ));
    }
    Ok(())
}

/// An expected-revision CAS and its history event commit atomically. A replay
/// with equivalent canonical JSON and authority does not create a revision.
pub fn put_glossary(
    db: &mut Connection,
    book_id: &str,
    glossary: &Value,
    versions: &Value,
    authority: &str,
    expected_revision: i64,
    actor: &str,
) -> Result<Value, LibraryError> {
    validate_payload(glossary, versions, authority, expected_revision)?;
    let packed = python_json(glossary)?;
    let packed_versions = python_json(versions)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let current: Option<(String, String, String, i64)> = tx.query_row(
        "SELECT glossary_json,glossary_versions_json,glossary_authority,glossary_revision \
         FROM books WHERE id=?1",
        params![book_id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
    ).optional()?;
    let Some((old_glossary,old_versions,old_auth,rev)) = current else {
        return Err(LibraryError::Missing("Book not found"));
    };
    if rev != expected_revision {
        return Err(LibraryError::Conflict("Glossary revision mismatch"));
    }
    {
        let mut query = tx.prepare("SELECT chapter_path FROM book_chapters WHERE book_id=?1")?;
        let mut rows = query.query(params![book_id])?;
        while let Some(row) = rows.next()? {
            let chapter_path: String = row.get(0)?;
            validate_scope_conflicts(
                versions.as_object().ok_or(LibraryError::Malformed)?,
                &chapter_path, authority,
            )?;
        }
    }
    if old_glossary == packed && old_versions == packed_versions && old_auth == authority {
        let response = get_book(&tx, book_id)?
            .ok_or(LibraryError::Missing("Book not found"))?;
        tx.commit()?;
        return Ok(response);
    }
    let timestamp = now()?;
    let next = expected_revision + 1;
    tx.execute(
        "UPDATE books SET glossary_json=?1,glossary_versions_json=?2,\
         glossary_authority=?3,glossary_revision=?4,updated_at=?5 WHERE id=?6",
        params![packed, packed_versions, authority, next, timestamp, book_id],
    )?;
    tx.execute(
        "INSERT INTO book_glossary_revisions \
         (book_id,revision,glossary_json,versions_json,authority,actor_id,created_at) \
         VALUES(?1,?2,?3,?4,?5,?6,?7)",
        params![book_id, next, packed, packed_versions, authority, actor, timestamp],
    )?;
    let response = get_book(&tx, book_id)?
        .ok_or(LibraryError::Missing("Book not found"))?;
    tx.commit()?;
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn fixture() -> Connection {
        let mut db = Connection::open_in_memory().unwrap();
        db.execute_batch(
            "PRAGMA foreign_keys=ON;\
             CREATE TABLE books(id TEXT PRIMARY KEY,title TEXT,owner_member_id TEXT,\
               owner_revision INTEGER NOT NULL DEFAULT 0,\
               glossary_json TEXT NOT NULL DEFAULT '{}',\
               glossary_versions_json TEXT NOT NULL DEFAULT '{}',\
               glossary_authority TEXT NOT NULL DEFAULT 'READY',\
               glossary_revision INTEGER NOT NULL DEFAULT 0,\
               created_at REAL,updated_at REAL);\
             CREATE TABLE book_chapters(book_id TEXT,chapter_path TEXT);\
             CREATE TABLE book_glossary_revisions(book_id TEXT,revision INTEGER,\
               glossary_json TEXT,versions_json TEXT,authority TEXT,actor_id TEXT,\
               created_at REAL,PRIMARY KEY(book_id,revision));"
        ).unwrap();
        db.execute(
            "INSERT INTO books(id,title,owner_member_id) VALUES('bk','Тест','mod')",[],
        ).unwrap();
        db
    }

    #[test]
    fn python_unicode_packing_and_replay_cas() {
        let mut db = fixture();
        let entries = json!({"龙":"Дракон","alpha":{"rus":"Солнце","note":"ввод"}});
        let versions = json!({"龙":[{"scope":["chapters/1.xhtml"],"override":{"rus":"Лун"}}]});
        let first = put_glossary(&mut db,"bk",&entries,&versions,"READY",0,"mod").unwrap();
        assert_eq!(first["glossary_revision"],1);
        let packed:String=db.query_row(
            "SELECT glossary_json FROM books WHERE id='bk'",[],|r|r.get(0)
        ).unwrap();
        assert_eq!(packed, r#"{"alpha": {"note": "ввод", "rus": "Солнце"}, "龙": "Дракон"}"#);
        assert_eq!(put_glossary(&mut db,"bk",&entries,&versions,"READY",1,"mod").unwrap(),first);
        assert!(matches!(put_glossary(&mut db,"bk",&entries,&versions,"READY",0,"mod"),
                         Err(LibraryError::Conflict(_))));
        let events:i64=db.query_row(
            "SELECT COUNT(*) FROM book_glossary_revisions",[],|r|r.get(0)
        ).unwrap();
        assert_eq!(events,1);
    }

    #[test]
    fn ambiguous_scoped_terms_never_mutate() {
        let mut db = fixture();
        db.execute("INSERT INTO book_chapters(book_id,chapter_path) VALUES('bk','chapters/1.xhtml')",[]).unwrap();
        let conflict=json!({"龙":[
          {"scope":["chapters/1.xhtml"],"override":{"rus":"Дракон"}},
          {"scope":["chapters\\1.xhtml"],"override":{"rus":"Змей"}}
        ]});
        assert!(matches!(
            put_glossary(&mut db,"bk",&json!({}),&conflict,"READY",0,"mod"),
            Err(LibraryError::GlossaryRuleConflict(_))
        ));
        let rev:i64=db.query_row(
            "SELECT glossary_revision FROM books WHERE id='bk'",[],|r|r.get(0)
        ).unwrap();
        assert_eq!(rev,0);
        let event_count:i64=db.query_row(
            "SELECT COUNT(*) FROM book_glossary_revisions",[],|r|r.get(0)
        ).unwrap();
        assert_eq!(event_count,0);
        // Python disables all scoped versions when glossary authority is offline.
        let disabled=put_glossary(&mut db,"bk",&json!({}),&conflict,"DISABLED",0,"mod").unwrap();
        assert_eq!(disabled["glossary_revision"],1);
    }
}
