"""Validated configuration readers for the independent Xiaohongshu runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trippostcollect.core.paths import XHS_POOL_CONFIG, XHS_TARGET_CONFIG


class XhsConfigError(ValueError):
    pass


def _read_object(
    path: str | Path,
    *,
    expected_schema: int,
) -> tuple[Path, dict[str, Any]]:
    resolved = Path(path).expanduser().resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise XhsConfigError(f"cannot read XHS config {resolved}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != expected_schema:
        raise XhsConfigError(f"unsupported XHS config schema: {resolved}")
    return resolved, value


def load_pool_config(path: str | Path = XHS_POOL_CONFIG) -> dict[str, Any]:
    resolved, value = _read_object(path, expected_schema=2)
    removed_automatic_controls = {
        "enabled",
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
    if value.get("headed") is not True:
        raise XhsConfigError("XHS formal workflow requires headed=true")
    return {"path": str(resolved), **value}


def load_target(target_key: str, path: str | Path = XHS_TARGET_CONFIG) -> dict[str, Any]:
    resolved, value = _read_object(path, expected_schema=3)
    targets = value.get("targets") or []
    matches = [item for item in targets if isinstance(item, dict) and item.get("target_key") == target_key]
    if len(matches) != 1:
        raise XhsConfigError(f"XHS target {target_key!r} must occur exactly once in {resolved}")
    target = dict(matches[0])
    if "enabled" in target:
        raise XhsConfigError(
            f"removed XHS target enabled gate remains in {resolved}: {target_key}"
        )
    if "download_images" in target:
        raise XhsConfigError(
            f"removed XHS target download_images option remains in {resolved}: {target_key}"
        )
    removed_quantity_fields = {
        "candidate_hard_limit",
        "max_stagnant_batches",
        "target_new_posts",
    }
    stale_fields = sorted(removed_quantity_fields & target.keys())
    if stale_fields:
        raise XhsConfigError(
            f"removed quantity fields remain in XHS target {target_key}: "
            f"{', '.join(stale_fields)}"
        )
    if "timeout_seconds" in target:
        raise XhsConfigError(
            f"removed XHS target timeout_seconds remains in {resolved}: {target_key}"
        )
    if "top_refresh_max_pages" not in target:
        raise XhsConfigError(f"XHS target {target_key} must define top_refresh_max_pages")
    top_refresh = int(target.get("top_refresh_max_pages") or 0)
    if top_refresh < 0:
        raise XhsConfigError(f"top_refresh_max_pages cannot be negative for XHS target {target_key}")
    if target.get("required_fields_profile") != "image_post_with_followers_v1":
        raise XhsConfigError("XHS requires image_post_with_followers_v1")
    if target.get("followers_policy") != "required":
        raise XhsConfigError("XHS author followers must be required")
    return {
        "path": str(resolved),
        **target,
        "local_image_storage_required": True,
    }
