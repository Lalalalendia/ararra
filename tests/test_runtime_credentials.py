"""Protect production credential sources and verified provider choice."""
from __future__ import annotations

import json

import pytest

from chaptera_server.config import Settings


def test_verified_gemini_model_is_runtime_default(tmp_path, monkeypatch):
    for key in ('CHAPTERA_GEMINI_MODEL', 'CHAPTERA_GEMINI_API_KEY',
                'CHAPTERA_TEAM_TOKENS_JSON', 'CREDENTIALS_DIRECTORY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('CHAPTERA_DB_PATH', str(tmp_path / 'jobs.db'))
    settings = Settings.from_env()
    assert settings.gemini_model == 'gemini-3.8-flash'
    assert settings.gemini_api_key == ''
    assert settings.team_tokens == {}
    assert settings.bind == '127.0.0.1'


def test_systemd_credentials_are_read_only_and_never_in_repr(tmp_path, monkeypatch):
    (tmp_path / 'team_tokens_json').write_text(json.dumps({'editor': 'secret-team-credential'}))
    (tmp_path / 'gemini_api_key').write_text('secret-gemini-credential\n')
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    monkeypatch.delenv('CHAPTERA_TEAM_TOKENS_JSON', raising=False)
    monkeypatch.delenv('CHAPTERA_GEMINI_API_KEY', raising=False)
    conf = Settings.from_env()
    assert conf.team_tokens == {'editor': 'secret-team-credential'}
    assert conf.gemini_api_key == 'secret-gemini-credential'
    assert 'secret-gemini-credential' not in repr(conf)
    assert 'secret-team-credential' not in repr(conf)
    assert conf.gemini_model == 'gemini-3.8-flash'


@pytest.mark.parametrize('name,env', [('gemini_api_key', 'CHAPTERA_GEMINI_API_KEY'),
                                     ('team_tokens_json', 'CHAPTERA_TEAM_TOKENS_JSON')])
def test_conflicting_credential_sources_refused(tmp_path, monkeypatch, name, env):
    (tmp_path / name).write_text('old-secret')
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    monkeypatch.setenv(env, 'new-secret')
    with pytest.raises(ValueError, match='Conflicting credential sources') as exc:
        Settings.from_env()
    assert 'old-secret' not in str(exc.value)
    assert 'new-secret' not in str(exc.value)


@pytest.mark.parametrize('name', ['gemini_api_key', 'team_tokens_json'])
def test_empty_systemd_secret_refused(tmp_path, monkeypatch, name):
    (tmp_path / name).write_text(' \n')
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    monkeypatch.delenv('CHAPTERA_GEMINI_API_KEY', raising=False)
    monkeypatch.delenv('CHAPTERA_TEAM_TOKENS_JSON', raising=False)
    with pytest.raises(ValueError, match='Empty systemd credential'):
        Settings.from_env()


def test_no_systemd_credential_still_supports_dev_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    monkeypatch.setenv('CHAPTERA_GEMINI_API_KEY', 'env-secret')
    monkeypatch.setenv('CHAPTERA_GEMINI_MODEL', 'gemini-3.6-flash')
    monkeypatch.setenv('CHAPTERA_TEAM_TOKENS_JSON', '{"test":"token"}')
    conf = Settings.from_env()
    assert conf.gemini_api_key == 'env-secret'
    assert conf.gemini_model == 'gemini-3.6-flash'
    assert conf.team_tokens == {'test': 'token'}
