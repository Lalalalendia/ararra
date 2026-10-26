//! M2e-1: native, CPU-only QA scanning for the precisely proven baseline.
//!
//! The full Python gate handles morphology, scoped entities and substitutions.
//! NEVER claim that absence of findings implies a clean manuscript unless the
//! glossary is empty and the target uses the deliberately small HTML grammar
//! proven below. Unsupported inputs fail closed (400); they do not write a
//! partial or falsely successful QA report. Python remains production writer.
use crate::chapter_qa::{current, QaReadError};
use rusqlite::{params, Connection, TransactionBehavior};
use serde_json::{json, Value};
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

const POLICY_VERSION: i64 = 1;

fn unsupported() -> QaReadError {
    QaReadError::Invalid(
        "Rust QA scan supports only plain <p>/<h1> and an empty glossary; use Python QA",
    )
}

/// A strict subset of BeautifulSoup's visible-node rules; no entities,
/// attributes, nested nodes, scripts, comments or unproven HTML decoding.
fn proven_visible_nodes(html: &str) -> Result<Vec<&str>, QaReadError> {
    if html.chars().count() > 2_000_000 {
        return Err(unsupported());
    }
    let mut input = html.trim();
    let mut nodes = Vec::new();
    while !input.is_empty() {
        let (tag, close) = if input.starts_with("<p>") {
            ("<p>", "</p>")
        } else if input.starts_with("<h1>") {
            ("<h1>", "</h1>")
        } else {
            return Err(unsupported());
        };
        let rest = &input[tag.len()..];
        let end = rest.find(close).ok_or_else(unsupported)?;
        let text = &rest[..end];
        // Do not silently misinterpret encoded CJK, raw markup or controls.
        if text.contains('&') || text.contains('<') || text.contains('>')
            || text.chars().any(|c| c.is_control() && !matches!(c, '\n' | '\r' | '\t'))
        {
            return Err(unsupported());
        }
        if !text.trim().is_empty() {
            nodes.push(text);
        }
        input = rest[end + close.len()..].trim_start();
    }
    if nodes.is_empty() {
        return Err(unsupported());
    }
    Ok(nodes)
}

fn is_cjk(ch: char) -> bool {
    matches!(ch as u32,
        0x3400..=0x4DBF | 0x4E00..=0x9FFF | 0xF900..=0xFAFF
        | 0x3040..=0x30FF | 0xAC00..=0xD7AF
        | 0x20000..=0x2EE5F | 0x2F800..=0x2FA1F
        | 0x30000..=0x323AF)
}

/// Exact CJK_RESIDUE diagnostic output for the admitted subset.
fn baseline_findings(html: &str) -> Result<Vec<Value>, QaReadError> {
    let nodes = proven_visible_nodes(html)?;
    let combined = nodes.join("\n");
    let residue: Vec<char> = combined.chars().filter(|ch| is_cjk(*ch)).collect();
    if residue.is_empty() {
        return Ok(Vec::new());
    }
    let excerpt: String = residue.iter().take(12).collect();
    let sample: String = residue.iter().take(24).collect();
    Ok(vec![json!({
        "code": "CJK_RESIDUE",
        "severity": "high",
        "blocking": false,
        "message": format!(
            "В русском visible text остались CJK-символы: '{excerpt}'."
        ),
        "evidence": {"sample": sample, "count": residue.len()},
    })])
}

fn empty_glossary(value: &str) -> Result<bool, QaReadError> {
    let json: Value = serde_json::from_str(value).map_err(|_| QaReadError::Malformed)?;
    Ok(json.as_object().is_some_and(|entries| entries.is_empty()))
}

fn required_int(snapshot: &Value, key: &str) -> Result<i64, QaReadError> {
    snapshot[key].as_i64().ok_or(QaReadError::Malformed)
}
fn required_str<'a>(snapshot: &'a Value, key: &str) -> Result<&'a str, QaReadError> {
    snapshot[key].as_str().ok_or(QaReadError::Malformed)
}

fn check_snapshot(
    snapshot: &Value,
    source: i64,
    accepted: i64,
    glossary: i64,
) -> Result<(), QaReadError> {
    if snapshot["accepted_current"] != true {
        return Err(QaReadError::Conflict(
            "Accepted translation is missing or stale; QA unavailable",
        ));
    }
    if snapshot["source_revision"] != source
        || snapshot["accepted_revision"] != accepted
        || snapshot["glossary_revision"] != glossary
    {
        return Err(QaReadError::Conflict(
            "Source, accepted or glossary revision changed; QA refresh required",
        ));
    }
    Ok(())
}

pub fn scan_baseline(
    db: &mut Connection,
    book_id: &str,
    chapter_id: &str,
    expected_source_revision: i64,
    expected_accepted_revision: i64,
    expected_glossary_revision: i64,
    actor: &str,
) -> Result<Value, QaReadError> {
    // First read without an immediate lock; expensive scanning runs only
    // against this snapshot and never holds SQLite's writer reservation.
    let original = current(db, book_id, chapter_id)?
        .ok_or(QaReadError::Missing("Chapter not found"))?;
    check_snapshot(&original, expected_source_revision, expected_accepted_revision,
                   expected_glossary_revision)?;
    if let Some(existing) = original.get("report").filter(|x| !x.is_null()) {
        return Ok(existing.clone()); // Python: stable idempotent report receipt
    }

    let (accepted, glossary_json, versions_json): (String, String, String) =
        db.query_row(
            "SELECT c.accepted_html,b.glossary_json,b.glossary_versions_json
             FROM book_chapters AS c
             JOIN books AS b ON b.id=c.book_id
             WHERE c.book_id=?1 AND c.chapter_id=?2",
            params![book_id, chapter_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )?;
    if !empty_glossary(&glossary_json)? || !empty_glossary(&versions_json)? {
        return Err(unsupported());
    }
    let findings = baseline_findings(&accepted)?;

    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let fresh = current(&tx, book_id, chapter_id)?
        .ok_or(QaReadError::Missing("Chapter not found"))?;
    check_snapshot(&fresh, expected_source_revision, expected_accepted_revision,
                   expected_glossary_revision)?;
    // A simultaneous Python scan may already have committed the report.
    if let Some(existing) = fresh.get("report").filter(|x| !x.is_null()) {
        return Ok(existing.clone());
    }
    for key in [
        "source_revision", "source_hash", "accepted_revision",
        "accepted_hash", "glossary_revision", "glossary_hash",
    ] {
        if fresh[key] != original[key] {
            return Err(QaReadError::Conflict("QA chapter changed during diagnostic scan"));
        }
    }
    // The competing write could change the unrecorded glossary content.
    // Compare fresh accepted bytes and glossary values under the writer lock.
    let (fresh_accepted, fresh_glossary, fresh_versions): (String, String, String) =
        tx.query_row(
            "SELECT c.accepted_html,b.glossary_json,b.glossary_versions_json
             FROM book_chapters AS c JOIN books AS b ON b.id=c.book_id
             WHERE c.book_id=?1 AND c.chapter_id=?2",
            params![book_id, chapter_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )?;
    if fresh_accepted != accepted || fresh_glossary != glossary_json
        || fresh_versions != versions_json
    {
        return Err(QaReadError::Conflict("QA chapter changed during diagnostic scan"));
    }
    let created = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| QaReadError::Malformed)?
        .as_secs_f64();
    let report_id = Uuid::new_v4().simple().to_string();
    let count = i64::try_from(findings.len()).map_err(|_| QaReadError::Malformed)?;
    tx.execute(
        "INSERT INTO chapter_qa_reports(
            id,book_id,chapter_id,source_revision,source_hash,
            accepted_revision,accepted_hash,glossary_revision,glossary_hash,
            policy_version,finding_count,stored_count,truncated,
            alignment_proven,generated_by,created_at
         ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,0,0,?13,?14)",
        params![
            report_id, book_id, chapter_id,
            required_int(&fresh, "source_revision")?,
            required_str(&fresh, "source_hash")?,
            required_int(&fresh, "accepted_revision")?,
            required_str(&fresh, "accepted_hash")?,
            required_int(&fresh, "glossary_revision")?,
            required_str(&fresh, "glossary_hash")?,
            POLICY_VERSION, count, count, actor, created,
        ],
    )?;
    for (index, finding) in findings.iter().enumerate() {
        let evidence = serde_json::to_string(&finding["evidence"])
            .map_err(|_| QaReadError::Malformed)?;
        tx.execute(
            "INSERT INTO chapter_qa_findings(
                id,report_id,ordinal,code,severity,would_block,message,evidence_json
             ) VALUES(?1,?2,?3,?4,?5,0,?6,?7)",
            params![
                Uuid::new_v4().simple().to_string(),
                report_id, i64::try_from(index).map_err(|_| QaReadError::Malformed)?,
                required_str(finding, "code")?, required_str(finding, "severity")?,
                required_str(finding, "message")?, evidence,
            ],
        )?;
    }
    let result = current(&tx, book_id, chapter_id)?
        .and_then(|value| value.get("report").cloned())
        .filter(|value| !value.is_null())
        .ok_or(QaReadError::Malformed)?;
    tx.commit()?;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_visible_subset_is_narrow_and_must_not_decode_html() {
        assert_eq!(proven_visible_nodes("<p>Привет</p><h1>Глава 1</h1>").unwrap(),
                   vec!["Привет", "Глава 1"]);
        for rejected in [
            "<p>Привет &amp; 世界</p>",
            "<p><em>слово</em></p>",
            "<script>мир</script>",
            "<p data-id='1'>слово</p>",
            "<p>незакрыто",
            "<p> </p>",
            "<p>abc</p>tail",
        ] {
            assert!(proven_visible_nodes(rejected).is_err(), "{rejected}");
        }
    }

    #[test]
    fn residues_preserve_exact_unicode_order_and_counts() {
        let findings = baseline_findings("<p>Перевод 汉字.</p><p>テスト 가나다 𠀀</p>").unwrap();
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0]["code"], "CJK_RESIDUE");
        assert_eq!(findings[0]["severity"], "high");
        assert_eq!(findings[0]["blocking"], false);
        assert_eq!(findings[0]["evidence"]["sample"], "汉字テスト가나다𠀀");
        assert_eq!(findings[0]["evidence"]["count"], 9);
        assert_eq!(baseline_findings("<p>Русский перевод.</p>").unwrap(), vec![]);
    }

    #[test]
    fn unsupported_glossary_cannot_yield_falsely_clean_report() {
        assert!(empty_glossary("{}").unwrap());
        assert!(empty_glossary("{\"keys\":[]}").is_ok_and(|v| !v));
        assert!(empty_glossary("[]").is_ok_and(|v| !v));
        assert!(empty_glossary("{not-json}").is_err());
    }
}
