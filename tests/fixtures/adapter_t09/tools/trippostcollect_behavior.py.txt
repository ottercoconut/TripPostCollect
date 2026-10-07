"""Bridge MediaCrawler browser pages into TripPostCollect's required behavior stage."""

from __future__ import annotations

import os
import json
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page


from trippostcollect.application.worker_inputs import _enabled as _enabled
from trippostcollect.runtime import behavior as _behavior


from trippostcollect.runtime.behavior import project_browser_args as project_browser_args


async def install_project_runtime_hints(context: BrowserContext) -> None:
    if not _enabled():
        return
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    if not scripts_dir.is_dir():
        raise RuntimeError("required TripPostCollect runtime hint configuration is incomplete")

    await _behavior.install_project_runtime_hints(context)


async def run_required_human_behavior(page: Page, platform_key: str) -> dict[str, Any]:
    if not _enabled():
        return {"status": "disabled", "platform": platform_key}

    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    profile_name = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "social_high_risk").strip()
    if not scripts_dir.is_dir() or not evidence_path:
        raise RuntimeError("required TripPostCollect human behavior configuration is incomplete")

    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import wait_for_xhs_search_ready, write_evidence

    return await _behavior.run_required_human_behavior(
        page,
        platform_key=platform_key,
        xhs_search_ready=wait_for_xhs_search_ready,
        write_evidence=write_evidence,
        evidence_path=evidence_path,
        profile_name=profile_name,
    )


async def run_required_request_pause(stage: str, minimum: float, maximum: float) -> dict[str, Any]:
    if not _enabled():
        raise RuntimeError("required TripPostCollect request pacing is disabled")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    profile_name = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path:
        raise RuntimeError("required TripPostCollect request pacing configuration is incomplete")
    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import run_guarded_request_pause

    return await run_guarded_request_pause(
        evidence_path=evidence_path,
        profile_name=profile_name,
        stage=stage,
        minimum=minimum,
        maximum=maximum,
    )


async def run_required_continuity_behavior(page: Page, stage: str) -> dict[str, Any]:
    if not _enabled():
        raise RuntimeError("required TripPostCollect continuity behavior is disabled")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    profile_name = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path or profile_name != "xhs_guarded":
        raise RuntimeError("required TripPostCollect continuity behavior configuration is incomplete")
    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import run_xhs_continuity_behavior

    return await run_xhs_continuity_behavior(
        page,
        evidence_path=evidence_path,
        stage=stage,
    )


async def run_required_api_captcha_verification(
    page: Page,
    *,
    verify_type: str,
    verify_uuid: str,
    verify_biz: int,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    if not _enabled():
        raise RuntimeError("required TripPostCollect API captcha verification is disabled")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    profile_name = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path or profile_name != "xhs_guarded":
        raise RuntimeError("required TripPostCollect API captcha configuration is incomplete")
    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import run_xhs_api_captcha_verification

    kwargs: dict[str, Any] = {
        "evidence_path": evidence_path,
        "verify_type": verify_type,
        "verify_uuid": verify_uuid,
        "verify_biz": verify_biz,
    }
    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = timeout_seconds
    return await run_xhs_api_captcha_verification(page, **kwargs)


async def inspect_visible_page_state(page: Page) -> tuple[str, dict[str, bool]]:
    """Reuse the project's visible challenge checks without replacing run evidence."""
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    if not scripts_dir.is_dir():
        raise RuntimeError("required TripPostCollect page inspection configuration is incomplete")
    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import visible_page_state

    return await visible_page_state(page)


async def record_platform_security_limit(
    page: Page,
    *,
    stage: str,
    visible_text_sample: str,
    visible_markers: dict[str, bool],
) -> dict[str, Any]:
    """Persist an XHS terminal account restriction through the project evidence writer."""
    if not _enabled():
        raise RuntimeError("required TripPostCollect platform security evidence is disabled")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path:
        raise RuntimeError("required TripPostCollect platform security evidence configuration is incomplete")
    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import record_xhs_platform_security_limit

    return await record_xhs_platform_security_limit(
        page,
        evidence_path=evidence_path,
        stage=stage,
        visible_text_sample=visible_text_sample,
        visible_markers=visible_markers,
    )


async def run_requested_post_interaction(
    page: Page,
    *,
    platform_key: str,
    requested_mode: str,
    note_id: str,
) -> dict[str, Any]:
    if requested_mode == "none":
        return {"status": "disabled", "platform": platform_key}
    if platform_key != "xhs" or not _enabled():
        raise RuntimeError("XHS post interaction requires the enabled project behavior bridge")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path:
        raise RuntimeError("required TripPostCollect post interaction configuration is incomplete")
    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import run_xhs_post_interaction

    return await run_xhs_post_interaction(
        page,
        evidence_path=evidence_path,
        requested_mode=requested_mode,
        note_id=note_id,
    )
