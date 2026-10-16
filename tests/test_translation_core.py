from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

from chaptera_translation_core import ChapterInput, TranslationCancelled, TranslationCore, TranslationRejected


class EchoTranslation:
    def __init__(self):
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return request.source_html_with_markers.replace("世界正在燃烧。", "Мир горит.")


def test_headless_translation_pipeline_preserves_structure():
    provider = EchoTranslation()
    events = []
    result = asyncio.run(TranslationCore(provider, on_event=events.append).translate(
        ChapterInput("chapter-12", "<body><p>世界正在燃烧。</p></body>", glossary={"世界": "мир"})
    ))
    assert result.translated_html == "<body><p>Мир горит.</p></body>"
    assert result.unit_count == 1
    assert len(result.source_hash) == 64
    assert "TSEG_" in provider.requests[0].prompt
    assert [x.stage for x in events] == ["preparing", "provider_request", "validating", "completed"]


def test_missing_provenance_markers_block_incomplete_output():
    class BadProvider:
        async def generate(self, request):
            return "<p>Готово</p>"

    with pytest.raises(TranslationRejected, match="unit_completeness"):
        asyncio.run(TranslationCore(BadProvider()).translate(ChapterInput("c", "<p>世界正在燃烧。</p>")))


def test_cooperative_cancel_prevents_provider_request():
    provider = EchoTranslation()
    with pytest.raises(TranslationCancelled):
        asyncio.run(TranslationCore(provider, is_cancelled=lambda: True).translate(
            ChapterInput("c", "<p>世界正在燃烧。</p>")))
    assert provider.requests == []


def test_no_desktop_dependency_at_import_time():
    core_root = Path(__file__).resolve().parents[1] / "src/chaptera_translation_core"
    for path in core_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "from full_cycle_app" not in source
        assert "import PyQt6" not in source
        assert "from PyQt6" not in source
    assert importlib.import_module("chaptera_translation_core") is not None
    assert "PyQt6" not in sys.modules
