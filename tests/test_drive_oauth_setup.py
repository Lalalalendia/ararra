"""RE-09A owner bootstrap: least privilege, PKCE, picked folders and secret safety."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import stat
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import httpx
import pytest

import chaptera_server.drive_oauth_setup as oauth
from chaptera_server.drive_export import DriveExportError

SOURCES="1daqCzEVgPdX639YV5otTLxgVTPbfeBEp"
TRANSLATIONS="1X2ZP8Fzbnq5MEAWJyGSErVGGFkfGBrR9"
REDIRECT="http://127.0.0.1:47851/oauth2callback"


def consent(*, verifier="v"*60,state="safe-oauth-state"):
    return oauth.make_consent_request(
        "desktop-app-client-id.apps.googleusercontent.com",
        REDIRECT,SOURCES,TRANSLATIONS,
        verifier=verifier,state=state,
    )


def make_callback(state="safe-oauth-state",picked=None,code="consent-code"):
    picked = picked if picked is not None else f"{SOURCES},{TRANSLATIONS}"
    return "/oauth2callback?"+urlencode({
        "state":state,"code":code,"picked_file_ids":picked,
        "scope":oauth.DRIVE_FILE_SCOPE,
    })


def test_picker_url_is_narrow_and_pkce_protected():
    req=consent()
    url=urlsplit(req.url)
    assert url.scheme=="https" and url.hostname=="accounts.google.com"
    q={key:values[0] for key,values in parse_qs(url.query).items()}
    assert q["scope"]==oauth.DRIVE_FILE_SCOPE
    assert q["response_type"]=="code"
    assert q["redirect_uri"]==REDIRECT
    assert q["access_type"]=="offline" and q["prompt"]=="consent select_account"
    assert q["trigger_onepick"]=="true"
    assert q["allow_folder_selection"]=="true"
    assert q["allow_multiple"]=="true"
    assert q["file_ids"]==f"{SOURCES},{TRANSLATIONS}"
    assert q["mimetypes"]=="application/vnd.google-apps.folder"
    assert q["code_challenge_method"]=="S256"
    expected=base64.urlsafe_b64encode(hashlib.sha256(req.verifier.encode()).digest()).decode().rstrip("=")
    assert q["code_challenge"]==expected
    assert "refresh_token" not in req.url
    assert oauth.verify_callback(make_callback(),state=req.state,
        source_folder_id=SOURCES,translation_folder_id=TRANSLATIONS)=="consent-code"


@pytest.mark.parametrize("callback,error",[
    (make_callback(state="stolen"),"state mismatch"),
    (make_callback(picked=SOURCES),"Both existing"),
    (make_callback(picked=f"{SOURCES},{SOURCES}"),"Both existing"),
    (make_callback(picked=f"{SOURCES},{TRANSLATIONS},extra"),"Both existing"),
    ("/oauth2callback?state=safe-oauth-state&code=x","Incomplete"),
    ("/oauth2callback?state=safe-oauth-state&error=access_denied","cancelled"),
    ("/untrusted?state=safe-oauth-state","Unexpected"),
    (make_callback()+"&code=forged","ambiguous"),
])
def test_oauth_callback_rejects_wrong_state_permissions_and_replays(callback,error):
    with pytest.raises(oauth.DriveAuthorizationError,match=error):
        oauth.verify_callback(
            callback,state="safe-oauth-state",
            source_folder_id=SOURCES,translation_folder_id=TRANSLATIONS,
        )


def test_invalid_destination_or_redirect_rejected():
    with pytest.raises(oauth.DriveAuthorizationError,match="differ"):
        oauth.make_consent_request("client",REDIRECT,SOURCES,SOURCES)
    with pytest.raises(oauth.DriveAuthorizationError,match="loopback"):
        oauth.make_consent_request("client","https://example.com/callback",SOURCES,TRANSLATIONS)
    with pytest.raises(DriveExportError,match="folder or file ID"):
        oauth.make_consent_request("client",REDIRECT,"../escape",TRANSLATIONS)


def test_oauth_code_exchange_cannot_expand_scope_or_use_redirects():
    calls=[]
    def good(request):
        calls.append(request)
        assert request.url.host=="oauth2.googleapis.com"
        assert request.url.path=="/token"
        raw=request.content.decode()
        assert "grant_type=authorization_code" in raw
        assert "code_verifier=" in raw
        return httpx.Response(200,json={
            "refresh_token":"refresh-credential",
            "scope":oauth.DRIVE_FILE_SCOPE,
        })
    with httpx.Client(transport=httpx.MockTransport(good),trust_env=False) as http:
        result=oauth.exchange_code(http,client_id="x",client_secret="y",
            redirect_uri=REDIRECT,code="oauth-code",verifier="v"*60)
        assert result=="refresh-credential"
    assert len(calls)==1
    for code,result in [
        (302,{}),(403,{}),(200,{"scope":"https://www.googleapis.com/auth/drive",
                                 "refresh_token":"secret"}),
        (200,{"scope":oauth.DRIVE_FILE_SCOPE}),
    ]:
        with httpx.Client(transport=httpx.MockTransport(
            lambda _req:httpx.Response(code,json=result,headers={
                "Location":"https://example.com/steal" if code==302 else ""
            })),trust_env=False,follow_redirects=False) as http:
            with pytest.raises(oauth.DriveAuthorizationError):
                oauth.exchange_code(http,client_id="x",client_secret="y",
                    redirect_uri=REDIRECT,code="oauth-code",verifier="v"*60)


def test_private_refresh_token_created_only_once_and_never_logged(tmp_path):
    dest=(tmp_path/"owner.refresh")
    oauth.save_private_token(dest,"refresh-credential")
    assert dest.read_text()=="refresh-credential\n"
    if os.name=="posix":
        assert stat.S_IMODE(dest.stat().st_mode)==0o600
    with pytest.raises(oauth.DriveAuthorizationError,match="without overwrite"):
        oauth.save_private_token(dest,"new-credential")
    assert dest.read_text()=="refresh-credential\n"
    with pytest.raises(oauth.DriveAuthorizationError,match="absolute"):
        oauth.save_private_token(Path("relative-token-file"),"bad")


def test_local_picker_roundtrip_preflights_both_folders_before_storing(monkeypatch,tmp_path,capsys):
    issued=[]
    def fake_google(request):
        issued.append((request.method,str(request.url)))
        if request.url.host=="oauth2.googleapis.com" and request.url.path=="/token":
            raw=request.content.decode()
            if "grant_type=authorization_code" in raw:
                return httpx.Response(200,json={
                    "refresh_token":"refresh-only-for-readera",
                    "scope":oauth.DRIVE_FILE_SCOPE,
                })
            assert "grant_type=refresh_token" in raw
            return httpx.Response(200,json={"access_token":"fake-access-token"})
        if request.url.host=="www.googleapis.com" and request.method=="GET":
            assert request.headers.get("authorization")=="Bearer fake-access-token"
            folder=request.url.path.rsplit("/",1)[-1]
            assert folder in (SOURCES,TRANSLATIONS)
            return httpx.Response(200,json={
                "id":folder,"name":"folder","mimeType":"application/vnd.google-apps.folder",
                "trashed":False,"capabilities":{"canAddChildren":True},
            })
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    browser_threads=[]
    def fake_browser(url):
        parts=urlsplit(url)
        q={k:v[0] for k,v in parse_qs(parts.query).items()}
        callback=q["redirect_uri"]+"?"+urlencode({
            "state":q["state"],"code":"approved-code",
            "picked_file_ids":f"{SOURCES},{TRANSLATIONS}",
        })
        def hit():
            with urlopen(callback,timeout=5) as response:
                assert response.status==200
        thread=threading.Thread(target=hit,daemon=True)
        browser_threads.append(thread)
        thread.start()
        return True
    monkeypatch.setattr(oauth.webbrowser,"open",fake_browser)
    path=tmp_path/"owner-private-token"
    with httpx.Client(transport=httpx.MockTransport(fake_google),
                      trust_env=False,follow_redirects=False) as http:
        receipt=oauth.authorize_owner(
            client_id="app.apps.googleusercontent.com",client_secret="desktop-client-secret",
            source_folder_id=SOURCES,translation_folder_id=TRANSLATIONS,
            token_file=path,client=http,
        )
    for thread in browser_threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert receipt["authorized"] and receipt["token_printed"] is False
    assert receipt["credential_path"]==str(path)
    assert path.read_text().strip()=="refresh-only-for-readera"
    printed=capsys.readouterr().out
    assert "refresh-only-for-readera" not in printed
    assert "fake-access-token" not in printed
    assert sum("drive/v3/files/" in url for _,url in issued)==2
    assert sum("/token" in url for _,url in issued)==2


def test_desktop_client_json_and_windows_acl_fail_closed(tmp_path,monkeypatch):
    path=tmp_path/"desktop.json"
    payload={"installed":{"client_id":"app-1.apps.googleusercontent.com",
                          "client_secret":"local-oauth-client-secret"}}
    path.write_text(json.dumps(payload),encoding="utf-8")
    assert oauth.load_desktop_oauth_client(path)==(
        "app-1.apps.googleusercontent.com","local-oauth-client-secret",
    )
    path.write_text(json.dumps({"web":payload["installed"]}))
    with pytest.raises(oauth.DriveAuthorizationError,match="Desktop"):
        oauth.load_desktop_oauth_client(path)
    path.write_text("{"+"z"*65536+"}")
    with pytest.raises(oauth.DriveAuthorizationError,match="too large"):
        oauth.load_desktop_oauth_client(path)

    calls=[]
    def fake_run(argv, **kwargs):
        calls.append((argv,kwargs))
        if argv[0]=="whoami.exe":
            return subprocess.CompletedProcess(argv,0,
                stdout='"DESKTOP\\reader","S-1-5-21-100-200-300-1000"\r\n')
        return subprocess.CompletedProcess(argv,0,stdout="Successfully processed 1 files")
    monkeypatch.setattr(oauth.subprocess,"run",fake_run)
    oauth._restrict_windows_owner_acl(tmp_path/"new-token")
    assert len(calls)==2
    assert calls[0][0][:2]==["whoami.exe","/user"]
    assert calls[1][0][-1]=="*S-1-5-21-100-200-300-1000:F"
    assert calls[1][0][2]=="/inheritance:r"
    assert calls[1][0][3]=="/grant:r"
    assert all(kw.get("check") is True and kw.get("timeout")==10 for _,kw in calls)


def test_failed_windows_acl_never_creates_secret(tmp_path,monkeypatch):
    # Simulate NT path handling on Linux while exercising actual token writer.
    monkeypatch.setattr(oauth,"_on_windows",lambda:True)
    def blocked(argv, **kwargs):
        raise subprocess.CalledProcessError(5,argv)
    monkeypatch.setattr(oauth.subprocess,"run",blocked)
    secret=tmp_path/"owner-credential"
    with pytest.raises(oauth.DriveAuthorizationError,match="file permissions"):
        oauth.save_private_token(secret,"never-store-this-secret")
    assert not secret.exists()


def test_cli_imports_downloaded_desktop_oauth_json_without_env_secret(tmp_path,monkeypatch,capsys):
    from chaptera_server import cli
    import sys
    json_path=tmp_path/"desktop-oauth-client.json"
    json_path.write_text(json.dumps({
        "installed":{
            "client_id":"operator.apps.googleusercontent.com",
            "client_secret":"local-client-json-secret",
        }
    }),encoding="utf-8")
    token_file=tmp_path/"private-token.txt"
    monkeypatch.setenv("CHAPTERA_DRIVE_SOURCE_FOLDER_ID",SOURCES)
    monkeypatch.setenv("CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID",TRANSLATIONS)
    monkeypatch.delenv("CHAPTERA_DRIVE_CLIENT_ID",raising=False)
    monkeypatch.delenv("CHAPTERA_DRIVE_CLIENT_SECRET",raising=False)
    passed=[]
    def fake_auth(**kw):
        passed.append(kw)
        return {"authorized":True,"token_printed":False}
    monkeypatch.setattr(oauth,"authorize_owner",fake_auth)
    monkeypatch.setattr(sys,"argv",[
        "chaptera-server","drive-authorize","--client-json",str(json_path),
        "--token-file",str(token_file),"--no-browser",
    ])
    cli.main()
    output=json.loads(capsys.readouterr().out)
    assert output["authorized"] is True
    assert passed[0]["client_id"]=="operator.apps.googleusercontent.com"
    assert passed[0]["client_secret"]=="local-client-json-secret"
    assert passed[0]["source_folder_id"]==SOURCES
    assert passed[0]["translation_folder_id"]==TRANSLATIONS
    assert passed[0]["open_browser"] is False
    assert not token_file.exists()



def test_private_oauth_refresh_token_read_refuses_weak_permissions(tmp_path):
    from chaptera_server.drive_oauth_setup import (
        read_private_refresh_token, save_private_token, DriveAuthorizationError,
    )
    dest=tmp_path/"owner-refresh"
    save_private_token(dest,"1//synthetic-token-for-test")
    assert read_private_refresh_token(dest)=="1//synthetic-token-for-test"
    if os.name=="posix":
        os.chmod(dest,0o644)
        with pytest.raises(DriveAuthorizationError,match="owner-only"):
            read_private_refresh_token(dest)
        os.chmod(dest,0o600)
        linked=tmp_path/"linked-owner-refresh"
        linked.symlink_to(dest)
        with pytest.raises(DriveAuthorizationError,match="not found"):
            read_private_refresh_token(linked)
    with pytest.raises(DriveAuthorizationError,match="not found"):
        read_private_refresh_token(Path("relative-owner-refresh"))


def test_drive_smoke_cli_uses_existing_private_token_only(tmp_path,monkeypatch,capsys):
    from chaptera_server import cli, drive_acceptance, drive_export
    import sys
    json_path=tmp_path/"desktop-client.json"
    json_path.write_text(json.dumps({"installed":{
        "client_id":"smoke.apps.googleusercontent.com",
        "client_secret":"locally-provisioned-secret",
    }}),encoding="utf-8")
    token_file=tmp_path/"owner-token"
    oauth.save_private_token(token_file,"1//local-secret-token")
    monkeypatch.setenv("CHAPTERA_DRIVE_SOURCE_FOLDER_ID",SOURCES)
    monkeypatch.setenv("CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID",TRANSLATIONS)
    monkeypatch.setenv("CHAPTERA_DRIVE_ROOT_FOLDER_ID","syntheticReadEraLibraryRoot0001")
    seen=[]
    def fake_acceptance(drive,*,source_folder_id,translation_folder_id,root_folder_id,live):
        seen.append((source_folder_id,translation_folder_id,root_folder_id,live))
        return {"stage":"live_two_folder_export_verified","file_ids_unchanged":True}
    monkeypatch.setattr(drive_acceptance,"run_drive_acceptance",fake_acceptance)
    class FakeClient:
        def __init__(self,**kwargs):
            assert kwargs["refresh_token"]=="1//local-secret-token"
            assert kwargs["client_id"]=="smoke.apps.googleusercontent.com"
    monkeypatch.setattr(drive_export,"GoogleDriveClient",FakeClient)
    monkeypatch.setattr(sys,"argv",[
        "chaptera-server","drive-smoke","--client-json",str(json_path),
        "--token-file",str(token_file),"--live-smoke",
    ])
    cli.main()
    result=json.loads(capsys.readouterr().out)
    assert result["file_ids_unchanged"] is True
    assert seen==[(SOURCES,TRANSLATIONS,"syntheticReadEraLibraryRoot0001",True)]



def test_drive_smoke_server_uses_protected_credentials_without_local_token(tmp_path,monkeypatch,capsys):
    from chaptera_server import cli, drive_acceptance, drive_export
    import sys
    creds={
        "google_drive_client_id":"server.apps.googleusercontent.com",
        "google_drive_client_secret":"synthetic-server-client-secret",
        "google_drive_refresh_token":"1//synthetic-server-refresh-token",
    }
    monkeypatch.setattr(cli,"_read_systemd_credential",
        lambda credential,env,default: creds.get(credential,default))
    monkeypatch.setenv("CHAPTERA_DRIVE_SOURCE_FOLDER_ID",SOURCES)
    monkeypatch.setenv("CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID",TRANSLATIONS)
    monkeypatch.delenv("CHAPTERA_DRIVE_ROOT_FOLDER_ID",raising=False)
    called=[]
    def fake_acceptance(drive,*,source_folder_id,translation_folder_id,root_folder_id,live):
        called.append((source_folder_id,translation_folder_id,root_folder_id,live))
        return {"stage":"read_only_preflight","drive_mutations":0}
    monkeypatch.setattr(drive_acceptance,"run_drive_acceptance",fake_acceptance)
    class FakeClient:
        def __init__(self,**kwargs):
            assert kwargs["client_id"]==creds["google_drive_client_id"]
            assert kwargs["refresh_token"]==creds["google_drive_refresh_token"]
    monkeypatch.setattr(drive_export,"GoogleDriveClient",FakeClient)
    monkeypatch.setattr(sys,"argv",["chaptera-server","drive-smoke"])
    cli.main()
    result=json.loads(capsys.readouterr().out)
    assert result["stage"]=="read_only_preflight"
    assert called==[(SOURCES,TRANSLATIONS,"",False)]

    del creds["google_drive_refresh_token"]
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code==2
    assert "synthetic-server-client-secret" not in capsys.readouterr().err
