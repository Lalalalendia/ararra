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
    owner_member_id: str
    previous_owner_member_ids: tuple[str, ...]
    chapter_index: int
    source: bytes
    translation: bytes


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _clean_name(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:72].rstrip(" .") or "Книга")


def _managed_folder(
    drive: GoogleDriveClient, children: list[dict], *, folder_name: str,
    marker: str, identity: str, expected_tags: dict[str, str] | None = None,
) -> str | None:
    """Find exactly one app-owned folder; never adopt same-name foreign files."""
    owned = [
        item for item in children
        if isinstance(item.get("appProperties"), dict)
        and item["appProperties"].get(marker) == identity
    ]
    named = [item for item in children if item.get("name") == folder_name]
    if len(owned) > 1 or len(named) > 1:
        raise DriveExportError("Duplicate managed Google Drive folder identity")
    if named and (not owned or named[0]["id"] != owned[0]["id"]):
        raise DriveExportError("Drive folder name belongs to a different identity")
    if not owned:
        return None
    item = owned[0]
    tags = item["appProperties"]
    if item.get("mimeType") != FOLDER_MIME or (
        expected_tags and any(tags.get(k) != v for k, v in expected_tags.items())
    ):
        raise DriveExportError("Managed Google Drive folder has invalid ownership tags")
    return drive._check_id(item["id"])


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
            "SELECT title,glossary_revision,owner_member_id,owner_revision "
            "FROM books WHERE id=?", (book_id,),
        ).fetchone()
        chapter = con.execute(
            "SELECT chapter_index,source_html,source_hash,source_revision "
            "FROM book_chapters WHERE book_id=? AND chapter_id=?",
            (book_id,chapter_id),
        ).fetchone()
        if book is None or chapter is None:
            raise DriveExportError("Book or source chapter no longer exists")
        owner = book["owner_member_id"]
        if not isinstance(owner, str) or not owner.strip() or owner != owner.strip():
            raise DriveExportError("Book has no canonical moderator owner")
        history = con.execute(
            "SELECT previous_owner_member_id FROM book_owner_events "
            "WHERE book_id=? AND revision>0 ORDER BY revision",
            (book_id,),
        ).fetchall()
        if len(history) != book["owner_revision"]:
            raise DriveExportError("Book owner transfer history is incomplete")
        previous_owners = tuple(dict.fromkeys(
            str(row["previous_owner_member_id"])
            for row in history
            if row["previous_owner_member_id"] and row["previous_owner_member_id"] != owner
        ))
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
        return DriveSource(
            job_id, book_id, str(book["title"]), owner, previous_owners,
            index, original, ru,
        )


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
    """Create only: roots / moderator / book / chapter, preserving remote edits."""
    if source_folder_id == translation_folder_id:
        raise DriveExportError("Sources and translations must use distinct Drive folders")
    snapshot = read_completed_job(store, job_id)
    drive.check_destination(source_folder_id)
    drive.check_destination(translation_folder_id)

    book_key = _digest(snapshot.book_id.encode("utf-8"))[:20]
    moderator_key = _digest(snapshot.owner_member_id.encode("utf-8"))[:20]
    book_name = f"{_clean_name(snapshot.book_title)} [{book_key[:10]}]"
    moderator_name = _clean_name(snapshot.owner_member_id)
    book_tags = {"readeraBookKey": book_key, "readeraModeratorKey": moderator_key}
    moderator_tags = {"readeraModeratorKey": moderator_key}

    destinations = (
        ("source", source_folder_id, snapshot.source),
        ("translation", translation_folder_id, snapshot.translation),
    )

    # Inspect BOTH sides before the first mutation. The historical layout
    # root/book must be handled through a separate operator-approved move:
    # creating root/moderator/book would leave a shadow copy of edited files.
    plans: list[tuple[str, str, bytes, str | None, str | None]] = []
    for part, root, data in destinations:
        root_children = drive.children(root)
        if any(
            (item.get("appProperties") or {}).get("readeraBookKey") == book_key
            or item.get("name") == book_name
            for item in root_children
        ):
            raise DriveExportError("Legacy root-level book requires reviewed folder migration")

        # Reassignment is tracked in SQLite; do not duplicate an existing book
        # under a new moderator if another owner previously exported it.
        for prior in snapshot.previous_owner_member_ids:
            prior_key = _digest(prior.encode("utf-8"))[:20]
            prior_folder = _managed_folder(
                drive, root_children, folder_name=_clean_name(prior),
                marker="readeraModeratorKey", identity=prior_key,
                expected_tags={"readeraModeratorKey": prior_key},
            )
            if prior_folder is None:
                continue
            if any(
                (entry.get("appProperties") or {}).get("readeraBookKey") == book_key
                or entry.get("name") == book_name
                for entry in drive.children(prior_folder)
            ):
                raise DriveExportError(
                    "Book was exported by a previous moderator; reviewed transfer required"
                )

        moderator_folder = _managed_folder(
            drive, root_children, folder_name=moderator_name,
            marker="readeraModeratorKey", identity=moderator_key,
            expected_tags=moderator_tags,
        )
        book_folder = None
        if moderator_folder:
            book_folder = _managed_folder(
                drive, drive.children(moderator_folder), folder_name=book_name,
                marker="readeraBookKey", identity=book_key,
                expected_tags=book_tags,
            )
            if book_folder is not None:
                existing_chapters = drive.children(book_folder)
                filename = f"Глава {snapshot.chapter_index:03d}.txt"
                props = {
                    "readeraBookKey": book_key, "readeraJobId": snapshot.job_id,
                    "readeraPart": part, "readeraContentSha256": _digest(data),
                }
                # Check a foreign chapter collision on either side BEFORE
                # creating anything, including the other side's directories.
                owned = [x for x in existing_chapters if x.get("appProperties") == props]
                named = [x for x in existing_chapters if x.get("name") == filename]
                if len(owned) > 1 or len(named) > 1 or (
                    named and (not owned or named[0]["id"] != owned[0]["id"])
                ):
                    raise DriveExportError(
                        "Existing chapter filename or Drive identity is ambiguous"
                    )
                if owned and owned[0].get("mimeType") != TEXT_MIME:
                    raise DriveExportError("Existing job export has incorrect content type")
        plans.append((part, root, data, moderator_folder, book_folder))

    receipt: dict[str, str | int | bool] = {}
    for part, root, data, moderator_folder, book_folder in plans:
        if read_completed_job(store, job_id) != snapshot:
            raise DriveExportError("Book, moderator owner, or translation changed during Drive export")
        if moderator_folder is None:
            created = drive.create_metadata(root, {
                "name": moderator_name, "mimeType": FOLDER_MIME,
                "appProperties": moderator_tags,
            })
            moderator_folder = created["id"]
        if book_folder is None:
            # Re-list to catch collisions if this parent was created earlier
            # or another writer modified it since the initial preflight.
            children = drive.children(moderator_folder)
            book_folder = _managed_folder(
                drive, children, folder_name=book_name,
                marker="readeraBookKey", identity=book_key,
                expected_tags=book_tags,
            )
            if book_folder is None:
                created = drive.create_metadata(moderator_folder, {
                    "name": book_name, "mimeType": FOLDER_MIME,
                    "appProperties": book_tags,
                })
                book_folder = created["id"]

        filename = f"Глава {snapshot.chapter_index:03d}.txt"
        props = {
            "readeraBookKey": book_key, "readeraJobId": snapshot.job_id,
            "readeraPart": part, "readeraContentSha256": _digest(data),
        }
        existing = drive.children(book_folder)
        owned = [x for x in existing if x.get("appProperties") == props]
        named = [x for x in existing if x.get("name") == filename]
        if len(owned) > 1 or len(named) > 1:
            raise DriveExportError("Duplicate chapter filenames or Drive file identity")
        if named and (not owned or named[0]["id"] != owned[0]["id"]):
            raise DriveExportError("Existing chapter is not this job's exported file")
        if owned:
            if owned[0].get("mimeType") != TEXT_MIME:
                raise DriveExportError("Existing job export has incorrect content type")
            file_id = drive._check_id(owned[0]["id"])
        else:
            item = drive.upload_text(
                book_folder,
                {"name": filename, "mimeType": TEXT_MIME, "appProperties": props},
                data,
            )
            file_id = item["id"]
        receipt[part + "_file_id"] = file_id
        receipt[part + "_moderator_folder_id"] = moderator_folder
        receipt[part + "_book_folder_id"] = book_folder

    return {
        "job_id": snapshot.job_id,
        "book_id": snapshot.book_id,
        "owner_member_id": snapshot.owner_member_id,
        "chapter_index": snapshot.chapter_index,
        **receipt,
        "status": "exported_or_already_present",
        "remote_edits_preserved": True,
    }
