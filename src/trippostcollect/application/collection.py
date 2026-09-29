"""父侧采集行为环境装配。"""

from __future__ import annotations

import json
from pathlib import Path

from trippostcollect.core.paths import PROJECT_ROOT as ROOT
from trippostcollect.runtime.browser_runtime import browser_runtime_args


def behavior_environment(evidence_path: Path, profile_name: str) -> dict[str, str]:
    return {
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_ENABLED": "1",
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE": profile_name,
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE": str(evidence_path),
        "TRIPPOSTCOLLECT_PROJECT_SCRIPTS": str(ROOT / "scripts"),
        "TRIPPOSTCOLLECT_BROWSER_ARGS_JSON": json.dumps(browser_runtime_args()),
    }
