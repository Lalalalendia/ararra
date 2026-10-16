"""Explicit runtime settings; no secret fallbacks or auto-registered accounts."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    team_tokens: dict[str, str] = field(default_factory=dict)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    worker_poll_seconds: float = 1.0
    bind: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        raw = os.environ.get("CHAPTERA_TEAM_TOKENS_JSON", "{}")
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
            gemini_api_key=os.environ.get("CHAPTERA_GEMINI_API_KEY", "").strip(),
            gemini_model=os.environ.get("CHAPTERA_GEMINI_MODEL", "gemini-2.5-flash").strip(),
            worker_poll_seconds=max(0.1, float(os.environ.get("CHAPTERA_WORKER_POLL_SECONDS", "1.0"))),
            bind=os.environ.get("CHAPTERA_BIND", "127.0.0.1"),
            port=int(os.environ.get("CHAPTERA_PORT", "8000")),
        )
