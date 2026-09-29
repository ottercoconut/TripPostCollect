"""候选集合、计数与来源耗尽决策；T04 前由 fork 适配注入原事件容错出口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


def _discard_event(event_type: str, details: dict[str, Any]) -> None:
    """默认不持久化事件；T04 前由 fork 适配注入原事件容错出口。"""


@dataclass
class AdaptiveAccumulator:
    platform: str
    stagnation_basis: str = "valid_new"
    candidate_count: int = 0
    existing_identities: set[str] = field(default_factory=set)
    seen_candidate_identities: set[str] = field(default_factory=set)
    new_valid_identities: set[str] = field(default_factory=set)
    existing_valid_identities: set[str] = field(default_factory=set)
    stagnant_batches: int = 0
    batch_no: int = 0
    stop_reason: str = ""
    last_source_page: int | str | None = None
    last_source_offset: int | None = None
    last_source_cursor: int | str | None = None
    last_next_cursor: int | str | None = None
    last_source_has_more: bool | int | None = None
    last_raw_batch_count: int | None = None
    last_resume_page: int | str | None = None
    last_resume_offset: int | None = None
    last_resume_cursor: int | str | None = None
    last_batch_complete: bool = False
    last_discovery_phase: str = "frontier"
    stop_detail: str = ""
    skipped_candidate_failures: list[dict[str, Any]] = field(default_factory=list)
    skipped_candidate_identities: set[str] = field(default_factory=set)
    _batch_new_before: int = 0
    _batch_candidate_before: int = 0
    event_sink: Callable[[str, dict[str, Any]], None] = field(
        default=_discard_event, repr=False, compare=False,
    )

    @classmethod
    def for_platform(cls, platform: str, *, existing_identities: set[str]) -> "AdaptiveAccumulator":
        return cls(
            platform=platform,
            stagnation_basis=(
                "candidate_identity" if platform == "weibo" else "valid_new"
            ),
            existing_identities=existing_identities,
        )

    @property
    def can_continue(self) -> bool:
        return not self.stop_reason

    def should_reseed_frontier(self, **kwargs) -> bool:
        """抖音前沿纪元判定端口，保持原四条件。"""
        return should_reseed_douyin_frontier(**kwargs)

    def begin_batch(self) -> None:
        self.batch_no += 1
        self._batch_new_before = len(self.new_valid_identities)
        self._batch_candidate_before = len(self.seen_candidate_identities)

    def is_known(self, identity: str) -> bool:
        return bool(
            identity
            and (
                identity in self.existing_identities
                or identity in self.seen_candidate_identities
                or identity in self.skipped_candidate_identities
            )
        )

    def consider(self, identity: str, *, valid: bool) -> bool:
        self.candidate_count += 1
        if identity:
            self.seen_candidate_identities.add(identity)
        if valid and identity:
            if identity in self.existing_identities:
                self.existing_valid_identities.add(identity)
            else:
                self.new_valid_identities.add(identity)
        return False

    def skip_candidate_failure(
        self,
        identity: str,
        *,
        failure_scope: str,
        detail: str,
        error_code: str,
        attempts: int,
        retryable: bool | None = None,
        source_index: int | None = None,
        source_page: int | str | None = None,
        source_offset: int | None = None,
        source_cursor: int | str | None = None,
        discovery_phase: str = "frontier",
    ) -> bool:
        """Record a failed candidate as processed and continue past it."""

        self.candidate_count += 1
        failure = {
            "platform": self.platform,
            "identity": identity,
            "failure_scope": failure_scope,
            "detail": detail,
            "error_code": error_code,
            "attempts": max(1, int(attempts)),
            "source_index": source_index,
            "source_page": source_page,
            "source_offset": source_offset,
            "source_cursor": source_cursor,
            "discovery_phase": discovery_phase,
        }
        failure["retryable"] = (
            error_code == "image_download_retryable"
            if retryable is None
            else bool(retryable)
        )
        self.skipped_candidate_failures.append(failure)
        if identity:
            self.skipped_candidate_identities.add(identity)
            self.seen_candidate_identities.add(identity)
        self.event_sink("candidate_skipped", failure)
        return False

    def _record_source(
        self,
        *,
        source_page: int | str | None = None,
        source_offset: int | None = None,
        source_cursor: int | str | None = None,
        next_cursor: int | str | None = None,
        source_has_more: bool | int | None = None,
        raw_batch_count: int | None = None,
        resume_page: int | str | None = None,
        resume_offset: int | None = None,
        resume_cursor: int | str | None = None,
        batch_complete: bool = False,
        discovery_phase: str = "frontier",
    ) -> None:
        self.last_source_page = source_page
        self.last_source_offset = source_offset
        self.last_source_cursor = source_cursor
        self.last_next_cursor = next_cursor
        self.last_source_has_more = source_has_more
        self.last_raw_batch_count = raw_batch_count
        self.last_resume_page = resume_page
        self.last_resume_offset = resume_offset
        self.last_resume_cursor = resume_cursor
        self.last_batch_complete = batch_complete
        self.last_discovery_phase = discovery_phase

    def finish_batch(
        self,
        *,
        source_page: int | str | None = None,
        source_offset: int | None = None,
        source_cursor: int | str | None = None,
        next_cursor: int | str | None = None,
        source_has_more: bool | int | None = None,
        raw_batch_count: int | None = None,
        resume_page: int | str | None = None,
        resume_offset: int | None = None,
        resume_cursor: int | str | None = None,
        batch_complete: bool = False,
        discovery_phase: str = "frontier",
        count_stagnation: bool = True,
    ) -> bool:
        self._record_source(
            source_page=source_page,
            source_offset=source_offset,
            source_cursor=source_cursor,
            next_cursor=next_cursor,
            source_has_more=source_has_more,
            raw_batch_count=raw_batch_count,
            resume_page=resume_page,
            resume_offset=resume_offset,
            resume_cursor=resume_cursor,
            batch_complete=batch_complete,
            discovery_phase=discovery_phase,
        )
        added = len(self.new_valid_identities) - self._batch_new_before
        candidate_identities_added = len(self.seen_candidate_identities) - self._batch_candidate_before
        stagnation_progress = (
            candidate_identities_added
            if self.stagnation_basis == "candidate_identity"
            else added
        )
        if count_stagnation:
            self.stagnant_batches = (
                self.stagnant_batches + 1 if stagnation_progress == 0 else 0
            )
        details = {
            "platform": self.platform,
            "batch_no": self.batch_no,
            "candidate_count": self.candidate_count,
            "valid_new_count": len(self.new_valid_identities),
            "valid_existing_count": len(self.existing_valid_identities),
            "batch_new_count": added,
            "batch_candidate_identity_count": candidate_identities_added,
            "stagnant_batches": self.stagnant_batches,
            "stagnation_basis": self.stagnation_basis,
            "stop_reason": self.stop_reason or "continue",
            "source_page": source_page,
            "source_offset": source_offset,
            "source_cursor": source_cursor,
            "next_cursor": next_cursor,
            "source_has_more": source_has_more,
            "raw_batch_count": raw_batch_count,
            "resume_page": resume_page,
            "resume_offset": resume_offset,
            "resume_cursor": resume_cursor,
            "batch_complete": batch_complete,
            "discovery_phase": discovery_phase,
            "candidate_identities": sorted(self.seen_candidate_identities),
        }
        self.event_sink("adaptive_batch_completed", details)
        if self.stop_reason:
            self.event_sink("adaptive_search_stopped", self.summary())
            return True
        return False

    def mark_source_exhausted(
        self,
        detail: str,
        *,
        source_page: int | str | None = None,
        source_offset: int | None = None,
        source_cursor: int | str | None = None,
        next_cursor: int | str | None = None,
        source_has_more: bool | int | None = None,
        raw_batch_count: int | None = None,
        resume_page: int | str | None = None,
        resume_offset: int | None = None,
        resume_cursor: int | str | None = None,
        batch_complete: bool = True,
        discovery_phase: str = "frontier",
    ) -> None:
        self._record_source(
            source_page=source_page,
            source_offset=source_offset,
            source_cursor=source_cursor,
            next_cursor=next_cursor,
            source_has_more=source_has_more,
            raw_batch_count=raw_batch_count,
            resume_page=resume_page,
            resume_offset=resume_offset,
            resume_cursor=resume_cursor,
            batch_complete=batch_complete,
            discovery_phase=discovery_phase,
        )
        if not self.stop_reason:
            self.stop_reason = "source_exhausted"
        self.stop_detail = detail
        self.event_sink("adaptive_search_stopped", self.summary())

    def mark_runtime_failed(
        self,
        detail: str,
        *,
        source_page: int | str | None = None,
        source_offset: int | None = None,
        source_cursor: int | str | None = None,
        resume_page: int | str | None = None,
        resume_offset: int | None = None,
        resume_cursor: int | str | None = None,
        discovery_phase: str = "frontier",
    ) -> None:
        self._record_source(
            source_page=source_page,
            source_offset=source_offset,
            source_cursor=source_cursor,
            resume_page=resume_page,
            resume_offset=resume_offset,
            resume_cursor=resume_cursor,
            batch_complete=False,
            discovery_phase=discovery_phase,
        )
        if not self.stop_reason:
            self.stop_reason = "runtime_failed"
        self.stop_detail = detail
        self.event_sink("adaptive_search_stopped", self.summary())

    def summary(self) -> dict[str, Any]:
        result = {
            "platform": self.platform,
            "candidate_count": self.candidate_count,
            "valid_new_count": len(self.new_valid_identities),
            "valid_existing_count": len(self.existing_valid_identities),
            "stagnant_batches": self.stagnant_batches,
            "stagnation_basis": self.stagnation_basis,
            "stop_reason": self.stop_reason or "running",
            "pages_fetched": self.batch_no,
            "source_page": self.last_source_page,
            "source_offset": self.last_source_offset,
            "source_cursor": self.last_source_cursor,
            "next_cursor": self.last_next_cursor,
            "source_has_more": self.last_source_has_more,
            "raw_batch_count": self.last_raw_batch_count,
            "resume_page": self.last_resume_page,
            "resume_offset": self.last_resume_offset,
            "resume_cursor": self.last_resume_cursor,
            "batch_complete": self.last_batch_complete,
            "discovery_phase": self.last_discovery_phase,
            "stop_detail": self.stop_detail,
        }
        result["skipped_candidate_count"] = len(
            self.skipped_candidate_failures
        )
        result["skipped_candidate_failures"] = list(
            self.skipped_candidate_failures
        )
        result["candidate_identities"] = sorted(self.seen_candidate_identities)
        return result


# TripPostCollect：T06 从 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303 迁入。
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
