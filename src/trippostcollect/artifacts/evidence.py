"""行为证据的原子文件写出。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_evidence(path: str | Path, evidence: dict[str, Any]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
