"""Offline protocol comparison and explicit paid API authorization boundary."""
from __future__ import annotations

import asyncio
import json

import pytest

from chaptera_server.eval_pilot import (
    RequestBudget, InspectedProvider, SyntheticProvider,
    evaluate_transports, main,
)
from chaptera_translation_core import ChapterInput, TranslationCore


def test_synthetic_eval_reports_structural_qa_without_claiming_real_tokens():
    source = '<body><p>世界。</p><p>兄弟。</p></body>'
    report = asyncio.run(evaluate_transports(source, provider_factory=SyntheticProvider))
    assert report['default_switch_recommendation'].startswith('NO_GO')
    assert [row['status'] for row in report['results']] == ['passed', 'passed']
    assert [row['provider_requests'] for row in report['results']] == [1, 1]
    assert report['results'][0]['provenance_count'] == 0
    assert report['results'][1]['provenance_count'] == 2
    assert all(row['usage_metadata_available'] is False for row in report['results'])
    assert all(row['actual_prompt_tokens'] is None for row in report['results'])
    assert all(row['source_visible_chars'] < row['prompt_chars_total'] for row in report['results'])
    assert '世界' not in json.dumps(report, ensure_ascii=False)
    assert 'Мир' not in json.dumps(report, ensure_ascii=False)


def test_budget_is_enforced_before_provider_call():
    class SpyProvider:
        calls = 0
        async def generate(self, request):
            self.calls += 1
            return ''
    spy = SpyProvider()
    budget = RequestBudget(max_requests=1, max_prompt_chars=100)
    provider = InspectedProvider(spy, budget)
    from chaptera_translation_core.ports import ProviderRequest
    from chaptera_server.eval_pilot import EvaluationBudgetExceeded
    async def run():
        with pytest.raises(EvaluationBudgetExceeded, match='size limit'):
            await provider.generate(ProviderRequest('x', 'p' * 101, '', 0))
        assert spy.calls == 0
        await provider.generate(ProviderRequest('x', 'ok', '', 0))
        with pytest.raises(EvaluationBudgetExceeded, match='budget exhausted'):
            await provider.generate(ProviderRequest('x', 'ok', '', 0))
    asyncio.run(run())
    assert spy.calls == 1
    assert budget.requests == 1


def test_usage_reporting_for_both_html_and_atom_protocols():
    class UsageProvider(SyntheticProvider):
        async def generate(self, request):
            if request.on_usage:
                request.on_usage({'promptTokenCount': 31, 'candidatesTokenCount': 9, 'totalTokenCount': 40})
            return await super().generate(request)
    report = asyncio.run(evaluate_transports('<p>世界。</p>', provider_factory=UsageProvider))
    for row in report['results']:
        assert row['status'] == 'passed'
        assert row['usage_metadata_available'] is True
        assert row['actual_prompt_tokens'] == 31
        assert row['actual_output_tokens'] == 9
        assert row['actual_total_tokens'] == 40


def test_cli_offline_writes_redacted_reproducible_report(tmp_path, monkeypatch):
    monkeypatch.delenv('CHAPTERA_GEMINI_API_KEY', raising=False)
    target = tmp_path / 'metrics.json'
    assert main(['--output', str(target)]) == 0
    report = json.loads(target.read_text())
    assert report['mode'] == 'synthetic_offline'
    assert report['real_gemini_used'] is False
    assert len(report['cases']) == 3
    assert all(row['status'] == 'passed' for item in report['cases'] for row in item['results'])
    assert report['pricing_usd_estimate'] is None
    assert 'contents' not in report


def test_live_mode_requires_unambiguous_billable_authorization(tmp_path, monkeypatch):
    source = tmp_path / 'chapter.html'
    source.write_text('<p>世界。</p>', encoding='utf8')
    monkeypatch.delenv('CHAPTERA_EVAL_ALLOW_PAID', raising=False)
    monkeypatch.setenv('CHAPTERA_GEMINI_API_KEY', 'synthetic-dummy')
    with pytest.raises(SystemExit) as err:
        main(['--live', '--chapter-file', str(source)])
    assert err.value.code == 2
    monkeypatch.setenv('CHAPTERA_EVAL_ALLOW_PAID', 'YES')
    with pytest.raises(SystemExit):
        main(['--live', '--chapter-file', str(source)])
    monkeypatch.delenv('CHAPTERA_GEMINI_API_KEY', raising=False)
    with pytest.raises(SystemExit):
        main(['--live', '--authorize-billable', '--chapter-file', str(source)])
    with pytest.raises(SystemExit):
        main(['--chapter-file', str(source)])


def test_request_budget_enforced_across_both_protocols():
    report = asyncio.run(evaluate_transports('<p>世界。</p>', provider_factory=SyntheticProvider, max_requests=1))
    assert report['results'][0]['status'] == 'passed'
    assert report['results'][1]['status'] == 'failed'
    assert report['results'][1]['failed_stage'] == 'evaluation_budget'
