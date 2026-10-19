"""RE-08: opt-in 3-folder Drive consent; preserve existing working OAuth token."""
from __future__ import annotations

import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import httpx
import pytest

from chaptera_server import cli
from chaptera_server import drive_oauth_setup as oauth
from chaptera_server.drive_export import DriveExportError

SOURCE = "1daqCzEVgPdX639YV5otTLxgVTPbfeBEp"
TRANSLATED = "1X2ZP8Fzbnq5MEAWJyGSErVGGFkfGBrR9"
BACKUP = "1ll0rsERY7kpdMm8rQQVDAFcdx7_gihtl"
REDIRECT = "http://127.0.0.1:47952/oauth2callback"


def _callback(picked: str, *, state: str = "three-folder-state") -> str:
    return "/oauth2callback?" + urlencode({
        "state": state, "code": "mock-consent-code",
        "picked_file_ids": picked,
    })


def test_oauth_requests_exactly_three_known_folders_with_existing_drive_file_scope():
    consent = oauth.make_consent_request(
        "test-desktop.apps.googleusercontent.com",
        REDIRECT, SOURCE, TRANSLATED,
        backup_folder_id=BACKUP,
        verifier="v" * 60, state="three-folder-state",
    )
    params = parse_qs(urlsplit(consent.url).query)
    assert params["file_ids"] == [f"{SOURCE},{TRANSLATED},{BACKUP}"]
    assert params["scope"] == [oauth.DRIVE_FILE_SCOPE]
    assert "https://www.googleapis.com/auth/drive" not in params["scope"]
    assert params["trigger_onepick"] == ["true"]
    assert params["allow_folder_selection"] == ["true"]
    assert params["code_challenge_method"] == ["S256"]
    assert oauth.verify_callback(
        _callback(f"{BACKUP},{SOURCE},{TRANSLATED}"),
        state=consent.state, source_folder_id=SOURCE,
        translation_folder_id=TRANSLATED, backup_folder_id=BACKUP,
    ) == "mock-consent-code"

    # Old workflow still requests and validates exactly its historical 2 IDs.
    old = oauth.make_consent_request(
        "test-desktop.apps.googleusercontent.com",
        REDIRECT, SOURCE, TRANSLATED,
        verifier="v" * 60, state="three-folder-state",
    )
    assert parse_qs(urlsplit(old.url).query)["file_ids"] == [f"{SOURCE},{TRANSLATED}"]
    assert oauth.verify_callback(
        _callback(f"{TRANSLATED},{SOURCE}"),
        state="three-folder-state",
        source_folder_id=SOURCE, translation_folder_id=TRANSLATED,
    ) == "mock-consent-code"


@pytest.mark.parametrize("picked", [
    f"{SOURCE},{TRANSLATED}",
    f"{SOURCE},{TRANSLATED},{SOURCE}",
    f"{SOURCE},{BACKUP},{BACKUP}",
    f"{SOURCE},{TRANSLATED},attackerFolder001",
    f"{SOURCE},{TRANSLATED},{BACKUP},{BACKUP}",
])
def test_three_folder_callback_requires_all_three_distinct_approved_ids(picked):
    with pytest.raises(oauth.DriveAuthorizationError, match="THREE"):
        oauth.verify_callback(
            _callback(picked), state="three-folder-state",
            source_folder_id=SOURCE, translation_folder_id=TRANSLATED,
            backup_folder_id=BACKUP,
        )


def test_backup_folder_must_not_duplicate_either_original():
    for fid in (SOURCE, TRANSLATED):
        with pytest.raises(oauth.DriveAuthorizationError, match="differ"):
            oauth.make_consent_request(
                "test-desktop.apps.googleusercontent.com", REDIRECT,
                SOURCE, TRANSLATED, backup_folder_id=fid,
            )


def _authorize_mock(monkeypatch, *, blocked: str = ""):
    requests = []
    threads = []

    def google(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.host == "oauth2.googleapis.com":
            if b"grant_type=authorization_code" in request.content:
                return httpx.Response(200, json={
                    "refresh_token": "1//new-private-backup-candidate",
                    "scope": oauth.DRIVE_FILE_SCOPE,
                })
            return httpx.Response(200, json={"access_token": "synthetic-access"})
        if request.url.host == "www.googleapis.com" and request.method == "GET":
            assert request.headers["authorization"] == "Bearer synthetic-access"
            fid = request.url.path.rsplit("/", 1)[-1]
            assert fid in (SOURCE, TRANSLATED, BACKUP)
            if fid == blocked:
                return httpx.Response(403, json={"error": "denied"})
            return httpx.Response(200, json={
                "id": fid, "mimeType": "application/vnd.google-apps.folder",
                "trashed": False, "capabilities": {"canAddChildren": True},
            })
        raise AssertionError("Unexpected synthetic Google request")

    def pick(url: str) -> bool:
        params = parse_qs(urlsplit(url).query)
        assert params["file_ids"] == [f"{SOURCE},{TRANSLATED},{BACKUP}"]
        callback = params["redirect_uri"][0] + "?" + urlencode({
            "state": params["state"][0], "code": "synthetic-consent-code",
            "picked_file_ids": f"{SOURCE},{TRANSLATED},{BACKUP}",
        })
        def finish():
            with urlopen(callback, timeout=5) as response:
                assert response.status == 200
        t = threading.Thread(target=finish, daemon=True)
        threads.append(t)
        t.start()
        return True

    monkeypatch.setattr(oauth.webbrowser, "open", pick)
    return httpx.Client(
        transport=httpx.MockTransport(google),
        trust_env=False, follow_redirects=False,
    ), requests, threads


def test_three_folder_owner_consent_keeps_old_token_and_checks_every_folder(
    tmp_path, monkeypatch, capsys,
):
    old = tmp_path / "drive-existing-refresh-token"
    oauth.save_private_token(old, "1//old-original-working-token")
    candidate = tmp_path / "drive-new-backup-candidate"
    http, requests, threads = _authorize_mock(monkeypatch)
    with http:
        receipt = oauth.authorize_owner(
            client_id="test-desktop.apps.googleusercontent.com",
            client_secret="synthetic-secret", client=http,
            source_folder_id=SOURCE, translation_folder_id=TRANSLATED,
            backup_folder_id=BACKUP, token_file=candidate,
        )
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert old.read_text().strip() == "1//old-original-working-token"
    assert candidate.read_text().strip() == "1//new-private-backup-candidate"
    assert receipt["authorized"] is True and receipt["token_printed"] is False
    assert receipt["backup_folder_id"] == BACKUP
    assert [path.rsplit("/",1)[-1] for method, path in requests
            if method == "GET"] == [SOURCE, TRANSLATED, BACKUP]
    printed = capsys.readouterr().out
    assert "1//old-original-working-token" not in printed
    assert "1//new-private-backup-candidate" not in printed


def test_third_folder_403_does_not_publish_new_token_or_modify_old(
    tmp_path, monkeypatch,
):
    old = tmp_path / "old"
    oauth.save_private_token(old, "1//still-working-existing-token")
    new = tmp_path / "backup-candidate"
    http, requests, threads = _authorize_mock(monkeypatch, blocked=BACKUP)
    with http:
        with pytest.raises(oauth.DriveAuthorizationError, match="ALL"):
            oauth.authorize_owner(
                client_id="test-desktop.apps.googleusercontent.com",
                client_secret="synthetic-secret", client=http,
                source_folder_id=SOURCE, translation_folder_id=TRANSLATED,
                backup_folder_id=BACKUP, token_file=new,
            )
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert not new.exists()
    assert old.read_text().strip() == "1//still-working-existing-token"
    assert requests.count(("GET", f"/drive/v3/files/{BACKUP}")) == 1


def test_read_only_cli_probes_backup_and_both_book_folders(
    tmp_path, monkeypatch, capsys,
):
    from chaptera_server import drive_acceptance, drive_export
    import sys

    payload = {"installed": {
        "client_id": "desktop.apps.googleusercontent.com",
        "client_secret": "synthetic-secret",
    }}
    json_file = tmp_path / "desktop.json"
    json_file.write_text(json.dumps(payload), encoding="utf-8")
    token_file = tmp_path / "candidate"
    oauth.save_private_token(token_file, "1//synthetic-private-token")
    monkeypatch.setenv("CHAPTERA_DRIVE_SOURCE_FOLDER_ID", SOURCE)
    monkeypatch.setenv("CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID", TRANSLATED)
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["refresh_token"] == "1//synthetic-private-token"

        def check_destination(self, folder):
            calls.append(("check", folder))

    def acceptance(*_args, **kwargs):
        calls.append(("accept", kwargs.get("live")))
        assert kwargs["live"] is False
        return {"stage": "read_only_preflight", "drive_mutations": 0}

    monkeypatch.setattr(drive_export, "GoogleDriveClient", FakeClient)
    monkeypatch.setattr(drive_acceptance, "run_drive_acceptance", acceptance)
    monkeypatch.setattr(sys, "argv", [
        "chaptera-server", "drive-smoke", "--client-json", str(json_file),
        "--token-file", str(token_file), "--backup-folder-id", BACKUP,
    ])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["backup_folder_access"] == "PASS"
    assert out["backup_folder_id"] == BACKUP
    assert out["drive_mutations"] == 0
    assert calls == [("check", BACKUP), ("accept", False)]

    monkeypatch.setattr(sys, "argv", [
        "chaptera-server", "drive-smoke", "--client-json", str(json_file),
        "--token-file", str(token_file), "--backup-folder-id", BACKUP,
        "--live-smoke",
    ])
    with pytest.raises(SystemExit) as err:
        cli.main()
    assert err.value.code == 2
    assert calls == [("check", BACKUP), ("accept", False)]


def test_cli_adds_backup_to_owner_consent_only_when_requested(tmp_path, monkeypatch, capsys):
    import sys
    client_json = tmp_path / "desktop.json"
    client_json.write_text(json.dumps({"installed":{
        "client_id": "client.apps.googleusercontent.com",
        "client_secret": "synthetic-secret",
    }}), encoding="utf-8")
    token_file = tmp_path / "candidate"
    monkeypatch.setenv("CHAPTERA_DRIVE_SOURCE_FOLDER_ID", SOURCE)
    monkeypatch.setenv("CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID", TRANSLATED)
    called = []

    def fake_authorize(**kwargs):
        called.append(kwargs)
        return {"authorized": True, "token_printed": False}

    monkeypatch.setattr(oauth, "authorize_owner", fake_authorize)
    monkeypatch.setattr(sys, "argv", [
        "chaptera-server", "drive-authorize", "--client-json", str(client_json),
        "--token-file", str(token_file), "--backup-folder-id", BACKUP,
        "--no-browser",
    ])
    cli.main()
    assert json.loads(capsys.readouterr().out)["authorized"] is True
    assert called[0]["backup_folder_id"] == BACKUP
    assert called[0]["open_browser"] is False


def test_windows_script_never_overwrites_existing_token_and_keeps_backup_opt_in():
    script = Path("scripts/connect-google-drive.ps1").read_text(encoding="utf-8")
    assert "drive-owner-refresh-token-backup-candidate.txt" in script
    assert "drive-owner-refresh-token.txt" in script
    assert "$UseOriginalToken" in script
    assert "Select ALL THREE folders" in script
    assert '"--backup-folder-id" $BackupFolder' in script
    assert "No file will be overwritten" in script
