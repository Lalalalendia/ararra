"""RE-09A: one completed book-translation job -> two editable Drive TXT files.

This is *export*, not two-way sync, publication, or an accepted translation.
Never update or delete a remote file: a moderator/ChatGPT may have edited it.
Google Drive has no two-file transaction; retries reconcile created files.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .storage import QueueStore

FOLDER_MIME = "application/vnd.google-apps.folder"
TEXT_MIME = "text/plain"
MAX_TXT_BYTES = 4 * 1024 * 1024
MAX_LIST_PAGES = 30
_ID = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")


class DriveExportError(ValueError):
    """A failed or ambiguous export must not be reported as complete."""


@dataclass(frozen=True)
class DriveSource:
    job_id: str
    book_id: str
    book_title: str
    chapter_index: int
    source: bytes
    translation: bytes


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _clean_name(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:72].rstrip(" .") or "Книга")


def read_completed_job(store: QueueStore, job_id: str) -> DriveSource:
    """Take original + machine RU from one SQLite snapshot and pin live input.

    This deliberately does NOT query the accepted book export: the first
    handoff to ChatGPT must precede moderator acceptance/publication.
    """
    if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
        raise DriveExportError("A valid translation job ID is required")
    with store._connect() as con:
        con.execute("BEGIN")
        job = con.execute(
            "SELECT status,input_json,result_html,source_hash,actor_id "
            "FROM jobs WHERE id=?", (job_id,),
        ).fetchone()
        if job is None or job["status"] != "completed" or not job["result_html"]:
            raise DriveExportError("Translation job is not completed")
        try:
            request = json.loads(job["input_json"])
        except (ValueError, TypeError) as exc:
            raise DriveExportError("Invalid stored translation job input") from exc
        if not isinstance(request, dict):
            raise DriveExportError("Invalid stored translation job input")
        book_id = request.get("book_id")
        chapter_id = request.get("chapter_id")
        if not isinstance(book_id,str) or not book_id or not isinstance(chapter_id,str) or not chapter_id:
            raise DriveExportError("Only book-owned translation jobs can be exported")
        book = con.execute(
            "SELECT title,glossary_revision FROM books WHERE id=?", (book_id,),
        ).fetchone()
        chapter = con.execute(
            "SELECT chapter_index,source_html,source_hash,source_revision "
            "FROM book_chapters WHERE book_id=? AND chapter_id=?",
            (book_id,chapter_id),
        ).fetchone()
        if book is None or chapter is None:
            raise DriveExportError("Book or source chapter no longer exists")
        if (request.get("book_chapter_revision") != chapter["source_revision"]
                or request.get("book_glossary_revision") != book["glossary_revision"]
                or request.get("source_html") != chapter["source_html"]
                or job["source_hash"] != chapter["source_hash"]):
            raise DriveExportError("Translation's source or glossary has changed")
        index = chapter["chapter_index"]
        if type(index) is not int or not 1 <= index <= 10_000_000:
            raise DriveExportError("Invalid stored chapter index")
        original = chapter["source_html"].encode("utf-8")
        ru = job["result_html"].encode("utf-8")
        if not original.strip() or not ru.strip() or max(len(original), len(ru)) > MAX_TXT_BYTES:
            raise DriveExportError("Source or machine translation outside 4 MiB export limit")
        return DriveSource(job_id, book_id, str(book["title"]), index, original, ru)


class GoogleDriveClient:
    """Minimal Drive v3 API transport. No wider file scopes or redirects."""

    def __init__(
        self, *, client_id: str, client_secret: str, refresh_token: str,
        http: httpx.Client | None = None,
    ):
        if not all((client_id,client_secret,refresh_token)):
            raise DriveExportError("Google Drive OAuth credentials are not configured")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.http = http or httpx.Client(timeout=30.0,trust_env=False,follow_redirects=False)
        self._token: str | None = None

    def _access_token(self) -> str:
        if self._token:
            return self._token
        try:
            response = self.http.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id":self.client_id,
                    "client_secret":self.client_secret,
                    "refresh_token":self.refresh_token,
                    "grant_type":"refresh_token",
                },
                headers={"Accept":"application/json"},
                timeout=20.0,
            )
        except httpx.HTTPError as exc:
            raise DriveExportError("Google OAuth token endpoint unavailable") from exc
        if response.status_code != 200:
            raise DriveExportError(f"Google OAuth refresh refused ({response.status_code})")
        try:
            value = response.json().get("access_token")
        except (ValueError, AttributeError) as exc:
            raise DriveExportError("Invalid Google OAuth response") from exc
        if not isinstance(value,str) or not value:
            raise DriveExportError("Google OAuth returned no access token")
        self._token = value
        return value

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if not (url.startswith("https://www.googleapis.com/drive/v3/") or
                url.startswith("https://www.googleapis.com/upload/drive/v3/")):
            raise DriveExportError("Refusing untrusted Google Drive endpoint")
        try:
            response = self.http.request(
                method,url,
                headers={"Authorization":f"Bearer {self._access_token()}",
                         "Accept":"application/json",**kwargs.pop("headers",{})},
                follow_redirects=False,timeout=30.0,**kwargs,
            )
        except httpx.HTTPError as exc:
            raise DriveExportError("Google Drive request failed; remote result is uncertain") from exc
        if response.status_code not in (200,201):
            # Do not include response body: it may expose remote metadata.
            raise DriveExportError(f"Google Drive {method} returned HTTP {response.status_code}")
        if len(response.content) > MAX_TXT_BYTES + 1024 * 1024:
            raise DriveExportError("Google Drive response exceeds configured byte limit")
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            obj = response.json()
        except ValueError as exc:
            raise DriveExportError("Google Drive returned malformed JSON") from exc
        if not isinstance(obj,dict):
            raise DriveExportError("Google Drive returned invalid JSON type")
        return obj

    @staticmethod
    def _check_id(file_id: str) -> str:
        if not isinstance(file_id,str) or not _ID.fullmatch(file_id):
            raise DriveExportError("Invalid Google Drive folder or file ID")
        return file_id

    def check_destination(self, folder_id: str) -> None:
        fid = self._check_id(folder_id)
        data = self._json(self._request(
            "GET",f"https://www.googleapis.com/drive/v3/files/{fid}",
            params={"fields":"id,name,mimeType,trashed,capabilities(canAddChildren)"},
        ))
        if (data.get("id") != fid or data.get("mimeType") != FOLDER_MIME or
            data.get("trashed") is True or
            data.get("capabilities",{}).get("canAddChildren") is not True):
            raise DriveExportError("Configured Drive folder is missing or not writable")

    def children(self, folder_id: str) -> list[dict]:
        fid = self._check_id(folder_id)
        output: list[dict] = []
        token: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_LIST_PAGES):
            params = {
                "q":f"'{fid}' in parents and trashed = false",
                "pageSize":1000,"spaces":"drive",
                "fields":"nextPageToken,incompleteSearch,files(id,name,mimeType,parents,appProperties,trashed)",
            }
            if token:
                params["pageToken"] = token
            data = self._json(self._request(
                "GET","https://www.googleapis.com/drive/v3/files",params=params,
            ))
            rows = data.get("files")
            if not isinstance(rows,list) or data.get("incompleteSearch"):
                raise DriveExportError("Google Drive folder listing is incomplete")
            if not all(isinstance(x,dict) and isinstance(x.get("id"),str) for x in rows):
                raise DriveExportError("Google Drive folder listing has malformed entries")
            output.extend(rows)
            token = data.get("nextPageToken")
            if token is None or token == "":
                return output
            if not isinstance(token,str) or token in seen:
                raise DriveExportError("Google Drive pagination is invalid")
            seen.add(token)
        raise DriveExportError("Google Drive folder requires too many listing pages")

    def create_metadata(self, parent: str, metadata: dict) -> dict:
        pid = self._check_id(parent)
        obj = dict(metadata)
        obj["parents"] = [pid]
        result = self._json(self._request(
            "POST","https://www.googleapis.com/drive/v3/files",
            params={"fields":"id,name,mimeType,parents,appProperties"},
            json=obj,
        ))
        self._validate_created(result,obj,pid)
        return result

    @staticmethod
    def _validate_created(result: dict, expected: dict, parent: str) -> None:
        if (not _ID.fullmatch(str(result.get("id",""))) or
            result.get("name") != expected.get("name") or
            result.get("mimeType") != expected.get("mimeType") or
            parent not in result.get("parents",[]) or
            result.get("appProperties") != expected.get("appProperties")):
            raise DriveExportError("Created Google Drive file identity was not confirmed")

    def upload_text(self, parent: str, metadata: dict, payload: bytes) -> dict:
        pid = self._check_id(parent)
        if not payload or len(payload) > MAX_TXT_BYTES:
            raise DriveExportError("Drive TXT outside 4 MiB size limit")
        obj = dict(metadata)
        obj["parents"]=[pid]
        boundary="readera"+secrets.token_hex(12)
        head=(
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            + json.dumps(obj,ensure_ascii=False,separators=(",",":"))
            + f"\r\n--{boundary}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n"
        ).encode("utf-8")
        body=head+payload+f"\r\n--{boundary}--\r\n".encode("ascii")
        result = self._json(self._request(
            "POST","https://www.googleapis.com/upload/drive/v3/files",
            params={"uploadType":"multipart",
                    "fields":"id,name,mimeType,parents,appProperties"},
            headers={"Content-Type":f"multipart/related; boundary={boundary}"},
            content=body,
        ))
        self._validate_created(result,obj,pid)
        return result


def export_completed_job(
    store: QueueStore, drive: GoogleDriveClient, job_id: str,
    *, source_folder_id: str, translation_folder_id: str,
) -> dict:
    """Create only; retries reconcile unchanged files and preserve both edits."""
    if source_folder_id == translation_folder_id:
        raise DriveExportError("Sources and translations must use distinct Drive folders")
    snapshot = read_completed_job(store,job_id)
    # Check *both* destinations before first write. Failure doesn't create
    # a half-complete new pair when access is invalid from the outset.
    drive.check_destination(source_folder_id)
    drive.check_destination(translation_folder_id)
    book_key=_digest(snapshot.book_id.encode("utf-8"))[:20]
    title=_clean_name(snapshot.book_title)
    folder_name=f"{title} [{book_key[:10]}]"
    receipt:dict[str,str]={}
    for part,root,data in (
        ("source",source_folder_id,snapshot.source),
        ("translation",translation_folder_id,snapshot.translation),
    ):
        if read_completed_job(store,job_id) != snapshot:
            raise DriveExportError("Source or translation changed during Drive export")
        folder_matches=[x for x in drive.children(root) if x.get("name")==folder_name]
        if len(folder_matches)>1:
            raise DriveExportError("Multiple folders with the same book identity")
        if folder_matches:
            folder=folder_matches[0]
            if (folder.get("mimeType")!=FOLDER_MIME or
                folder.get("appProperties",{}).get("readeraBookKey")!=book_key):
                raise DriveExportError("Book folder exists but is not owned by ReadEra")
            folder_id=drive._check_id(folder["id"])
        else:
            folder=drive.create_metadata(root,{
                "name":folder_name,"mimeType":FOLDER_MIME,
                "appProperties":{"readeraBookKey":book_key},
            })
            folder_id=folder["id"]
        filename=f"Глава {snapshot.chapter_index:03d}.txt"
        content_sha=_digest(data)
        props={
            "readeraBookKey":book_key,"readeraJobId":snapshot.job_id,
            "readeraPart":part,"readeraContentSha256":content_sha,
        }
        matches=[x for x in drive.children(folder_id) if x.get("name")==filename]
        if len(matches)>1:
            raise DriveExportError("Duplicate chapter filenames in Drive book folder")
        if matches:
            remote=matches[0]
            if (remote.get("mimeType")!=TEXT_MIME or
                remote.get("appProperties")!=props):
                raise DriveExportError("Existing chapter is not this job's exported file")
            # The remote may have been edited. Preserve it without reading or
            # comparing its current body to the original upload.
            file_id=drive._check_id(remote["id"])
        else:
            item=drive.upload_text(folder_id,{
                "name":filename,"mimeType":TEXT_MIME,"appProperties":props,
            },data)
            file_id=item["id"]
        receipt[part+"_file_id"]=file_id
    return {
        "job_id":snapshot.job_id,
        "book_id":snapshot.book_id,
        "chapter_index":snapshot.chapter_index,
        **receipt,
        "status":"exported_or_already_present",
        "remote_edits_preserved":True,
    }
