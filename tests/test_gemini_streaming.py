"""Offline Gemini SSE transport and source-proven partial recovery tests."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from chaptera_server.provider import GeminiProvider, ProviderFailure, TransientProviderFailure
from chaptera_translation_core import ChapterInput, TranslationCore
from chaptera_translation_core.api.errors import PartialGenerationError
from chaptera_translation_core.ports import ProviderRequest


def frame(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def answer(text: str, *, reason: str | None = None, thought: bool = False) -> dict:
    candidate = {"content": {"parts": [{"text": text, **({"thought": True} if thought else {})}]}}
    if reason is not None:
        candidate["finishReason"] = reason
    return {"candidates": [candidate]}


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, interrupt=False):
        self.chunks = chunks
        self.interrupt = interrupt

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.interrupt:
            raise httpx.ReadError("simulated physical connection reset")

    async def aclose(self):
        pass


def test_opt_in_gemini_sse_accepts_only_completed_stop_and_ignores_thoughts():
    seen = {}
    usage = []
    def handler(req):
        seen["url"] = str(req.url)
        seen["method"] = req.method
        seen["header"] = req.headers.get("x-goog-api-key")
        wire = frame(answer("analysis thoughts", thought=True)) + frame(answer("Мир"))
        end = frame({"candidates": [{
            "finishReason": "STOP", "content": {"parts": [{"text": " горит"}]}}],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 15}})
        return httpx.Response(200, stream=Chunks([wire[:7], wire[7:55], wire[55:], end]))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = GeminiProvider("fake-secret", "gemini-test", client=client,
                                      stream_atoms=True)
            return await provider.generate(ProviderRequest(
                "1", "source prompt", "payload", 1, "atoms_v1", usage.append))
    assert asyncio.run(run()) == "Мир горит"
    assert seen["method"] == "POST"
    assert ":streamGenerateContent" in seen["url"]
    assert "alt=sse" in seen["url"]
    assert seen["header"] == "fake-secret"
    assert usage == [{"promptTokenCount": 120, "candidatesTokenCount": 15}]


def test_sse_transport_break_preserves_partial_answer_using_desktop_error():
    captured = '{"segments":[{"covers":["atom-id"],"translation":"Мир"},'
    def handler(_req):
        return httpx.Response(200, stream=Chunks(
            [frame(answer(captured))], interrupt=True,
        ))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = GeminiProvider("key", "model", client=client, stream_atoms=True)
            with pytest.raises(PartialGenerationError) as failure:
                await provider.generate(ProviderRequest(
                    "c", "prompt", "{}", 1, "atoms_v1",
                ))
            assert failure.value.partial_text == captured
            assert failure.value.reason == "STREAM_TRUNCATED"
            assert failure.value.is_network_partial
            assert provider.completed_calls == 0
    asyncio.run(run())


def test_sse_network_error_without_text_is_noncountable_transport_error():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, stream=Chunks([], interrupt=True))
        )) as client:
            provider = GeminiProvider("key", "model", client=client, stream_atoms=True)
            with pytest.raises(TransientProviderFailure) as failure:
                await provider.generate(ProviderRequest(
                    "c", "prompt", "{}", 1, "atoms_v1",
                ))
            assert failure.value.is_network_error is True
    asyncio.run(run())


def test_sse_max_tokens_preserves_partial_and_safety_never_salvages():
    async def run(payload):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, stream=Chunks([frame(payload)]))
        )) as client:
            return await GeminiProvider(
                "key", "model", client=client, stream_atoms=True,
            ).generate(ProviderRequest("c", "prompt", "{}", 1, "atoms_v1"))
    with pytest.raises(PartialGenerationError) as partial:
        asyncio.run(run(answer("partial", reason="MAX_TOKENS")))
    assert partial.value.reason == "MAX_TOKENS"
    assert partial.value.partial_text == "partial"

    with pytest.raises(ProviderFailure) as blocked:
        asyncio.run(run(answer("must not be used", reason="SAFETY")))
    assert blocked.value.finish_reason == "SAFETY"
    assert "must not be used" not in str(blocked.value)


def test_sse_is_opt_in_and_html_v0_remains_plain_generate_content():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={
            "candidates": [{"finishReason": "STOP", "content": {
                "parts": [{"text": "<p>Мир</p>"}]}}],
        })
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = GeminiProvider("key", "model", client=client, stream_atoms=True)
            return await provider.generate(ProviderRequest(
                "c", "prompt", "<p>世界</p>", 1, "html_v0"))
    assert asyncio.run(run()) == "<p>Мир</p>"
    assert ":generateContent" in seen[0]
    assert ":streamGenerateContent" not in seen[0]


def test_real_sse_transport_break_repairs_only_unfinished_source_atoms():
    """Full HTTP -> original PartialGenerationError -> CoverageMap -> repair."""
    source = "<body><p>世界。</p><p>兄弟。</p></body>"
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        prompt = payload["contents"][0]["parts"][0]["text"]
        packet = json.loads(prompt.split("SOURCE ATOMS JSON:\n", 1)[1])
        names = {name: index for index, name in enumerate(packet["columns"])}
        rows = [{name: values[i] for name, i in names.items()} for values in packet["rows"]]
        groups = []
        for row in rows:
            if groups and groups[-1][0] == row["render_domain_id"]:
                groups[-1][1].append(row)
            else:
                groups.append((row["render_domain_id"], [row]))
        translated = [
            {"covers": [row["atom_id"] for row in group],
             "translation": "".join(row["source_text"] for row in group)
             .replace("世界", "Мир").replace("兄弟", "братья")}
            for _, group in groups
        ]
        requests.append((prompt, len(rows)))
        if len(requests) == 1:
            assert len(translated) == 2
            interrupted = ('{"segments":[' + json.dumps(translated[0], ensure_ascii=False)
                           + ',{"covers":["unfinished')
            return httpx.Response(200, stream=Chunks(
                [frame(answer(interrupted))], interrupt=True,
            ))
        assert "TARGETED REPAIR" in prompt
        return httpx.Response(200, stream=Chunks([
            frame(answer(json.dumps({"segments": translated}, ensure_ascii=False),
                         reason="STOP")),
        ]))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = GeminiProvider("key", "model", client=client, stream_atoms=True)
            return await TranslationCore(provider).translate(ChapterInput(
                "chapter", source, transport="atoms_v1",
            ))
    result = asyncio.run(run())
    assert result.translated_html == "<body><p>Мир。</p><p>братья。</p></body>"
    assert len(requests) == 2
    assert [r[1] for r in requests] == [2, 1]
