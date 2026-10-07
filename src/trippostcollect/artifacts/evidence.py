"""行为证据的原子文件写出。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_evidence(path: str | Path, evidence: dict[str, Any]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)


# T09：原旧桥 E 的修复报告写出；平台修复编排经注入端口调用。
def _write_xhs_repair_report(report: dict[str, Any]) -> None:
    raw_path = os.environ.get("TRIPPOSTCOLLECT_XHS_REPAIR_REPORT_PATH", "").strip()
    if not raw_path:
        return
    path = Path(raw_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
