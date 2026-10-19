"""Per-job selection of a moderator's Google project on the one existing worker.

One project has exactly one stored key. Each project keeps its own desktop
RPM/TPM scheduler and (when enabled) SQLite daily quota ledger. No extra
processes, concurrent Gemini requests or automatic cross-member key sharing.
"""
from __future__ import annotations

from .config import Settings
from .personal_gemini_keys import PersonalGeminiKeys, GeminiKeyMissing
from .provider import GeminiProvider, ProviderFailure
from .quota import DailyQuotaLedger, QuotaControlledProvider, read_daily_quota_status
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
            raise GeminiKeyMissing("Gemini credential revoked or moderator inactive")
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
        self._providers: dict[str, tuple[str, int, ScheduledGeminiProvider]] = {}
        self._rotation: dict[str, int] = {}

    def _provider(self, member: str, project: str, fingerprint: str) -> ScheduledGeminiProvider:
        user_cap = self.keys.daily_cap_for(member, project)
        if user_cap <= 0:
            raise GeminiKeyMissing("Moderator has not configured a project daily cap")
        host_cap = self.settings.gemini_requests_per_day
        effective_cap = min(user_cap, host_cap) if host_cap else user_cap
        previous = self._providers.get(project)
        if previous and previous[:2] == (fingerprint, effective_cap):
            return previous[2]
        # Only the worker resolves plaintext, and never passes it to web UI.
        key = self.keys.resolve(member, project, fingerprint)
        output_tokens = (max(1, int(self.settings.gemini_reserved_output_tokens * 0.98))
                         if self.settings.gemini_reserved_output_tokens else None)
        provider = GeminiProvider(
            key, self.settings.gemini_model,
            max_output_tokens=output_tokens,
            stream_atoms=self.settings.gemini_stream_atoms,
        )
        # Every personal-key request (context, chapter, repairs) now uses the
        # existing durable project+model ledger, with a moderator-entered cap.
        # It cannot be raised above a separately configured host ceiling.
        def live_cap(con):
            value = self.keys._budget_in_transaction(con, member, project)
            return min(value, host_cap) if host_cap else value

        ledger = DailyQuotaLedger(
            self.store, project, self.settings.gemini_model,
            effective_cap, limit_reader=live_cap,
        )
        provider = QuotaControlledProvider(provider, ledger)
        # Check revocation again AFTER the desktop scheduler has admitted
        # the call, immediately before spending RPD / sending Gemini HTTP.
        provider = _BoundMemberProvider(
            provider, self.keys, self.credentials, self.settings,
            member, project, fingerprint,
        )
        result = ScheduledGeminiProvider(
            provider, rpm_limit=self.settings.gemini_rpm,
            tpm_limit=self.settings.gemini_tpm,
            reserved_output_tokens=self.settings.gemini_reserved_output_tokens,
        )
        # Replaced credentials do not keep their old scheduler/provider in memory.
        self._providers[project] = (fingerprint, effective_cap, result)
        return result

    def for_job(self, job: dict):
        from .personal_gemini_keys import GeminiKeyMissing
        actor = job["actor_id"]
        if not self.credentials.is_active(actor, self.settings):
            raise GeminiKeyMissing("Moderator account is inactive")
        capped = [(project, fingerprint, cap)
                  for project, fingerprint in self.keys.choices(actor)
                  if (cap := self.keys.daily_cap_for(actor, project)) > 0]
        if not capped:
            raise GeminiKeyMissing("Moderator has no project with an explicit daily cap")
        # Reuse still-available personal allowance before Pacific midnight.
        # This is a read-only choice, never a second scheduler or reservation.
        host_cap = self.settings.gemini_requests_per_day
        available = [
            (project, fingerprint) for project, fingerprint, cap in capped
            if read_daily_quota_status(
                self.store, project, self.settings.gemini_model,
                min(cap, host_cap) if host_cap else cap,
            )["remaining"] > 0
        ]
        choices = available or [(project, fingerprint) for project, fingerprint, _ in capped]
        position = self._rotation.get(actor, 0) % len(choices)
        self._rotation[actor] = (position + 1) % len(choices)
        project, fingerprint = choices[position]
        delegate = self._provider(actor, project, fingerprint)
        return _BoundMemberProvider(
            delegate, self.keys, self.credentials, self.settings,
            actor, project, fingerprint,
        )
