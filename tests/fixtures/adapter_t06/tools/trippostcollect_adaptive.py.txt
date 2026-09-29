"""TripPostCollect adaptive candidate accounting for MediaCrawler search loops."""

from __future__ import annotations

from trippostcollect.application.events import (
    _utc_iso as _utc_iso,
    append_worker_execution_event as append_execution_event,
)
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from trippostcollect.application.candidates import AdaptiveAccumulator as _AdaptiveAccumulator
from trippostcollect.application.worker_inputs import env_int as _env_int
from trippostcollect.db.discovery_read import existing_platform_identities as _existing_platform_identities


def env_int(name: str, default: int) -> int:
    return _env_int(name, default, environ=os.environ)


def existing_platform_identities(platform: str) -> set[str]:
    return _existing_platform_identities(
        platform,
        db_path=os.environ.get("TRIPPOSTCOLLECT_DB_PATH", ""),
        xhs_target_key=os.environ.get("TRIPPOSTCOLLECT_XHS_DISCOVERY_TARGET_KEY", ""),
        xhs_account_id=os.environ.get("TRIPPOSTCOLLECT_XHS_ACCOUNT_ID", ""),
        xhs_fingerprint=os.environ.get("TRIPPOSTCOLLECT_XHS_DISCOVERY_QUERY_FINGERPRINT", ""),
        job_id=os.environ.get("TRIPPOSTCOLLECT_DISCOVERY_JOB_ID", ""),
        fingerprint=os.environ.get("TRIPPOSTCOLLECT_DISCOVERY_QUERY_FINGERPRINT", ""),
        resume_identities_path=os.environ.get("TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH", ""),
    )


def _fork_event_sink(event_type: str, details: dict[str, Any]) -> None:
    """在事件发生时解析本模块出口，保留原 patch 目标与容错行为。"""
    return append_execution_event(event_type, details)


@dataclass
class AdaptiveAccumulator(_AdaptiveAccumulator):
    """四站旧入口适配；已知集合仍在搜索入口只加载一次。"""

    event_sink: Callable[[str, dict[str, Any]], None] = field(
        default=_fork_event_sink, repr=False, compare=False,
    )

    @classmethod
    def from_environment(cls, platform: str) -> "AdaptiveAccumulator":
        return super().for_platform(
            platform, existing_identities=existing_platform_identities(platform),
        )








def should_reseed_douyin_frontier(
    *,
    saved_source_exhausted: bool,
    refresh_has_more: bool | int | None,
    refresh_next_cursor: str | None,
    refresh_new_candidate_count: int,
) -> bool:
    """Start a new cursor epoch only when refresh proves new identities and continuation."""
    return bool(
        saved_source_exhausted
        and refresh_has_more in (True, 1)
        and str(refresh_next_cursor or "").strip()
        and refresh_new_candidate_count > 0
    )
