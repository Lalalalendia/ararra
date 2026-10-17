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
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.8-flash"
    # Opt-in local safety cap. Google quota belongs to PROJECT + MODEL, not key.
    gemini_quota_project: str = ""
    gemini_requests_per_day: int = 0
    worker_poll_seconds: float = 1.0
    bind: str = "127.0.0.1"
    port: int = 8000

    def __post_init__(self) -> None:
        if self.admin_member_id and self.admin_member_id not in self.team_tokens:
            raise ValueError("Configured admin member must exist in team_tokens")
        if self.gemini_requests_per_day < 0 or self.gemini_requests_per_day > 1_000_000:
            raise ValueError("Gemini daily request limit must be between 0 and 1000000")
        if self.gemini_requests_per_day and not self.gemini_quota_project:
            raise ValueError("CHAPTERA_GEMINI_QUOTA_PROJECT must be set for daily quota")

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
            gemini_api_key=_read_systemd_credential("gemini_api_key", "CHAPTERA_GEMINI_API_KEY", "").strip(),
            gemini_model=os.environ.get("CHAPTERA_GEMINI_MODEL", "gemini-3.8-flash").strip(),
            gemini_quota_project=os.environ.get("CHAPTERA_GEMINI_QUOTA_PROJECT", "").strip(),
            gemini_requests_per_day=int(os.environ.get("CHAPTERA_GEMINI_REQUESTS_PER_DAY", "0")),
            worker_poll_seconds=max(0.1, float(os.environ.get("CHAPTERA_WORKER_POLL_SECONDS", "1.0"))),
            bind=os.environ.get("CHAPTERA_BIND", "127.0.0.1"),
            port=int(os.environ.get("CHAPTERA_PORT", "8000")),
        )
