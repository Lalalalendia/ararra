from __future__ import annotations

import asyncio

import httpx
import pytest

from chaptera_server.provider import GeminiProvider, ProviderFailure, TransientProviderFailure
from chaptera_translation_core.ports import ProviderRequest


def test_gemini_adapter_uses_header_and_extracts_text():
    seen = {}

    def responder(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Привет"}]}}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
            provider = GeminiProvider("secret", "gemini-2.5-flash", client=client)
            text = await provider.generate(ProviderRequest("1", "prompt", "<p>source</p>", 1))
            assert text == "Привет"

    asyncio.run(scenario())
    assert seen["key"] == "secret"
    assert "secret" not in seen["url"]


def test_gemini_respects_retry_after_for_429_without_logging_body():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "17"}, text="sensitive data")
        )) as client:
            with pytest.raises(TransientProviderFailure) as exc:
                await GeminiProvider("secret", "gemini-2.5-flash", client=client).generate(
                    ProviderRequest("1", "prompt", "<p>source</p>", 1)
                )
            assert exc.value.retry_after_seconds == 17.0
            assert "sensitive" not in str(exc.value)

    asyncio.run(scenario())


def test_gemini_rejects_bad_model_and_truncated_answer():
    with pytest.raises(ValueError):
        GeminiProvider("secret", "../../../abc")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]})
        )) as client:
            with pytest.raises(TransientProviderFailure):
                await GeminiProvider("secret", "gemini-2.5-flash", client=client).generate(
                    ProviderRequest("1", "prompt", "<p>source</p>", 1))

    asyncio.run(scenario())


def test_actual_gemini_usage_metadata_can_be_reported_without_logging_prompts():
    usages = []
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'Привет'}]}}],
            'usageMetadata': {'promptTokenCount': 126, 'candidatesTokenCount': 23,
                              'totalTokenCount': 149},
        }))) as client:
            provider = GeminiProvider('secret', 'gemini-2.5-flash', client=client)
            result = await provider.generate(ProviderRequest('1', 'prompt', '{}', 1, 'atoms_v1', usages.append))
            assert result == 'Привет'
    asyncio.run(scenario())
    assert usages == [{'promptTokenCount': 126, 'candidatesTokenCount': 23, 'totalTokenCount': 149}]


def test_extended_gemini_usage_counters_include_thought_and_cached_tokens():
    import asyncio
    import httpx
    from chaptera_server.provider import GeminiProvider
    from chaptera_translation_core.ports import ProviderRequest
    usages = []
    def response(_request):
        return httpx.Response(200, json={
            'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'Мир'}]}}],
            'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 15,
                              'totalTokenCount': 130, 'thoughtsTokenCount': 15,
                              'cachedContentTokenCount': 80},
        })
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            provider = GeminiProvider('fake-test-key', 'gemini-test', client=client)
            return await provider.generate(ProviderRequest('x', 'prompt', '', 0, on_usage=usages.append))
    assert asyncio.run(run()) == 'Мир'
    assert usages == [{'promptTokenCount': 100, 'candidatesTokenCount': 15,
                       'totalTokenCount': 130, 'thoughtsTokenCount': 15,
                       'cachedContentTokenCount': 80}]
