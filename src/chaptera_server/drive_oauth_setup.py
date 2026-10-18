"""Owner-mediated first Google Drive authorization for the two existing folders.

Run on the owner's trusted workstation (not a remote headless VPS).
Google Picker OnePick grants the desktop OAuth client narrow drive.file
access only to folders the owner explicitly selected. The resulting refresh
token is written into an operator-chosen private file, NEVER printed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import secrets
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .drive_export import DriveExportError, GoogleDriveClient

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
CONSENT_SECONDS = 240


class DriveAuthorizationError(ValueError):
    """Auth incomplete; remote storage and existing credentials untouched."""


@dataclass(frozen=True)
class ConsentRequest:
    url: str
    verifier: str
    state: str


def make_consent_request(
    client_id: str, redirect_uri: str, source_folder_id: str,
    translation_folder_id: str, *,
    verifier: str | None = None, state: str | None = None,
) -> ConsentRequest:
    for folder in (source_folder_id,translation_folder_id):
        GoogleDriveClient._check_id(folder)
    if source_folder_id == translation_folder_id:
        raise DriveAuthorizationError("Source and translation folders must differ")
    if not client_id.strip():
        raise DriveAuthorizationError("Google OAuth client ID is missing")
    url=urlsplit(redirect_uri)
    if (url.scheme != "http" or url.hostname != "127.0.0.1"
        or not url.port or url.path != "/oauth2callback"
        or url.username or url.password or url.query or url.fragment):
        raise DriveAuthorizationError("OAuth redirect must be an exact loopback callback")
    proof=verifier or secrets.token_urlsafe(64)
    request_state=state or secrets.token_urlsafe(32)
    if not 43 <= len(proof) <= 128:
        raise DriveAuthorizationError("OAuth PKCE verifier has invalid length")
    challenge=base64.urlsafe_b64encode(
        hashlib.sha256(proof.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    params={
        "client_id":client_id,"redirect_uri":redirect_uri,
        "response_type":"code","scope":DRIVE_FILE_SCOPE,
        "access_type":"offline","prompt":"consent select_account",
        "code_challenge":challenge,"code_challenge_method":"S256",
        "state":request_state,
        "trigger_onepick":"true","allow_multiple":"true",
        "allow_folder_selection":"true",
        "mimetypes":"application/vnd.google-apps.folder",
        "file_ids":",".join((source_folder_id,translation_folder_id)),
        "include_granted_scopes":"false",
    }
    return ConsentRequest(
        "https://accounts.google.com/o/oauth2/v2/auth?"+urlencode(params),
        proof,request_state,
    )


def verify_callback(
    callback_path: str, *, state: str,
    source_folder_id: str, translation_folder_id: str,
) -> str:
    parsed=urlsplit(callback_path)
    if parsed.path != "/oauth2callback" or parsed.fragment or len(callback_path)>8192:
        raise DriveAuthorizationError("Unexpected OAuth callback path")
    query=parse_qs(parsed.query,keep_blank_values=True)
    if "error" in query:
        raise DriveAuthorizationError("Google OAuth consent was cancelled or refused")
    if any(len(query.get(key,()))!=1 for key in ("state","code","picked_file_ids")):
        raise DriveAuthorizationError("Incomplete or ambiguous OAuth callback")
    if not secrets.compare_digest(query["state"][0],state):
        raise DriveAuthorizationError("OAuth state mismatch")
    picked=query["picked_file_ids"][0].split(",")
    expected={source_folder_id,translation_folder_id}
    if len(picked)!=2 or len(set(picked))!=2 or set(picked)!=expected:
        raise DriveAuthorizationError("Both existing Drive folders must be selected")
    code=query["code"][0]
    if not code or len(code)>4096:
        raise DriveAuthorizationError("Invalid OAuth authorization code")
    return code


def exchange_code(
    client: httpx.Client, *, client_id: str, client_secret: str,
    redirect_uri: str, code: str, verifier: str,
) -> str:
    try:
        response=client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id":client_id,"client_secret":client_secret,
                "grant_type":"authorization_code",
                "redirect_uri":redirect_uri,"code":code,
                "code_verifier":verifier,
            },
            timeout=20.0,follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise DriveAuthorizationError("Google OAuth code exchange unavailable") from exc
    if response.status_code != 200:
        raise DriveAuthorizationError(
            f"Google OAuth code exchange rejected (HTTP {response.status_code})"
        )
    try:
        data=response.json()
    except ValueError as exc:
        raise DriveAuthorizationError("Malformed Google OAuth token response") from exc
    if not isinstance(data,dict):
        raise DriveAuthorizationError("Malformed Google OAuth token response")
    scopes=data.get("scope")
    if scopes and scopes.split()!=[DRIVE_FILE_SCOPE]:
        raise DriveAuthorizationError("Google granted an unexpected OAuth scope")
    token=data.get("refresh_token")
    if not isinstance(token,str) or not token or len(token)>8192:
        raise DriveAuthorizationError(
            "No durable refresh token. Confirm offline access and rerun consent"
        )
    return token


def load_desktop_oauth_client(path: Path) -> tuple[str, str]:
    """Read an operator-downloaded Google *Desktop* client JSON locally.

    Reject web-client credentials: these generally require fixed redirect
    URIs and cannot use this temporary loopback callback.
    """
    if not path.is_file() or path.stat().st_size > 64 * 1024:
        raise DriveAuthorizationError("Desktop OAuth client JSON missing or too large")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DriveAuthorizationError("Invalid Desktop OAuth client JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("installed"),dict):
        raise DriveAuthorizationError("Select a Desktop app OAuth client, not a Web application")
    value=payload["installed"]
    client_id=value.get("client_id")
    client_secret=value.get("client_secret")
    if (not isinstance(client_id,str) or not client_id.endswith(".apps.googleusercontent.com")
        or not isinstance(client_secret,str) or not client_secret
        or len(client_id)>300 or len(client_secret)>500):
        raise DriveAuthorizationError("Desktop OAuth client ID or secret is invalid")
    return client_id,client_secret


def _restrict_windows_owner_acl(path: Path) -> None:
    """Remove inherited ACLs and allow only the current Windows account.

    os.chmod(0o600) is NOT an ACL control on Windows. Apply a restrictive
    file DACL BEFORE writing any OAuth secret, or fail without saving.
    """
    try:
        who=subprocess.run(
            ["whoami.exe","/user","/fo","csv","/nh"],
            capture_output=True,text=True,timeout=10,check=True,
        )
        sids=re.findall(r"S-\d+(?:-\d+){2,}",who.stdout)
        if len(sids)!=1:
            raise DriveAuthorizationError("Could not identify Windows owner SID")
        subprocess.run(
            ["icacls.exe",str(path),"/inheritance:r","/grant:r",f"*{sids[0]}:F"],
            capture_output=True,text=True,timeout=10,check=True,
        )
    except (OSError,subprocess.SubprocessError) as exc:
        raise DriveAuthorizationError(
            "Cannot restrict Windows file permissions; refresh token was not saved"
        ) from exc


def save_private_token(path: Path, token: str) -> None:
    """Refuse symlinks, relative paths and overwrite of an existing credential."""
    if not path.is_absolute() or not path.parent.is_dir():
        raise DriveAuthorizationError("Token destination must be in an existing absolute directory")
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0)
    try:
        fd=os.open(path,flags,0o600)
    except OSError as exc:
        raise DriveAuthorizationError("Cannot create private token file without overwrite") from exc
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as out:
            if os.name == "nt":
                _restrict_windows_owner_acl(path)
            else:
                os.fchmod(out.fileno(),0o600)
            out.write(token+"\n")
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def authorize_owner(
    *, client_id: str, client_secret: str,
    source_folder_id: str, translation_folder_id: str,
    token_file: Path, open_browser: bool = True,
    client: httpx.Client | None = None,
) -> dict:
    """Local-only consent with state+PKCE, verify both folders, then save token."""
    if not client_secret:
        raise DriveAuthorizationError("Configured OAuth client secret is missing")
    if not token_file.is_absolute() or token_file.exists():
        raise DriveAuthorizationError("Use a new, absolute token file path")
    with HTTPServer(("127.0.0.1",0),_ConsentHandler) as server:
        server.expected_host=f"127.0.0.1:{server.server_port}"
        redirect=f"http://{server.expected_host}/oauth2callback"
        consent=make_consent_request(
            client_id,redirect,source_folder_id,translation_folder_id,
        )
        server.callback=None
        server.denied=False
        server.state=consent.state
        server.source_folder_id=source_folder_id
        server.translation_folder_id=translation_folder_id
        print("Google Drive authorization URL (open on this same workstation):")
        print(consent.url)
        if open_browser:
            webbrowser.open(consent.url)
        deadline=time.monotonic()+CONSENT_SECONDS
        while server.callback is None and not server.denied:
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise DriveAuthorizationError("Google Drive owner consent timed out")
            server.timeout=min(0.8,remaining)
            server.handle_request()
        if server.denied:
            raise DriveAuthorizationError("Google Drive owner cancelled consent")
        code=server.callback
    owns_client=client is None
    http=client or httpx.Client(
        timeout=30.0,trust_env=False,follow_redirects=False,
    )
    try:
        refresh_token=exchange_code(
            http,client_id=client_id,client_secret=client_secret,
            redirect_uri=redirect,code=code,verifier=consent.verifier,
        )
        drive=GoogleDriveClient(
            client_id=client_id,client_secret=client_secret,
            refresh_token=refresh_token,http=http,
        )
        try:
            drive.check_destination(source_folder_id)
            drive.check_destination(translation_folder_id)
        except DriveExportError as exc:
            raise DriveAuthorizationError(
                "OAuth did not grant writable access to BOTH selected folders"
            ) from exc
        save_private_token(token_file,refresh_token)
    finally:
        if owns_client:
            http.close()
    return {
        "authorized":True,
        "source_folder_id":source_folder_id,
        "translation_folder_id":translation_folder_id,
        "credential_path":str(token_file),
        "token_printed":False,
    }


class _ConsentHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        # Never log URL query strings containing authorization codes.
        return

    def do_GET(self) -> None:
        server=self.server
        if self.headers.get("Host") != server.expected_host:
            self._reply(400,"Invalid OAuth callback origin")
            return
        try:
            code=verify_callback(
                self.path,state=server.state,
                source_folder_id=server.source_folder_id,
                translation_folder_id=server.translation_folder_id,
            )
        except DriveAuthorizationError as exc:
            if "cancelled" in str(exc):
                server.denied=True
            self._reply(400,"The Google Drive selection was not accepted")
            return
        server.callback=code
        self._reply(200,"Both folders selected. Return to the application.")

    def _reply(self,status: int,message: str) -> None:
        body=(message+"\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type","text/plain; charset=utf-8")
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)
