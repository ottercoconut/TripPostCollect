"""Validated configuration readers for the independent Xiaohongshu runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trippostcollect.core.paths import XHS_POOL_CONFIG, XHS_TARGET_CONFIG


class XhsConfigError(ValueError):
    pass


def _read_object(path: str | Path) -> tuple[Path, dict[str, Any]]:
    resolved = Path(path).expanduser().resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise XhsConfigError(f"cannot read XHS config {resolved}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise XhsConfigError(f"unsupported XHS config schema: {resolved}")
    return resolved, value


def load_pool_config(path: str | Path = XHS_POOL_CONFIG) -> dict[str, Any]:
    resolved, value = _read_object(path)
    removed_automatic_controls = {
        "max_parallel",
        "global_daily_runs",
        "per_account_daily_runs",
        "minimum_run_interval_seconds",
        "account_cooldown_seconds",
        "challenge_cooldown_seconds",
    }
    stale_fields = sorted(removed_automatic_controls & value.keys())
    if stale_fields:
        raise XhsConfigError(
            f"removed automatic XHS controls remain in {resolved}: {', '.join(stale_fields)}"
        )
    if int(value.get("lease_seconds") or 0) <= 0:
        raise XhsConfigError(f"lease_seconds must be positive in {resolved}")
    profile = str(value.get("behavior_profile") or "")
    if profile != "xhs_guarded":
        raise XhsConfigError("XHS behavior_profile must be xhs_guarded")
    return {"path": str(resolved), **value}


def load_target(target_key: str, path: str | Path = XHS_TARGET_CONFIG) -> dict[str, Any]:
    resolved, value = _read_object(path)
    targets = value.get("targets") or []
    matches = [item for item in targets if isinstance(item, dict) and item.get("target_key") == target_key]
    if len(matches) != 1:
        raise XhsConfigError(f"XHS target {target_key!r} must occur exactly once in {resolved}")
    target = dict(matches[0])
    target_new = int(target.get("target_new_posts") or 0)
    candidates = int(target.get("candidate_hard_limit") or 0)
    stagnant = int(target.get("max_stagnant_batches") or 0)
    if target_new <= 0 or candidates < target_new or stagnant <= 0:
        raise XhsConfigError(f"invalid formal limits for XHS target {target_key}")
    if target.get("required_fields_profile") != "image_post_with_followers_v1":
        raise XhsConfigError("XHS requires image_post_with_followers_v1")
    if target.get("followers_policy") != "required":
        raise XhsConfigError("XHS author followers must be required")
    return {"path": str(resolved), **target}
