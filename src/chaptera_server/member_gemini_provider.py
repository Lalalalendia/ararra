"""Per-job selection of a moderator's Google project on the one existing worker.

One project has exactly one stored key. Each project keeps its own desktop
RPM/TPM scheduler and (when enabled) SQLite daily quota ledger. No extra
processes, concurrent Gemini requests or automatic cross-member key sharing.
"""
from __future__ import annotations

from .config import Settings
from .personal_gemini_keys import PersonalGeminiKeys
from .provider import GeminiProvider, ProviderFailure
from .quota import DailyQuotaLedger, QuotaControlledProvider
from .scheduled_provider import ScheduledGeminiProvider
from .storage import QueueStore
from .team_credentials import TeamCredentials


class _BoundMemberProvider:
    """Recheck revocation before each preflight/translation/repair API call."""
    def __init__(self, delegate, keys, credentials, settings, member, project, fingerprint):
        self.delegate = delegate
        self.keys = keys
        self.credentials = credentials
        self.settings = settings
        self.member = member
        self.project = project
        self.fingerprint = fingerprint
        self.model = settings.gemini_model

    @property
    def completed_calls(self) -> int:
        return int(getattr(self.delegate, "completed_calls", 0))

    @property
    def sent_calls(self) -> int:
        return int(getattr(self.delegate, "sent_calls", 0))

    async def generate(self, request):
        if (not self.credentials.is_active(self.member, self.settings)
                or not self.keys.is_current(self.member, self.project, self.fingerprint)):
            # No key or project identifier in the exception or logs.
            raise ProviderFailure("Gemini credential revoked or moderator inactive",
                                  status_code=401)
        return await self.delegate.generate(request)


class MemberGeminiProviders:
    """Select one of an actor's own projects per job, rotating across jobs.

    A project-specific provider/scheduler persists across jobs so normal RPM/
    TPM protection is not reset at each chapter boundary.
    """
    def __init__(self, store: QueueStore, settings: Settings,
                 keys: PersonalGeminiKeys | None = None):
        self.store = store
        self.settings = settings
        self.keys = keys or PersonalGeminiKeys(store, settings.gemini_vault_key)
        if not self.keys.configured:
            raise ValueError("Personal Gemini mode requires a protected vault key")
        self.credentials = TeamCredentials(store)
        self._providers: dict[str, tuple[str, ScheduledGeminiProvider]] = {}
        self._rotation: dict[str, int] = {}

    def _provider(self, member: str, project: str, fingerprint: str) -> ScheduledGeminiProvider:
        previous = self._providers.get(project)
        if previous and previous[0] == fingerprint:
            return previous[1]
        # Only the worker resolves plaintext, and never passes it to web UI.
        key = self.keys.resolve(member, project, fingerprint)
        output_tokens = (max(1, int(self.settings.gemini_reserved_output_tokens * 0.98))
                         if self.settings.gemini_reserved_output_tokens else None)
        provider = GeminiProvider(
            key, self.settings.gemini_model,
            max_output_tokens=output_tokens,
            stream_atoms=self.settings.gemini_stream_atoms,
        )
        if self.settings.gemini_requests_per_day:
            ledger = DailyQuotaLedger(
                self.store, project, self.settings.gemini_model,
                self.settings.gemini_requests_per_day,
            )
            provider = QuotaControlledProvider(provider, ledger)
        result = ScheduledGeminiProvider(
            provider, rpm_limit=self.settings.gemini_rpm,
            tpm_limit=self.settings.gemini_tpm,
            reserved_output_tokens=self.settings.gemini_reserved_output_tokens,
        )
        # Replaced credentials do not keep their old scheduler/provider in memory.
        self._providers[project] = (fingerprint, result)
        return result

    def for_job(self, job: dict):
        from .personal_gemini_keys import GeminiKeyMissing
        actor = job["actor_id"]
        if not self.credentials.is_active(actor, self.settings):
            raise GeminiKeyMissing("Moderator account is inactive")
        choices = self.keys.choices(actor)
        if not choices:
            raise GeminiKeyMissing("Moderator has not added a Gemini key")
        position = self._rotation.get(actor, 0) % len(choices)
        self._rotation[actor] = (position + 1) % len(choices)
        project, fingerprint = choices[position]
        delegate = self._provider(actor, project, fingerprint)
        return _BoundMemberProvider(
            delegate, self.keys, self.credentials, self.settings,
            actor, project, fingerprint,
        )
