"""Admin API settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from trippostcollect.core.paths import DEFAULT_CONFIG, DEFAULT_DB


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AdminSettings:
    db_path: Path
    config_path: Path
    host: str
    port: int
    db_readonly: bool
    allow_commands: bool
    refresh_seconds: int


def load_settings() -> AdminSettings:
    return AdminSettings(
        db_path=Path(os.getenv("TRIPPOST_ADMIN_DB", str(DEFAULT_DB))).expanduser(),
        config_path=Path(os.getenv("TRIPPOST_ADMIN_CONFIG", str(DEFAULT_CONFIG))).expanduser(),
        host=os.getenv("TRIPPOST_ADMIN_HOST", "127.0.0.1"),
        port=int(os.getenv("TRIPPOST_ADMIN_PORT", "8787")),
        db_readonly=_bool_env("TRIPPOST_ADMIN_DB_READONLY", True),
        allow_commands=_bool_env("TRIPPOST_ADMIN_ALLOW_COMMANDS", False),
        refresh_seconds=int(os.getenv("TRIPPOST_ADMIN_REFRESH_SECONDS", "5")),
    )
