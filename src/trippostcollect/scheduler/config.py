"""Scheduler config readers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_scheduler_config(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path).expanduser()
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "path": str(path),
        "defaults": data.get("defaults") or {},
        "jobs": data.get("jobs") or [],
        "job_count": len(data.get("jobs") or []),
    }
