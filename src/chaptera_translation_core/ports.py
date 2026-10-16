"""I/O contracts for GUI, server workers, and provider implementations."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol


@dataclass(frozen=True, slots=True)
class ChapterInput:
    chapter_id: str
    source_html: str
    target_language: str = "Russian"
    source_language: str = "auto"
    glossary: Mapping[str, str | dict[str, Any]] = field(default_factory=dict)
    context: str = ""
    chapter_path: str = ""
    # Server-specific extension: desktop glossary versions use exact chapter paths.
    glossary_versions: Mapping[str, list[dict[str, Any]]] = field(default_factory=dict)
    glossary_authority: str = "READY"
    chapter_context_snapshot: Mapping[str, Any] | None = None
    predecessor: Mapping[str, Any] | None = None
    style_instructions: str = ""


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    chapter_id: str
    prompt: str
    source_html_with_markers: str
    unit_count: int


class TranslationProvider(Protocol):
    """API transport injected by server/desktop. No network or credentials in core."""

    async def generate(self, request: ProviderRequest) -> str:
        ...


@dataclass(frozen=True, slots=True)
class TranslationEvent:
    chapter_id: str
    stage: str
    detail: str = ""


TranslationEventHandler = Callable[[TranslationEvent], None]


@dataclass(frozen=True, slots=True)
class ChapterResult:
    chapter_id: str
    source_hash: str
    translated_html: str
    unit_count: int


class TranslationRejected(ValueError):
    """Translation failed a deterministic QA gate and MUST NOT be persisted."""
    def __init__(self, chapter_id: str, stage: str, detail: str):
        self.chapter_id = chapter_id
        self.stage = stage
        self.detail = detail
        super().__init__(f"{chapter_id}: {stage}: {detail}")


class TranslationCancelled(RuntimeError):
    """Cancellation is cooperative; nothing is persisted by the translation core."""
