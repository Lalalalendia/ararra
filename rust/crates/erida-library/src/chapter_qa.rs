//! M2c: exact read-only Python ChapterQaStore.current() projection.
//! Python owns report creation, revision checks and human review. Never infer
//! alignment proof, rescan a chapter or modify the existing SQLite schema.
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const POLICY_VERSION: i64 = 1;

#[derive(Debug)]
pub enum QaReadError {
    Sqlite(rusqlite::Error),
    Malformed,
    IntegrityDrift,
}
impl From<rusqlite::Error> for QaReadError {
    fn from(error: rusqlite::Error) -> Self { Self::Sqlite(error) }
}

struct Chapter {
    source_revision: i64,
    source_hash: String,
    accepted_revision: i64,
    accepted_html: Option<String>,
    accepted_source_revision: i64,
    glossary_json: String,
    versions_json: String,
    glossary_authority: String,
    glossary_revision: i64,
}

fn sha(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

/// Python json.dumps(ensure_ascii=False,sort_keys=True) uses a space after
/// every comma and colon. Hashing serde_json compact bytes breaks QA revisions.
fn python_json(value: &Value) -> Result<String, QaReadError> {
    match value {
        Value::Array(items) => {
            let items = items.iter().map(python_json)
                .collect::<Result<Vec<_>, _>>()?;
            Ok(format!("[{}]", items.join(", ")))
        }
        Value::Object(map) => {
            let mut keys: Vec<_> = map.keys().collect();
            keys.sort();
            let mut entries = Vec::with_capacity(keys.len());
            for key in keys {
                let quoted = serde_json::to_string(key).map_err(|_| QaReadError::Malformed)?;
                entries.push(format!("{}: {}", quoted, python_json(&map[key])?));
            }
            Ok(format!("{{{}}}", entries.join(", ")))
        }
        scalar => serde_json::to_string(scalar).map_err(|_| QaReadError::Malformed),
    }
}

fn glossary_hash(ch: &Chapter) -> Result<String, QaReadError> {
    let glossary: Value = serde_json::from_str(&ch.glossary_json)
        .map_err(|_| QaReadError::Malformed)?;
    let versions: Value = serde_json::from_str(&ch.versions_json)
        .map_err(|_| QaReadError::Malformed)?;
    let record = json!({
        "glossary": glossary,
        "versions": versions,
        "authority": ch.glossary_authority,
    });
    Ok(sha(&python_json(&record)?))
}

/// Return Ok(None) for a missing chapter, matching Python's 404.
pub fn current(
    db: &Connection, book_id: &str, chapter_id: &str,
) -> Result<Option<Value>, QaReadError> {
    let chapter: Option<Chapter> = db.query_row(
        "SELECT c.source_revision,c.source_hash,c.accepted_revision,
                c.accepted_html,c.accepted_source_revision,
                b.glossary_json,b.glossary_versions_json,
                b.glossary_authority,b.glossary_revision
         FROM book_chapters AS c
         JOIN books AS b ON b.id=c.book_id
         WHERE c.book_id=?1 AND c.chapter_id=?2",
        params![book_id, chapter_id],
        |r| Ok(Chapter {
            source_revision: r.get(0)?,
            source_hash: r.get(1)?,
            accepted_revision: r.get(2)?,
            accepted_html: r.get(3)?,
            accepted_source_revision: r.get(4)?,
            glossary_json: r.get(5)?,
            versions_json: r.get(6)?,
            glossary_authority: r.get(7)?,
            glossary_revision: r.get(8)?,
        }),
    ).optional()?;
    let Some(ch) = chapter else { return Ok(None); };
    let current = ch.accepted_html.as_deref().is_some_and(|s| !s.is_empty())
        && ch.accepted_revision > 0
        && ch.accepted_source_revision == ch.source_revision;
    let accepted_hash = sha(ch.accepted_html.as_deref().unwrap_or(""));
    let glossary_hash = glossary_hash(&ch)?;

    let mut report: Option<Value> = None;
    if current {
        let saved = db.query_row(
            "SELECT id,source_revision,source_hash,accepted_revision,
                    accepted_hash,glossary_revision,glossary_hash,
                    policy_version,finding_count,stored_count,truncated,
                    generated_by,created_at
             FROM chapter_qa_reports
             WHERE book_id=?1 AND chapter_id=?2 AND source_revision=?3
               AND accepted_revision=?4 AND glossary_revision=?5
               AND policy_version=?6",
            params![book_id, chapter_id, ch.source_revision,
                    ch.accepted_revision, ch.glossary_revision, POLICY_VERSION],
            |r| Ok(json!({
                "report_id": r.get::<_, String>(0)?,
                "source_revision": r.get::<_, i64>(1)?,
                "source_hash": r.get::<_, String>(2)?,
                "accepted_revision": r.get::<_, i64>(3)?,
                "accepted_hash": r.get::<_, String>(4)?,
                "glossary_revision": r.get::<_, i64>(5)?,
                "glossary_hash": r.get::<_, String>(6)?,
                "policy_version": r.get::<_, i64>(7)?,
                "alignment_proven": false,
                "diagnostic_only": true,
                "finding_count": r.get::<_, i64>(8)?,
                "stored_count": r.get::<_, i64>(9)?,
                "truncated": r.get::<_, i64>(10)? != 0,
                "generated_by": r.get::<_, String>(11)?,
                "created_at": r.get::<_, f64>(12)?,
            })),
        ).optional()?;
        if let Some(mut item) = saved {
            if item["source_hash"] != ch.source_hash
                || item["accepted_hash"] != accepted_hash
                || item["glossary_hash"] != glossary_hash
            {
                return Err(QaReadError::IntegrityDrift);
            }
            let report_id = item["report_id"].as_str()
                .ok_or(QaReadError::Malformed)?;
            let mut stmt = db.prepare(
                "SELECT id,code,severity,would_block,message,evidence_json,
                        review_status,review_revision,reviewer_id,review_note,reviewed_at
                 FROM chapter_qa_findings
                 WHERE report_id=?1 ORDER BY ordinal",
            )?;
            let rows = stmt.query_map([report_id], |r| {
                Ok((
                    r.get::<_, String>(0)?, r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?, r.get::<_, i64>(3)?,
                    r.get::<_, String>(4)?, r.get::<_, String>(5)?,
                    r.get::<_, String>(6)?, r.get::<_, i64>(7)?,
                    r.get::<_, Option<String>>(8)?,
                    r.get::<_, String>(9)?,
                    r.get::<_, Option<f64>>(10)?,
                ))
            })?;
            let mut findings = Vec::new();
            for row in rows {
                let (id, code, severity, would_block, message, evidence,
                     review_status, review_revision, reviewer_id, review_note,
                     reviewed_at) = row?;
                let evidence: Value = serde_json::from_str(&evidence)
                    .map_err(|_| QaReadError::Malformed)?;
                findings.push(json!({
                    "finding_id": id,
                    "code": code,
                    "severity": severity,
                    "would_block": would_block != 0,
                    "message": message,
                    "evidence": evidence,
                    "review_status": review_status,
                    "review_revision": review_revision,
                    "reviewer_id": reviewer_id,
                    "review_note": review_note,
                    "reviewed_at": reviewed_at,
                }));
            }
            item["findings"] = json!(findings);
            report = Some(item);
        }
    }
    Ok(Some(json!({
        "book_id": book_id,
        "chapter_id": chapter_id,
        "accepted_current": current,
        "source_revision": ch.source_revision,
        "source_hash": ch.source_hash,
        "accepted_revision": ch.accepted_revision,
        "accepted_hash": accepted_hash,
        "glossary_revision": ch.glossary_revision,
        "glossary_hash": glossary_hash,
        "report": report,
    })))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn canonical_python_spaced_glossary_json() {
        let j = json!({"glossary":{"名字":{"rus":"Ланселот","note":"Персонаж"}},
                       "versions":{}, "authority":"READY"});
        let serial = python_json(&j).unwrap();
        assert_eq!(
            serial,
            r#"{"authority": "READY", "glossary": {"名字": {"note": "Персонаж", "rus": "Ланселот"}}, "versions": {}}"#,
        );
        assert_eq!(sha(&serial).len(), 64);
    }
}
