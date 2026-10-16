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
