"""End-to-end Gemini smoke must not read real credentials in offline mode."""
from __future__ import annotations

import asyncio
import json
import pytest
import httpx

from chaptera_server import smoke


@pytest.mark.parametrize('transport,expected_proofs', [('html_v0', 0), ('atoms_v1', 1)])
def test_offline_mock_drives_rest_adapter_worker_and_persistence(transport, expected_proofs):
    result = asyncio.run(smoke.run_smoke(transport=transport))
    assert result['mode'] == 'offline_mock'
    assert result['provider_requests'] == result['provider_successful_responses'] == 1
    assert result['job_status'] == 'completed'
    assert result['qa_passed'] and result['structural_qa_passed']
    assert result['language_probe_passed'] and result['result_persisted']
    assert result['atom_provenance_count'] == expected_proofs
    assert 'Мир' not in json.dumps(result, ensure_ascii=False)
    assert '世界' not in json.dumps(result, ensure_ascii=False)
    assert result['provider_reported_tokens'] is None


def test_offline_mode_does_not_require_or_use_key(tmp_path, monkeypatch):
    monkeypatch.setenv('CHAPTERA_GEMINI_API_KEY', 'secret-that-must-not-leak')
    output = tmp_path / 'report.json'
    assert smoke.main(['--output', str(output)]) == 0
    data = output.read_text(encoding='utf8')
    assert 'secret-that-must-not-leak' not in data
    assert json.loads(data)['mode'] == 'offline_mock'


def test_live_mode_fails_closed_without_approval(monkeypatch):
    monkeypatch.setenv('CHAPTERA_GEMINI_API_KEY', 'placeholder-key')
    monkeypatch.delenv('CHAPTERA_SMOKE_ALLOW_LIVE', raising=False)
    with pytest.raises(SystemExit) as exc:
        smoke.main(['--live', '--authorize-billable'])
    assert exc.value.code == 2
    monkeypatch.setenv('CHAPTERA_SMOKE_ALLOW_LIVE', 'YES')
    with pytest.raises(SystemExit) as exc:
        smoke.main(['--live'])
    assert exc.value.code == 2
    monkeypatch.delenv('CHAPTERA_GEMINI_API_KEY', raising=False)
    with pytest.raises(SystemExit) as exc:
        smoke.main(['--live', '--authorize-billable'])
    assert exc.value.code == 2


def test_provider_http_failure_never_persists_a_translation(monkeypatch):
    def fake_error(_request):
        return httpx.Response(403, text='private raw provider response')
    monkeypatch.setattr(smoke, '_mock_gemini', fake_error)
    result = asyncio.run(smoke.run_smoke())
    assert result['job_status'] == 'failed'
    assert result['provider_requests'] == 1
    assert result['provider_successful_responses'] == 0
    assert not result['result_persisted']
    assert not result['qa_passed']
    assert 'HTTP 403' in result['failure_category']
    assert 'private raw provider response' not in json.dumps(result)


def test_smoke_detects_untranslated_source_and_does_not_store_it(monkeypatch):
    def fake_untranslated(request):
        prompt = json.loads(request.content)['contents'][0]['parts'][0]['text']
        return httpx.Response(200, json={'candidates': [
            {'finishReason': 'STOP', 'content': {'parts': [
                {'text': prompt.rsplit('SOURCE HTML:\n', 1)[1]}
            ]}}
        ]})
    monkeypatch.setattr(smoke, '_mock_gemini', fake_untranslated)
    result = asyncio.run(smoke.run_smoke())
    assert result['provider_successful_responses'] == 1
    assert not result['structural_qa_passed']
    assert not result['result_persisted']
    assert not result['language_probe_passed']
    assert result['qa_passed'] is False
    assert result['failure_category'] == 'translation_qa_or_worker'


def test_request_cap_validation():
    with pytest.raises(ValueError):
        asyncio.run(smoke.run_smoke(max_requests=4))
    with pytest.raises(ValueError):
        asyncio.run(smoke.run_smoke(transport='unknown'))
