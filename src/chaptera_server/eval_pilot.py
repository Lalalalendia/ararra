"""Opt-in, content-redacted fidelity/cost pilot for the two translation protocols.

Default is an OFFLINE deterministic transport probe. Live Gemini requires a
separately supplied file, key, authorization flag and explicit env gate. Reports
never include raw source, prompt, provider response or API credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from chaptera_translation_core import ChapterInput, TranslationCore, TranslationRejected
from chaptera_translation_core.ports import ProviderRequest
from .provider import GeminiProvider


class EvaluationBudgetExceeded(RuntimeError):
    """Stop before an unapproved additional request or oversized prompt."""


# Synthetic short examples only; no book text is bundled in public source.
SYNTHETIC_CHAPTERS = (
    '<body><p>世界。</p><p>兄弟。</p></body>',
    '<div><p><em>こんにちは。</em>世界。</p><p>王雪。</p></div>',
    '<section><p>' + ('世界。' * 150) + '</p><p>ありがとう。</p></section>',
)
_SYNTHETIC_WORDS = {
    'こんにちは': 'Привет', 'ありがとう': 'Спасибо',
    '世界': 'Мир', '兄弟': 'братья', '王雪': 'Ван Сюэ',
}


def _synthetic_translate(text: str) -> str:
    for original, replacement in _SYNTHETIC_WORDS.items():
        text = text.replace(original, replacement)
    return text


class SyntheticProvider:
    """Deterministic mock for STRUCTURAL correctness, not literary quality."""
    async def generate(self, request: ProviderRequest) -> str:
        if request.transport == 'html_v0':
            return _synthetic_translate(request.source_html_with_markers)
        packet = json.loads(request.source_html_with_markers)
        columns = packet['columns']
        rows = [dict(zip(columns, row)) for row in packet['rows']]
        segments: list[dict[str, Any]] = []
        previous_domain = None
        for row in rows:
            domain = row['render_domain_id']
            if domain != previous_domain:
                segments.append({'covers': [], 'translation': ''})
                previous_domain = domain
            segments[-1]['covers'].append(row['atom_id'])
            segments[-1]['translation'] += _synthetic_translate(row['source_text'])
        return json.dumps({'segments': segments}, ensure_ascii=False)


class RequestBudget:
    """Shared budget prevents one protocol from exceeding the paid pilot cap."""
    def __init__(self, *, max_requests: int, max_prompt_chars: int):
        if max_requests < 1 or max_prompt_chars < 1:
            raise ValueError('Positive evaluation budgets are required')
        self.max_requests = max_requests
        self.max_prompt_chars = max_prompt_chars
        self.requests = 0


class InspectedProvider:
    """Measure request/usage metadata without capturing any chapter text."""
    def __init__(self, provider, budget: RequestBudget):
        self.provider = provider
        self.budget = budget
        self.prompt_chars = 0
        self.packet_chars = 0
        self.requests = 0
        self.usage_seen = False
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.thought_tokens = 0
        self.cached_tokens = 0

    async def generate(self, request: ProviderRequest) -> str:
        if len(request.prompt) > self.budget.max_prompt_chars:
            raise EvaluationBudgetExceeded('Prompt exceeds the caller-authorized size limit')
        if self.budget.requests >= self.budget.max_requests:
            raise EvaluationBudgetExceeded('Provider request budget exhausted')
        self.budget.requests += 1
        self.requests += 1
        self.prompt_chars += len(request.prompt)
        self.packet_chars += len(request.source_html_with_markers)
        def usage(counter: dict[str, int]) -> None:
            self.usage_seen = True
            self.prompt_tokens += counter.get('promptTokenCount', 0)
            self.output_tokens += counter.get('candidatesTokenCount', 0)
            self.total_tokens += counter.get('totalTokenCount', 0)
            self.thought_tokens += counter.get('thoughtsTokenCount', 0)
            self.cached_tokens += counter.get('cachedContentTokenCount', 0)
            if request.on_usage is not None:
                request.on_usage(counter)
        return await self.provider.generate(replace(request, on_usage=usage))


async def evaluate_transports(
    source_html: str, *, provider_factory, transports: tuple[str, ...] = ('html_v0', 'atoms_v1'),
    max_requests: int = 32, max_prompt_chars: int = 100_000,
) -> dict[str, Any]:
    """Test structural success and report real usage only if provider supplies it.

    A passing result proves protocol QA, NOT faithfulness, prose quality or
    economic superiority. A production default switch always needs human review.
    """
    budget = RequestBudget(max_requests=max_requests, max_prompt_chars=max_prompt_chars)
    rows = []
    for transport in transports:
        if transport not in {'html_v0', 'atoms_v1'}:
            raise ValueError('Unknown evaluation transport')
        provider = InspectedProvider(provider_factory(), budget)
        events: Counter[str] = Counter()
        status = 'failed'
        failed_stage = None
        unit_count = 0
        provenance_count = 0
        generated_chars = 0
        try:
            core = TranslationCore(provider, on_event=lambda e: events.update([e.stage]))
            result = await core.translate(ChapterInput(
                chapter_id='eval', source_html=source_html, transport=transport,
            ))
            status = 'passed'
            unit_count = result.unit_count
            generated_chars = len(result.translated_html)
            provenance_count = len(result.atom_provenance)
        except TranslationRejected as exc:
            failed_stage = exc.stage
        except EvaluationBudgetExceeded:
            failed_stage = 'evaluation_budget'
        except Exception as exc:
            # Failures must not return an exception containing private provider text.
            failed_stage = type(exc).__name__
        rows.append({
            'transport': transport, 'status': status,
            'failed_stage': failed_stage,
            'provider_requests': provider.requests,
            'prompt_chars_total': provider.prompt_chars,
            'source_packet_chars_total': provider.packet_chars,
            'source_visible_chars': len(BeautifulSoup(source_html, 'html.parser').get_text(' ', strip=True)),
            'unit_count': unit_count,
            'provenance_count': provenance_count,
            'translated_html_chars': generated_chars,
            'repair_round_events': events['atom_repair'],
            'usage_metadata_available': provider.usage_seen,
            'actual_prompt_tokens': provider.prompt_tokens if provider.usage_seen else None,
            'actual_output_tokens': provider.output_tokens if provider.usage_seen else None,
            'actual_total_tokens': provider.total_tokens if provider.usage_seen else None,
            'actual_thought_tokens': provider.thought_tokens if provider.usage_seen else None,
            'actual_cached_prompt_tokens': provider.cached_tokens if provider.usage_seen else None,
        })
    return {
        'schema': 'chaptera-evaluation-v1',
        'source_html_chars': len(source_html),
        'source_visible_chars': len(BeautifulSoup(source_html, 'html.parser').get_text(' ', strip=True)),
        'results': rows,
        'default_switch_recommendation': 'NO_GO_NEEDS_HUMAN_REVIEW_AND_LIVE_TOKEN_SAMPLE',
        'caveat': 'Automated QA verifies structural coverage, not semantic or literary faithfulness.',
    }


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Content-redacted Chaptera translation protocol pilot')
    parser.add_argument('--live', action='store_true', help='Use Gemini (billable; requires additional explicit gates)')
    parser.add_argument('--authorize-billable', action='store_true', help='Confirm API charges are authorized')
    parser.add_argument('--chapter-file', type=Path, help='Locally supplied, authorized chapter HTML; required for live')
    parser.add_argument('--model', default=os.environ.get('CHAPTERA_GEMINI_MODEL', 'gemini-3.8-flash'))
    parser.add_argument('--max-requests', type=int, default=6)
    parser.add_argument('--max-prompt-chars', type=int, default=30_000)
    parser.add_argument('--output', type=Path, help='Write redacted statistics (never text or prompts)')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _argument_parser()
    args = parser.parse_args(argv)
    if args.max_requests < 1 or args.max_requests > 32 or not 1000 <= args.max_prompt_chars <= 100_000:
        parser.error('Evaluation bounds: 1..32 requests and 1000..100000 prompt characters')
    if args.live:
        if (not args.authorize_billable or os.getenv('CHAPTERA_EVAL_ALLOW_PAID') != 'YES'
                or not os.getenv('CHAPTERA_GEMINI_API_KEY') or args.chapter_file is None):
            parser.error('Live API use requires --authorize-billable, CHAPTERA_EVAL_ALLOW_PAID=YES, '
                         'CHAPTERA_GEMINI_API_KEY and --chapter-file')
        source = args.chapter_file.read_text(encoding='utf-8')
        if not source or len(source) > 100_000:
            parser.error('Live chapter source must be 1..100000 characters')
        factory = lambda: GeminiProvider(os.environ['CHAPTERA_GEMINI_API_KEY'], args.model)
        samples = (source,)
        mode = 'authorized_live'
    else:
        if args.chapter_file is not None or args.authorize_billable:
            parser.error('Local chapter files and billable authorization are supported only with --live')
        samples = SYNTHETIC_CHAPTERS
        factory = SyntheticProvider
        mode = 'synthetic_offline'
    async def collect():
        return [await evaluate_transports(src, provider_factory=factory,
                                          max_requests=args.max_requests if args.live else max(args.max_requests, 32),
                                          max_prompt_chars=args.max_prompt_chars if args.live else max(args.max_prompt_chars, 100_000))
                for src in samples]
    rows = asyncio.run(collect())
    report = {
        'mode': mode, 'cases': rows,
        'real_gemini_used': args.live,
        'human_literary_review_performed': False,
        'pricing_usd_estimate': None,
    }
    report_text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        args.output.write_text(report_text + '\n', encoding='utf-8')
    else:
        print(report_text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
