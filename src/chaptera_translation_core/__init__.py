"""Chaptera Translation Core, reusable from Windows application and server workers.

No Qt/PyQt, filesystem, process-global session, or provider credentials are
required to import the package.
"""
from .ports import (
    ChapterInput, ChapterResult, ProviderRequest, TranslationProvider,
    TranslationEvent, TranslationEventHandler, TranslationRejected, TranslationCancelled,
)
from .service import TranslationCore, build_translation_prompt

__version__ = "0.1.0"
__all__ = [
    "ChapterInput", "ChapterResult", "ProviderRequest", "TranslationProvider",
    "TranslationEvent", "TranslationEventHandler", "TranslationRejected", "TranslationCancelled",
    "TranslationCore", "build_translation_prompt",
]
