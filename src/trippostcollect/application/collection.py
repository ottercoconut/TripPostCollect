"""父侧采集行为环境与正式字段校验装配。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trippostcollect.records import formal as _formal
from trippostcollect.artifacts.image_candidates import content_image_candidates

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


def validate_formal_record(
    platform_key: str,
    record: dict[str, Any],
    seen: set[str],
    *,
    allow_xhs_title_image_only: bool = False,
) -> dict[str, Any]:
    return _formal.validate_formal_record(
        platform_key, record, seen, allow_xhs_title_image_only=allow_xhs_title_image_only,
        content_image_candidates=content_image_candidates,
    )
