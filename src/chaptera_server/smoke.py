"""One-shot Gemini -> TranslationCore -> QueueStore smoke test.

Offline mode runs a real GeminiProvider against an in-process mock HTTP server.
Live mode must be explicitly enabled on the actual worker host. No real chapter,
key, prompt, translated text, or API response is included in the report.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import os
import re
from bs4 import BeautifulSoup
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx

from chaptera_translation_core.ports import ProviderRequest
from .provider import GeminiProvider, ProviderFailure
from .storage import JobSpec, QueueStore
from .worker import TranslationWorker

_SAMPLE = '<p>世界。</p>'


class MeteredProvider:
    def __init__(self, provider: GeminiProvider, max_requests: int):
        self.provider = provider
        self.max_requests = max_requests
        self.requests = 0
        self.responses = 0
        self.usage: dict[str, int] = {}
        self.failure: str | None = None

    async def generate(self, request: ProviderRequest) -> str:
        if self.requests >= self.max_requests:
            self.failure = 'smoke_request_budget'
            raise ProviderFailure('Smoke request budget exhausted')
        if len(request.prompt) > 30000:
            self.failure = 'smoke_prompt_budget'
            raise ProviderFailure('Smoke prompt budget exceeded')
        self.requests += 1

        def record_usage(counters: dict[str, int]) -> None:
            for name, value in counters.items():
                self.usage[name] = self.usage.get(name, 0) + value
            if request.on_usage is not None:
                request.on_usage(counters)

        try:
            response = await self.provider.generate(replace(request, on_usage=record_usage))
            self.responses += 1
            return response
        except ProviderFailure as exc:
            # ProviderFailure deliberately contains only HTTP status and safe state.
            self.failure = str(exc)
            raise


def _mock_gemini(request: httpx.Request) -> httpx.Response:
    """Exercise GeminiProvider's actual HTTP envelope without network access."""
    if request.method != 'POST' or request.url.host != 'generativelanguage.googleapis.com':
        return httpx.Response(400, json={'error': 'unexpected request'})
    prompt = json.loads(request.content)['contents'][0]['parts'][0]['text']
    if 'SOURCE ATOMS JSON:\n' in prompt:
        packet = json.loads(prompt.rsplit('SOURCE ATOMS JSON:\n', 1)[1])
        items = [dict(zip(packet['columns'], row)) for row in packet['rows']]
        segments: list[dict[str, Any]] = []
        for atom in items:
            if not segments or atom['render_domain_id'] != segments[-1]['_domain']:
                segments.append({'_domain': atom['render_domain_id'], 'covers': [], 'translation': ''})
            segments[-1]['covers'].append(atom['atom_id'])
            segments[-1]['translation'] += atom['source_text'].replace('世界。', 'Мир.')
        for segment in segments:
            segment.pop('_domain')
        answer = json.dumps({'segments': segments}, ensure_ascii=False)
    else:
        answer = prompt.rsplit('SOURCE HTML:\n', 1)[1].replace('世界。', 'Мир.')
    return httpx.Response(200, json={
        'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': answer}]}}],
    })


async def run_smoke(*, live: bool = False, transport: str = 'html_v0',
                    max_requests: int = 1, model: str = 'gemini-3.8-flash',
                    api_key: str = '') -> dict[str, Any]:
    if transport not in {'html_v0', 'atoms_v1'} or not 1 <= max_requests <= 3:
        raise ValueError('Invalid transport or request budget')
    if live and not api_key:
        raise ValueError('Live smoke requires a Gemini API key')
    async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_gemini)) as mock_client:
        provider = GeminiProvider(api_key if live else 'offline-mock', model,
                                  client=None if live else mock_client, timeout_seconds=45)
        metered = MeteredProvider(provider, max_requests)
        with TemporaryDirectory(prefix='chaptera-smoke-') as folder:
            store = QueueStore(Path(folder) / 'smoke.sqlite3')
            job = store.create_job('smoke', JobSpec(
                chapter_id='smoke', source_html=_SAMPLE, transport=transport,
                max_attempts=1,
            ))
            ran = await TranslationWorker(store, metered).run_once()
            saved = store.get_job(job['id'])
            assert ran and saved is not None
            events = [e['stage'] for e in store.events(job['id'])]
            completed = saved['status'] == 'completed' and bool(saved['translated_html'])
            visible = (BeautifulSoup(saved['translated_html'], 'html.parser').get_text(' ', strip=True)
                       if completed else '')
            language_signal = bool(re.search(r'[А-Яа-яЁё]', visible)) and '世界' not in visible
            success = completed and language_signal
            result = {
                'schema': 'chaptera-gemini-smoke-v1',
                'mode': 'live_gemini' if live else 'offline_mock',
                'transport': transport,
                'model': model,
                'provider_requests': metered.requests,
                'provider_successful_responses': metered.responses,
                'job_status': saved['status'],
                'structural_qa_passed': completed,
                'language_probe_passed': language_signal,
                'qa_passed': success,
                'result_persisted': completed,
                'unit_count': saved['unit_count'] if completed else None,
                'atom_provenance_count': len(saved['atom_provenance']) if completed else 0,
                'stage_names': events,
                'failure_category': metered.failure or (
                    'translation_qa_or_worker' if not completed else
                    ('untranslated_or_wrong_language' if not language_signal else None)
                ),
                'provider_reported_tokens': metered.usage if live and metered.usage else None,
            }
            return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Bounded Gemini translation smoke test on this host')
    parser.add_argument('--live', action='store_true', help='Use real Gemini API (may incur charges)')
    parser.add_argument('--authorize-billable', action='store_true')
    parser.add_argument('--transport', choices=['html_v0', 'atoms_v1'], default='html_v0')
    parser.add_argument('--model', default=os.environ.get('CHAPTERA_GEMINI_MODEL', 'gemini-3.8-flash'))
    parser.add_argument('--max-requests', type=int, default=1)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.max_requests <= 3:
        parser.error('One to three provider requests allowed')
    key = os.getenv('CHAPTERA_GEMINI_API_KEY', '') if args.live else ''
    if args.live and (not args.authorize_billable or os.getenv('CHAPTERA_SMOKE_ALLOW_LIVE') != 'YES' or not key):
        parser.error('Live smoke requires --authorize-billable, CHAPTERA_SMOKE_ALLOW_LIVE=YES '
                     'and CHAPTERA_GEMINI_API_KEY in the worker environment')
    report = asyncio.run(run_smoke(live=args.live, transport=args.transport,
                                   max_requests=args.max_requests, model=args.model, api_key=key))
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        args.output.write_text(encoded + '\n', encoding='utf-8')
    else:
        print(encoded)
    return 0 if report['qa_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
