"""Stable request validation contracts for the v1 HTTP API."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict

GlossaryAuthority = Literal[
    "READY", "READY_WITH_WARNINGS", "EMPTY_EXPECTED", "UNAVAILABLE", "DISABLED"
]
Transport = Literal["html_v0", "atoms_v1"]


class CreateBook(BaseModel):
    book_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    owner_member_id: str | None = Field(default=None, max_length=128)


class ReassignBookOwner(BaseModel):
    owner_member_id: str = Field(min_length=1, max_length=128)
    expected_owner_revision: int = Field(ge=0)


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


class SaveEditorDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body_text: str = Field(max_length=1_000_000)
    expected_revision: int = Field(ge=0)
    expected_source_revision: int = Field(ge=1)
    expected_accepted_revision: int = Field(ge=0)
    rebase: bool = False


class CreateEditorNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body_text: str = Field(min_length=1, max_length=4000)
    expected_source_revision: int = Field(ge=1)
    expected_draft_revision: int = Field(ge=0)
    expected_accepted_revision: int = Field(ge=0)


class SetEditorNoteStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["open", "resolved"]
    expected_revision: int = Field(ge=1)


class RunChapterQa(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_source_revision: int = Field(ge=1)
    expected_accepted_revision: int = Field(ge=1)
    expected_glossary_revision: int = Field(ge=0)


class ReviewChapterQa(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["open", "reviewed", "waived"]
    expected_review_revision: int = Field(ge=1)
    expected_source_revision: int = Field(ge=1)
    expected_accepted_revision: int = Field(ge=1)
    expected_glossary_revision: int = Field(ge=0)
    note: str = Field(default="", max_length=1000)


class RestoreEditorAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    expected_current_revision: int = Field(ge=0)
    expected_source_revision: int = Field(ge=1)


class TranslateBookChapter(BaseModel):
    source_language: str = Field(default="auto", max_length=64)
    target_language: str = Field(default="Russian", min_length=1, max_length=64)
    style_instructions: str = Field(default="", max_length=10_000)
    max_attempts: int = Field(default=3, ge=1, le=10)
    transport: Transport = "html_v0"


class TranslationBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter_id: str = Field(min_length=1, max_length=255)
    expected_source_revision: int = Field(ge=1)


class PreviewTranslationBatch(TranslateBookChapter):
    model_config = ConfigDict(extra="forbid")
    expected_glossary_revision: int = Field(ge=0)
    chapters: list[TranslationBatchItem] = Field(min_length=1, max_length=100)


class SubmitTranslationBatch(PreviewTranslationBatch):
    expected_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


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

class ImportChapterDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sequence: int = Field(ge=1, le=2000)
    action: Literal["create", "replace", "skip"]
    chapter_id: str | None = Field(default=None, max_length=255)
    chapter_index: int | None = Field(default=None, ge=1)
    chapter_path: str | None = Field(default=None, max_length=1024)
    expected_revision: int | None = Field(default=None, ge=0)


class ConfirmBookImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    book_id: str = Field(min_length=1, max_length=128)
    book_mode: Literal["create", "existing"]
    title: str | None = Field(default=None, max_length=500)
    owner_member_id: str | None = Field(default=None, max_length=128)
    prefix_policy: Literal["reject", "preserve", "discard"] = "reject"
    chapters: list[ImportChapterDecision] = Field(min_length=1, max_length=2000)


class InviteModerator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: str = Field(min_length=1, max_length=64)
