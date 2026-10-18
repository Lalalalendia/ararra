"""Private server configuration must never accept implicit proxy addresses."""
from __future__ import annotations

import sys

from chaptera_server import cli


def test_private_server_proxy_header_trust_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAPTERA_DB_PATH", str(tmp_path / "private.sqlite3"))
    monkeypatch.setenv("CHAPTERA_TEAM_TOKENS_JSON",
                       '{"operator":"test-credential"}')
    monkeypatch.setenv("CHAPTERA_ADMIN_MEMBER_ID", "operator")
    monkeypatch.setenv("CHAPTERA_BIND", "127.0.0.1")
    monkeypatch.setenv("CHAPTERA_PORT", "8091")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setattr(sys, "argv", ["chaptera-server", "serve"])
    import uvicorn
    calls = []

    def capture(app, **kwargs):
        assert app is not None
        calls.append(kwargs)

    monkeypatch.setattr(uvicorn, "run", capture)
    cli.main()
    assert len(calls) == 1
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 8091
    assert calls[0]["proxy_headers"] is True
    assert calls[0]["forwarded_allow_ips"] == "127.0.0.1"


def test_nonloopback_bind_does_not_trust_proxy_headers(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAPTERA_DB_PATH", str(tmp_path / "public.sqlite3"))
    monkeypatch.setenv("CHAPTERA_TEAM_TOKENS_JSON",
                       '{"operator":"test-credential"}')
    monkeypatch.setenv("CHAPTERA_ADMIN_MEMBER_ID", "operator")
    monkeypatch.setenv("CHAPTERA_BIND", "0.0.0.0")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setattr(sys, "argv", ["chaptera-server", "serve"])
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    cli.main()
    assert calls[0]["proxy_headers"] is False
    assert calls[0]["forwarded_allow_ips"] == "127.0.0.1"
