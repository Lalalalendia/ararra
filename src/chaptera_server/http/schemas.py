"""Stable request validation contracts for the v1 HTTP API."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field

GlossaryAuthority = Literal[
    "READY", "READY_WITH_WARNINGS", "EMPTY_EXPECTED", "UNAVAILABLE", "DISABLED"
]
Transport = Literal["html_v0", "atoms_v1"]


class CreateBook(BaseModel):
    book_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)


class SaveChapter(BaseModel):
    chapter_index: int = Field(ge=1)
    chapter_path: str = Field(min_length=1, max_length=1024)
    source_html: str = Field(min_length=1, max_length=2_000_000)
    expected_revision: int = Field(ge=0)


class SaveGlossary(BaseModel):
    glossary: dict[str, str | dict[str, Any]] = Field(default_factory=dict)
    glossary_versions: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    glossary_authority: GlossaryAuthority = "READY"
    expected_revision: int = Field(ge=0)


class AcceptTranslation(BaseModel):
    translated_html: str = Field(min_length=1, max_length=2_000_000)
    expected_revision: int = Field(ge=0)
    expected_source_revision: int = Field(ge=1)


class TranslateBookChapter(BaseModel):
    source_language: str = Field(default="auto", max_length=64)
    target_language: str = Field(default="Russian", min_length=1, max_length=64)
    style_instructions: str = Field(default="", max_length=10_000)
    max_attempts: int = Field(default=3, ge=1, le=10)
    transport: Transport = "html_v0"


class SubmitChapter(BaseModel):
    chapter_id: str = Field(min_length=1, max_length=255)
    source_html: str = Field(min_length=1, max_length=2_000_000)
    source_language: str = Field(default="auto", max_length=64)
    target_language: str = Field(default="Russian", min_length=1, max_length=64)
    glossary: dict[str, str | dict[str, Any]] = Field(default_factory=dict)
    context: str = Field(default="", max_length=100_000)
    chapter_path: str = Field(default="", max_length=1024)
    max_attempts: int = Field(default=3, ge=1, le=10)
    glossary_versions: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    glossary_authority: GlossaryAuthority = "READY"
    chapter_context_snapshot: dict[str, Any] | None = None
    predecessor: dict[str, Any] | None = None
    style_instructions: str = Field(default="", max_length=10_000)
    transport: Transport = "html_v0"
