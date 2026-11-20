"""正式抓取配置的测试副本。

操作人可以临时停用某个平台的正式 job（如 #220 停用 B站）。行为测试要覆盖全部通用平台，
不应随正式配置里的 ``enabled`` 变化而失败，因此读正式配置后把 job 全部置为启用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FORMAL_CONFIG = ROOT / "config" / "crawl_targets.json"


def formal_config_all_enabled() -> dict[str, Any]:
    config = json.loads(FORMAL_CONFIG.read_text(encoding="utf-8"))
    for job in config["jobs"]:
        job["enabled"] = True
    return config


def write_formal_config_all_enabled(directory: Path) -> Path:
    path = directory / "crawl_targets.json"
    path.write_text(json.dumps(formal_config_all_enabled(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
