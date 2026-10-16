"""Chaptera Translation Core, reusable from Windows application and server workers.

No Qt/PyQt, filesystem, process-global session, or provider credentials are
required to import the package.
"""
from .ports import (
    ChapterInput, ChapterResult, ProviderRequest, TranslationProvider,
    TranslationEvent, TranslationEventHandler, TranslationRejected, TranslationCancelled,
)
from .service import TranslationCore, build_translation_prompt
from .chapter_context import (
    make_snapshot, glossary_fingerprint, sha256_text, bounded_translation_tail,
    context_is_current,
)

__version__ = "0.2.0"
__all__ = [
    "ChapterInput", "ChapterResult", "ProviderRequest", "TranslationProvider",
    "TranslationEvent", "TranslationEventHandler", "TranslationRejected", "TranslationCancelled",
    "TranslationCore", "build_translation_prompt",
    "make_snapshot", "glossary_fingerprint", "sha256_text",
    "bounded_translation_tail", "context_is_current",
]
