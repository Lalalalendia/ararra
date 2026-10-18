"""Explicit runtime settings; no secret fallbacks or auto-registered accounts."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

 
def _read_systemd_credential(name: str, env_name: str, fallback: str) -> str:
    """Read an explicit systemd credential, without leaking secret contents.

    Traditional environment-based development remains supported. If both are
    configured at once, fail closed instead of silently choosing a secret.
    """
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if directory:
        path = Path(directory) / name
        if path.is_file():
            if os.environ.get(env_name):
                raise ValueError(f"Conflicting credential sources for {env_name}")
            value = path.read_text(encoding="utf-8").strip()
            if not value:
                raise ValueError(f"Empty systemd credential: {name}")
            return value
    return os.environ.get(env_name, fallback)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    team_tokens: dict[str, str] = field(default_factory=dict, repr=False)
    # Exactly two global roles: this one explicit admin and all other token holders
    # (moderators). No implicit admin when left empty.
    admin_member_id: str = ""
    # Browser-only opaque sessions; never disable Secure outside local HTTP tests.
    browser_session_secure: bool = True
    browser_session_ttl_seconds: int = 12 * 60 * 60
    gemini_api_key: str = field(default="", repr=False)
    # Identical server-held AES-256 vault secret for API + worker, never browser.
    gemini_vault_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.8-flash"
    # Opt-in local safety cap. Google quota belongs to PROJECT + MODEL, not key.
    gemini_quota_project: str = ""
    gemini_requests_per_day: int = 0
    # Desktop full_cycle_app default for gemini-3.8-flash. Local admission
    # limits can be adjusted to actual model/project entitlement.
    gemini_rpm: int = 5
    gemini_tpm: int = 250_000
    gemini_reserved_output_tokens: int = 65_536
    # Desktop-compatible streaming transport is opt-in for atom packets only.
    gemini_stream_atoms: bool = False
    worker_poll_seconds: float = 1.0
    # Bounds apply to verified raw crawler text across all historical jobs;
    # protect small VPS disks independently of the per-job 256 MiB cap.
    source_actor_quota_mib: int = 512
    source_global_quota_mib: int = 2048
    bind: str = "127.0.0.1"
    port: int = 8000

    def __post_init__(self) -> None:
        if self.admin_member_id and self.admin_member_id not in self.team_tokens:
            raise ValueError("Configured admin member must exist in team_tokens")
        if not 300 <= self.browser_session_ttl_seconds <= 604_800:
            raise ValueError("Browser session TTL must be between 300 and 604800 seconds")
        if self.gemini_requests_per_day < 0 or self.gemini_requests_per_day > 1_000_000:
            raise ValueError("Gemini daily request limit must be between 0 and 1000000")
        if self.gemini_requests_per_day and not self.gemini_quota_project:
            raise ValueError("CHAPTERA_GEMINI_QUOTA_PROJECT must be set for daily quota")
        if self.gemini_rpm < 0 or self.gemini_tpm < 0 or self.gemini_reserved_output_tokens < 0:
            raise ValueError("Gemini RPM, TPM and output reservation must be non-negative")
        if self.gemini_tpm and not self.gemini_reserved_output_tokens:
            raise ValueError("Gemini TPM requires an explicit output-token reservation")
        if not (1 <= self.source_actor_quota_mib <= 1_048_576):
            raise ValueError("Source actor quota must be 1..1048576 MiB")
        if not (self.source_actor_quota_mib <= self.source_global_quota_mib <= 1_048_576):
            raise ValueError("Source global quota must be >= actor quota and <=1048576 MiB")

    @classmethod
    def from_env(cls) -> "Settings":
        raw = _read_systemd_credential("team_tokens_json", "CHAPTERA_TEAM_TOKENS_JSON", "{}")
        data = json.loads(raw)
        if not isinstance(data, dict) or any(
            not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
            for k, v in data.items()
        ):
            raise ValueError("CHAPTERA_TEAM_TOKENS_JSON must map member IDs to tokens")
        tokens = {k.strip(): v.strip() for k, v in data.items()}
        if len(set(tokens.values())) != len(tokens):
            raise ValueError("Each team member must have a different token")
        return cls(
            db_path=Path(os.environ.get("CHAPTERA_DB_PATH", "./data/jobs.sqlite3")),
            team_tokens=tokens,
            admin_member_id=os.environ.get("CHAPTERA_ADMIN_MEMBER_ID", "").strip(),
            browser_session_secure=os.environ.get("CHAPTERA_BROWSER_SESSION_SECURE", "1").strip().lower()
                                   not in {"0", "false", "no", "off"},
            browser_session_ttl_seconds=int(os.environ.get("CHAPTERA_BROWSER_SESSION_TTL_SECONDS", "43200")),
            gemini_api_key=_read_systemd_credential("gemini_api_key", "CHAPTERA_GEMINI_API_KEY", "").strip(),
            gemini_vault_key=_read_systemd_credential("gemini_vault_key", "CHAPTERA_GEMINI_VAULT_KEY", "").strip(),
            gemini_model=os.environ.get("CHAPTERA_GEMINI_MODEL", "gemini-3.8-flash").strip(),
            gemini_quota_project=os.environ.get("CHAPTERA_GEMINI_QUOTA_PROJECT", "").strip(),
            gemini_requests_per_day=int(os.environ.get("CHAPTERA_GEMINI_REQUESTS_PER_DAY", "0")),
            gemini_rpm=int(os.environ.get("CHAPTERA_GEMINI_RPM", "5")),
            gemini_tpm=int(os.environ.get("CHAPTERA_GEMINI_TPM", "250000")),
            gemini_reserved_output_tokens=int(os.environ.get("CHAPTERA_GEMINI_RESERVED_OUTPUT_TOKENS", "65536")),
            gemini_stream_atoms=os.environ.get("CHAPTERA_GEMINI_STREAM_ATOMS", "0").strip().lower()
                                in {"1", "true", "yes", "on"},
            worker_poll_seconds=max(0.1, float(os.environ.get("CHAPTERA_WORKER_POLL_SECONDS", "1.0"))),
            source_actor_quota_mib=int(os.environ.get("CHAPTERA_SOURCE_ACTOR_QUOTA_MIB", "512")),
            source_global_quota_mib=int(os.environ.get("CHAPTERA_SOURCE_GLOBAL_QUOTA_MIB", "2048")),
            bind=os.environ.get("CHAPTERA_BIND", "127.0.0.1"),
            port=int(os.environ.get("CHAPTERA_PORT", "8000")),
        )
