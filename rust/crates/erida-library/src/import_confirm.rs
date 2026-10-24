//! M1p — explicit, atomic, actor-owned chapter import confirmation.
//! EXPERIMENTAL ONLY: invoke via secured_write() on a disposable SQLite clone.
//! No source-site requests, Gemini, production DB writes or Python fallback.
use super::import_stage::StageError;
use erida_source_clean::import_txt::clean_numbered_import_txt;
use rusqlite::{params, Connection, ErrorCode, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::time::{SystemTime, UNIX_EPOCH};

fn now() -> Result<f64, StageError> {
    SystemTime::now().duration_since(UNIX_EPOCH)
        .map(|v| v.as_secs_f64()).map_err(|_| StageError::Malformed)
}
fn field<'a>(v: &'a Value, key: &str) -> Result<&'a Value, StageError> {
    v.get(key).ok_or(StageError::Invalid("Missing import confirmation field"))
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str, StageError> {
    field(v,key)?.as_str().ok_or(StageError::Invalid("Invalid import confirmation field"))
}
fn optional_text(v: &Value, key: &str, max: usize) -> Result<Option<String>, StageError> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.chars().count()<=max => Ok(Some(s.to_owned())),
        _ => Err(StageError::Invalid("Invalid optional confirmation field")),
    }
}
fn optional_integer(v: &Value,key:&str,min:i64)->Result<Option<i64>,StageError>{
    match v.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(x) if x.as_i64().is_some_and(|n|n>=min) => Ok(x.as_i64()),
        _=>Err(StageError::Invalid("Invalid confirmation integer")),
    }
}
fn allowed(v:&Value,names:&[&str])->Result<(),StageError>{
    let o=v.as_object().ok_or(StageError::Invalid("Invalid import confirmation body"))?;
    if o.keys().any(|k|!names.contains(&k.as_str())) {
        return Err(StageError::Invalid("Unknown import confirmation field"));
    }
    Ok(())
}
/// Canonicalize the exact Pydantic ConfirmBookImport.model_dump() shape,
/// including null defaults, before computing Python's sorted-key SHA256.
fn normalize(raw: Value)->Result<Value,StageError>{
    allowed(&raw,&["book_id","book_mode","title","owner_member_id","prefix_policy","chapters"])?;
    let book_id=text(&raw,"book_id")?;
    let mode=text(&raw,"book_mode")?;
    if book_id.is_empty()||book_id.chars().count()>128
        || !matches!(mode,"create"|"existing")
    {return Err(StageError::Invalid("Invalid book import identity"));}
    let title=optional_text(&raw,"title",500)?;
    let owner=optional_text(&raw,"owner_member_id",128)?;
    let prefix=raw.get("prefix_policy").and_then(Value::as_str).unwrap_or("reject");
    if !matches!(prefix,"reject"|"preserve"|"discard") {
        return Err(StageError::Invalid("Invalid prefix policy"));
    }
    let choices=field(&raw,"chapters")?.as_array()
        .ok_or(StageError::Invalid("Chapters must be an array"))?;
    if choices.is_empty()||choices.len()>2000 {
        return Err(StageError::Invalid("Choose 1..2000 chapters"));
    }
    let mut normalized=Vec::with_capacity(choices.len());
    for v in choices {
        allowed(v,&["sequence","action","chapter_id","chapter_index","chapter_path","expected_revision"])?;
        let seq=field(v,"sequence")?.as_i64()
            .filter(|n|(1..=2000).contains(n))
            .ok_or(StageError::Invalid("Invalid import sequence"))?;
        let action=text(v,"action")?;
        if !matches!(action,"create"|"replace"|"skip") {
            return Err(StageError::Invalid("Invalid chapter action"));
        }
        let chapter_id=optional_text(v,"chapter_id",255)?;
        let path=optional_text(v,"chapter_path",1024)?;
        let index=optional_integer(v,"chapter_index",1)?;
        let revision=optional_integer(v,"expected_revision",0)?;
        normalized.push(json!({
            "sequence":seq,"action":action,"chapter_id":chapter_id,
            "chapter_index":index,"chapter_path":path,
            "expected_revision":revision
        }));
    }
    Ok(json!({"book_id":book_id,"book_mode":mode,"title":title,
        "owner_member_id":owner,"prefix_policy":prefix,"chapters":normalized}))
}
fn canonical_fingerprint(request:&Value)->Result<String,StageError>{
    let mut canonical=request.clone();
    let chapters=canonical["chapters"].as_array_mut().ok_or(StageError::Malformed)?;
    chapters.sort_by_key(|x|x["sequence"].as_i64().unwrap_or(0));
    let packed=serde_json::to_string(&canonical).map_err(|_|StageError::Malformed)?;
    Ok(hex::encode(Sha256::digest(packed.as_bytes())))
}
fn path(value:&str)->Result<String,StageError>{
    let p=value.trim().replace('\\',"/");
    if p.is_empty()||p.starts_with('/')||p.split('/').any(|p|p==".."){
        return Err(StageError::Conflict("Invalid chapter destination path"));
    }
    Ok(p)
}
fn sql(err:rusqlite::Error)->StageError{
    if let rusqlite::Error::SqliteFailure(ref e,_)=err {
        if e.code==ErrorCode::ConstraintViolation {
            return StageError::Conflict("A chapter or preface destination became unavailable");
        }
    }
    StageError::Sqlite(err)
}
struct Write {
    seq:i64,action:String,id:String,index:i64,path:String,html:String,hash:String,expected:i64
}
fn meaningful_prefix(prefix:&str)->bool{
    // For M1o's canonical generated TXT prefix, literal <p></p> alone
    // cannot count as meaningful. Arbitrary Python stage HTML is never
    // interpreted as an independent proof.
    let s=prefix.trim();
    if !s.starts_with("<p>") || !s.ends_with("</p>") { return false; }
    // The M1o canonical HTML contains escaped text and optional <br/>
    // separators, never arbitrary nested tags. A tag-name by itself is
    // not evidence of meaningful preface text.
    let visible=s.replace("<p>","").replace("</p>","").replace("<br/>","");
    !visible.trim().is_empty()
}

/// Only the strict, independently Python-proven H1+paragraph subset may
/// reach this batch writer. Any unproved chapter aborts the entire batch.
pub fn confirm(
    db:&mut Connection,actor:&str,stage_id:&str,raw:Value,
)->Result<Value,StageError>{
    let request=normalize(raw)?;
    let fingerprint=canonical_fingerprint(&request)?;
    let tx=db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    type StageRow=(String,Option<String>,String,String,f64,Option<String>,Option<String>);
    let row:Option<StageRow>=tx.query_row(
        "SELECT status,payload_json,prefix_html,filename,expires_at,confirm_fingerprint,commit_result_json
         FROM book_import_stages WHERE id=?1 AND actor_id=?2",
        params![stage_id,actor],
        |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))
    ).optional()?;
    let (status,payload,prefix,filename,expires,saved_fingerprint,receipt)=
        row.ok_or(StageError::Missing)?;
    let stamp=now()?;
    if expires<stamp { return Err(StageError::Expired); }
    if status=="committed" {
        if saved_fingerprint.as_deref()!=Some(fingerprint.as_str()) {
            return Err(StageError::Conflict("Already committed with a different chapter selection"));
        }
        let receipt=receipt.ok_or(StageError::Malformed)?;
        return serde_json::from_str(&receipt).map_err(|_|StageError::Malformed);
    }
    if status!="staged" {return Err(StageError::Malformed);}
    let payload=payload.ok_or(StageError::Malformed)?;
    let stored:Value=serde_json::from_str(&payload).map_err(|_|StageError::Malformed)?;
    let chapters=stored.get("chapters").and_then(Value::as_array)
        .ok_or(StageError::Malformed)?;
    if chapters.is_empty()||chapters.len()>2000 {return Err(StageError::Malformed);}
    let mut candidates=BTreeMap::<i64,&Value>::new();
    for entry in chapters {
        let seq=entry.get("sequence").and_then(Value::as_i64)
            .ok_or(StageError::Malformed)?;
        if seq<1||seq>2000||candidates.insert(seq,entry).is_some() {
            return Err(StageError::Malformed);
        }
    }
    let decisions=request["chapters"].as_array().ok_or(StageError::Malformed)?;
    if decisions.len()!=candidates.len() {
        return Err(StageError::Conflict("Every staged chapter needs one explicit decision"));
    }
    let mut seen=BTreeSet::new();
    for choice in decisions {
        let n=choice["sequence"].as_i64().ok_or(StageError::Malformed)?;
        if !seen.insert(n)||!candidates.contains_key(&n) {
            return Err(StageError::Conflict("Duplicate or missing import chapter decision"));
        }
    }
    let has_prefix=meaningful_prefix(&prefix);
    let prefix_policy=request["prefix_policy"].as_str().ok_or(StageError::Malformed)?;
    if has_prefix && prefix_policy=="reject" {
        return Err(StageError::Conflict("Explicitly preserve or discard unassigned preface"));
    }
    if prefix_policy=="preserve"&&!has_prefix {
        return Err(StageError::Conflict("No unassigned preface to preserve"));
    }
    let book_id=request["book_id"].as_str().ok_or(StageError::Malformed)?.trim();
    let mode=request["book_mode"].as_str().ok_or(StageError::Malformed)?;
    let title=request["title"].as_str().unwrap_or("").trim();
    let owner=request["owner_member_id"].as_str().unwrap_or(actor).trim();
    if book_id.is_empty() || book_id.chars().count()>128 {
        return Err(StageError::Invalid("Valid book_id is required"));
    }
    let book:Option<(String,Option<String>)>=tx.query_row(
        "SELECT title,owner_member_id FROM books WHERE id=?1",
        [book_id],|r|Ok((r.get(0)?,r.get(1)?))
    ).optional()?;
    match mode{
        "create"=>{
            if book.is_some(){return Err(StageError::Conflict("Book already exists"));}
            if title.is_empty()||title.chars().count()>500||owner.is_empty()||owner.chars().count()>128{
                return Err(StageError::Invalid("Book title and active owner are required"));
            }
        }
        "existing"=>{
            let (current_title,_)=book.ok_or(StageError::Missing)?;
            if !title.is_empty()&&title!=current_title {
                return Err(StageError::Conflict("Existing book cannot be renamed in import"));
            }
            if !request["owner_member_id"].is_null() {
                return Err(StageError::Conflict("Existing owner cannot change in import"));
            }
        }
        _=>return Err(StageError::Invalid("Invalid book mode")),
    }
    let mut writes=Vec::<Write>::new();
    let mut result_chapters=Vec::<Value>::with_capacity(decisions.len());
    let(mut ids,mut indexes,mut paths)=(BTreeSet::new(),BTreeSet::new(),BTreeSet::new());
    for decision in decisions {
        let seq=decision["sequence"].as_i64().ok_or(StageError::Malformed)?;
        let action=decision["action"].as_str().ok_or(StageError::Malformed)?;
        if action=="skip" {
            if ["chapter_id","chapter_index","chapter_path","expected_revision"]
                .iter().any(|k|!decision[k].is_null()) {
                return Err(StageError::Conflict("Skip must not specify a destination"));
            }
            result_chapters.push(json!({"sequence":seq,"action":"skip"}));
            continue;
        }
        let id=decision["chapter_id"].as_str().ok_or(StageError::Conflict("Missing chapter ID"))?.trim();
        let index=decision["chapter_index"].as_i64().ok_or(StageError::Conflict("Missing chapter index"))?;
        let dest=path(decision["chapter_path"].as_str()
            .ok_or(StageError::Conflict("Missing chapter path"))?)?;
        let expected=decision["expected_revision"].as_i64()
            .ok_or(StageError::Conflict("Expected revision required"))?;
        if id.is_empty()||id.chars().count()>255||index<1||expected<0
            || action=="create"&&expected!=0
            || !ids.insert(id.to_owned())||!indexes.insert(index)||!paths.insert(dest.clone()) {
            return Err(StageError::Conflict("Invalid or duplicate import destination"));
        }
        let source=candidates.get(&seq).ok_or(StageError::Malformed)?;
        let html=source.get("source_html").and_then(Value::as_str)
            .ok_or(StageError::Malformed)?;
        if html.chars().count()>2_000_000 {
            return Err(StageError::Conflict("Chapter HTML exceeds 2M characters"));
        }
        let proof=clean_numbered_import_txt(html,&dest)
            .map_err(|_|StageError::Invalid("Unproved source HTML: needs review before native confirm"))?;
        if proof.html!=html || proof.removed_text_nodes!=0 {
            return Err(StageError::Invalid("Source cleaner did not preserve exact staged bytes"));
        }
        writes.push(Write{
            seq,action:action.to_owned(),id:id.to_owned(),index,path:dest,
            html:html.to_owned(),hash:proof.source_hash,expected
        });
    }
    if writes.is_empty(){return Err(StageError::Conflict("Select at least one chapter"));}
    // All destination versions and ownership are verified under BEGIN IMMEDIATE
    // before any book, chapter, prefix or receipt INSERT/UPDATE.
    for entry in &writes {
        let old:Option<i64>=tx.query_row(
            "SELECT source_revision FROM book_chapters WHERE book_id=?1 AND chapter_id=?2",
            params![book_id,entry.id],|r|r.get(0)
        ).optional()?;
        if entry.action=="create"&&old.is_some(){
            return Err(StageError::Conflict("Create destination already exists"));
        }
        if entry.action=="replace"&&old.is_none(){
            return Err(StageError::Conflict("Replacement destination is missing"));
        }
        if entry.action=="replace"&&old!=Some(entry.expected) {
            return Err(StageError::Conflict("Stale chapter source revision"));
        }
        let mut stmt=tx.prepare(
            "SELECT chapter_id FROM book_chapters
             WHERE book_id=?1 AND (chapter_index=?2 OR chapter_path=?3)"
        )?;
        let owners=stmt.query_map(params![book_id,entry.index,entry.path],|r|r.get::<_,String>(0))?;
        for owner in owners {
            if owner?!=entry.id {
                return Err(StageError::Conflict("Chapter index/path belongs to another chapter"));
            }
        }
    }
    if mode=="create" {
        tx.execute(
            "INSERT INTO books(id,title,owner_member_id,created_at,updated_at) VALUES(?1,?2,?3,?4,?5)",
            params![book_id,title,owner,stamp,stamp]
        ).map_err(sql)?;
        tx.execute(
            "INSERT INTO book_owner_events
             (book_id,revision,previous_owner_member_id,new_owner_member_id,actor_id,changed_at)
             VALUES(?1,0,NULL,?2,?3,?4)",
            params![book_id,owner,actor,stamp]
        ).map_err(sql)?;
    }
    for entry in &writes {
        let old:Option<(i64,i64,String,String)>=tx.query_row(
            "SELECT source_revision,chapter_index,chapter_path,source_html
             FROM book_chapters WHERE book_id=?1 AND chapter_id=?2",
            params![book_id,entry.id],
            |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))
        ).optional()?;
        let revision=match old {
            Some((rev,idx,old_path,old_html)) if idx==entry.index
                &&old_path==entry.path&&old_html==entry.html=>rev,
            Some((rev,_,_,_))=>{
                tx.execute(
                    "UPDATE book_chapters SET chapter_index=?1,chapter_path=?2,source_html=?3,
                     source_hash=?4,source_revision=?5,updated_at=?6
                     WHERE book_id=?7 AND chapter_id=?8",
                    params![entry.index,entry.path,entry.html,entry.hash,rev+1,stamp,book_id,entry.id]
                ).map_err(sql)?;
                rev+1
            }
            None=>{
                tx.execute(
                    "INSERT INTO book_chapters(book_id,chapter_id,chapter_index,chapter_path,
                     source_html,source_hash,source_revision,updated_at)
                     VALUES(?1,?2,?3,?4,?5,?6,1,?7)",
                    params![book_id,entry.id,entry.index,entry.path,entry.html,entry.hash,stamp]
                ).map_err(sql)?;
                1
            }
        };
        result_chapters.push(json!({"sequence":entry.seq,"action":entry.action,
            "chapter_id":entry.id,"chapter_index":entry.index,"source_revision":revision}));
    }
    if has_prefix&&prefix_policy=="preserve" {
        let source_hash=hex::encode(Sha256::digest(prefix.as_bytes()));
        tx.execute(
            "INSERT INTO book_import_unassigned(book_id,stage_id,filename,source_html,
             source_sha256,actor_id,created_at) VALUES(?1,?2,?3,?4,?5,?6,?7)",
            params![book_id,stage_id,filename,prefix,source_hash,actor,stamp]
        ).map_err(sql)?;
    }
    result_chapters.sort_by_key(|v|v["sequence"].as_i64().unwrap_or(0));
    let receipt=json!({
        "stage_id":stage_id,"status":"committed","book_id":book_id,
        "created_book":mode=="create","imported_count":writes.len(),
        "skipped_count":candidates.len()-writes.len(),
        "unassigned_prefix_preserved":has_prefix&&prefix_policy=="preserve",
        "chapters":result_chapters
    });
    let packed=serde_json::to_string(&receipt).map_err(|_|StageError::Malformed)?;
    tx.execute(
        "UPDATE book_import_stages SET status='committed',payload_json=NULL,
         confirm_fingerprint=?1,commit_result_json=?2,expires_at=?3 WHERE id=?4 AND actor_id=?5",
        params![fingerprint,packed,stamp+86_400.0,stage_id,actor]
    ).map_err(sql)?;
    tx.commit()?;
    Ok(receipt)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::import_stage;

    fn db()->Connection{
        let c=Connection::open_in_memory().unwrap();
        c.execute_batch(r#"
            PRAGMA foreign_keys=ON;
            CREATE TABLE books(
                id TEXT PRIMARY KEY,title TEXT NOT NULL,owner_member_id TEXT,
                owner_revision INTEGER NOT NULL DEFAULT 0,
                glossary_json TEXT NOT NULL DEFAULT '{}',
                glossary_versions_json TEXT NOT NULL DEFAULT '{}',
                glossary_authority TEXT NOT NULL DEFAULT 'READY',
                glossary_revision INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,updated_at REAL NOT NULL
            );
            CREATE TABLE book_owner_events(
                book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL,previous_owner_member_id TEXT,
                new_owner_member_id TEXT NOT NULL,actor_id TEXT NOT NULL,
                changed_at REAL NOT NULL,PRIMARY KEY(book_id,revision)
            );
            CREATE TABLE book_chapters(
                book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                chapter_id TEXT NOT NULL,chapter_index INTEGER NOT NULL,
                chapter_path TEXT NOT NULL,source_html TEXT NOT NULL,
                source_hash TEXT NOT NULL,source_revision INTEGER NOT NULL,
                accepted_html TEXT,accepted_revision INTEGER NOT NULL DEFAULT 0,
                accepted_source_revision INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,PRIMARY KEY(book_id,chapter_id),
                UNIQUE(book_id,chapter_index),UNIQUE(book_id,chapter_path)
            );
            CREATE TABLE book_import_stages(
                id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,idempotency_key TEXT,
                input_fingerprint TEXT NOT NULL,original_sha256 TEXT NOT NULL,
                filename TEXT NOT NULL,status TEXT NOT NULL,
                preview_json TEXT NOT NULL,payload_json TEXT,prefix_html TEXT NOT NULL,
                confirm_fingerprint TEXT,commit_result_json TEXT,
                created_at REAL NOT NULL,expires_at REAL NOT NULL,
                UNIQUE(actor_id,idempotency_key)
            );
            CREATE TABLE book_import_unassigned(
                book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                stage_id TEXT NOT NULL,filename TEXT NOT NULL,
                source_html TEXT NOT NULL,source_sha256 TEXT NOT NULL,
                actor_id TEXT NOT NULL,created_at REAL NOT NULL,
                PRIMARY KEY(book_id,stage_id)
            );"#).unwrap();
        c
    }
    fn src()->String{
        let narrative="События продолжаются вдоль широкой реки. ".repeat(20);
        format!("Глава 1\n{narrative}\nГлава 2\n{narrative}")
    }
    fn stage(db:&mut Connection,data:&str)->String{
        import_stage::stage(db,"alice","story.txt",data.as_bytes(),None).unwrap()
            ["stage_id"].as_str().unwrap().to_owned()
    }
    fn request()->Value{
        json!({
            "book_id":"book-1","book_mode":"create","title":"История",
            "owner_member_id":"alice","prefix_policy":"discard",
            "chapters":[
                {"sequence":2,"action":"create","chapter_id":"c2",
                 "chapter_index":2,"chapter_path":"chapters/2.xhtml",
                 "expected_revision":0},
                {"sequence":1,"action":"create","chapter_id":"c1",
                 "chapter_index":1,"chapter_path":"chapters/1.xhtml",
                 "expected_revision":0}
            ]
        })
    }
    fn count(db:&Connection,table:&str)->i64{
        db.query_row(&format!("SELECT COUNT(*) FROM {table}"),[],|r|r.get(0)).unwrap()
    }

    #[test]
    fn two_chapter_confirm_is_atomic_canonical_and_idempotent(){
        let mut c=db();
        let stage_id=stage(&mut c,&src());
        let receipt=confirm(&mut c,"alice",&stage_id,request()).unwrap();
        assert_eq!(receipt["status"],"committed");
        assert_eq!(receipt["created_book"],true);
        assert_eq!(receipt["imported_count"],2);
        assert_eq!(receipt["skipped_count"],0);
        assert_eq!(receipt["chapters"][0]["sequence"],1);
        assert_eq!(receipt["chapters"][1]["sequence"],2);
        assert_eq!(count(&c,"books"),1);
        assert_eq!(count(&c,"book_owner_events"),1);
        assert_eq!(count(&c,"book_chapters"),2);
        assert_eq!(count(&c,"book_import_unassigned"),0);
        let (html,digest):(String,String)=c.query_row(
            "SELECT source_html,source_hash FROM book_chapters WHERE book_id='book-1'
             AND chapter_id='c1'",[],|r|Ok((r.get(0)?,r.get(1)?))
        ).unwrap();
        assert!(html.starts_with("<h1>Глава 1</h1>\n<p>"));
        assert_eq!(digest,hex::encode(Sha256::digest(html.as_bytes())));
        assert_eq!(confirm(&mut c,"alice",&stage_id,request()).unwrap(),receipt);
        assert_eq!(count(&c,"book_chapters"),2);
        assert_eq!(count(&c,"book_owner_events"),1);
        let payload:Option<String>=c.query_row(
            "SELECT payload_json FROM book_import_stages WHERE id=?1",
            [stage_id],|r|r.get(0)
        ).unwrap();
        assert!(payload.is_none(),"source is purged after committed receipt");
    }

    #[test]
    fn second_insert_sql_error_rolls_back_book_first_chapter_and_receipt(){
        let mut c=db();
        let id=stage(&mut c,&src());
        c.execute_batch(r#"
            CREATE TRIGGER inject_failure BEFORE INSERT ON book_chapters
            WHEN NEW.chapter_id='c2'
            BEGIN SELECT RAISE(FAIL,'simulate disk/constraint error'); END;
        "#).unwrap();
        assert!(confirm(&mut c,"alice",&id,request()).is_err());
        for table in ["books","book_owner_events","book_chapters","book_import_unassigned"] {
            assert_eq!(count(&c,table),0,"partial state in {table}");
        }
        let state:String=c.query_row("SELECT status FROM book_import_stages WHERE id=?1",
            [id.as_str()],|r|r.get(0)).unwrap();
        assert_eq!(state,"staged");
        c.execute_batch("DROP TRIGGER inject_failure").unwrap();
        assert_eq!(confirm(&mut c,"alice",&id,request()).unwrap()["imported_count"],2);
    }

    #[test]
    fn unknown_actor_changed_decision_and_unproved_html_fail_closed(){
        let mut c=db();
        let id=stage(&mut c,&src());
        assert!(matches!(confirm(&mut c,"bob",&id,request()),Err(StageError::Missing)));
        let mut duplicate=request();
        duplicate["chapters"][1]["sequence"]=json!(2);
        assert!(matches!(confirm(&mut c,"alice",&id,duplicate),Err(StageError::Conflict(_))));
        assert_eq!(count(&c,"book_chapters"),0);
        let bad=stage(&mut c,"Глава 1\nA & B\nГлава 2\nВторая.");
        assert!(matches!(
            confirm(&mut c,"alice",&bad,request()),
            Err(StageError::Invalid("Unproved source HTML: needs review before native confirm"))
        ));
        assert_eq!(count(&c,"books"),0);
        assert_eq!(count(&c,"book_owner_events"),0);
        assert_eq!(count(&c,"book_chapters"),0);
    }

    #[test]
    fn new_commit_receipt_cannot_be_replayed_with_different_choices(){
        let mut c=db();
        let id=stage(&mut c,&src());
        confirm(&mut c,"alice",&id,request()).unwrap();
        let mut changed=request();
        changed["chapters"][0]["chapter_id"]=json!("reassigned");
        assert!(matches!(confirm(&mut c,"alice",&id,changed),Err(StageError::Conflict(_))));
        assert_eq!(count(&c,"book_chapters"),2);
    }
}
