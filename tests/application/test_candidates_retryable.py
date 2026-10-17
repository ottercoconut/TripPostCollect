"""可重试的候选失败不写入跨轮 seen（小红书装配 persist_retryable_skips=False）；通用平台默认行为不变。"""

from __future__ import annotations

import pytest

from trippostcollect.application.candidates import AdaptiveAccumulator


def _skip(accumulator: AdaptiveAccumulator, identity: str, **kwargs) -> None:
    accumulator.skip_candidate_failure(
        identity, failure_scope="post", detail="note_detail_not_observed", error_code="note_detail_not_observed",
        attempts=2, **kwargs,
    )


def test_retryable_failure_is_run_only_when_not_persisted():
    events = []
    accumulator = AdaptiveAccumulator.for_platform("xhs", existing_identities=set(), persist_retryable_skips=False)
    accumulator.event_sink = lambda kind, details: events.append((kind, details))
    accumulator.begin_batch()

    _skip(accumulator, "retry-me", retryable=True)
    _skip(accumulator, "final", retryable=False)

    assert accumulator.is_known("retry-me") and accumulator.is_known("final")
    assert accumulator.run_only_skipped_identities == {"retry-me"}
    assert accumulator.seen_candidate_identities == {"final"}
    assert accumulator.skipped_candidate_identities == {"final"}
    assert [details["identity"] for kind, details in events if kind == "candidate_skipped"] == ["retry-me", "final"]
    summary = accumulator.summary()
    assert summary["candidate_identities"] == ["final"]
    assert [item["identity"] for item in summary["skipped_candidate_failures"]] == ["retry-me", "final"]
    accumulator.finish_batch(batch_complete=True, discovery_phase="full_scan")
    (batch,) = [details for kind, details in events if kind == "adaptive_batch_completed"]
    assert batch["batch_candidate_identity_count"] == 1
    assert batch["candidate_identities"] == ["final"]


def test_image_retryable_default_follows_error_code_when_not_persisted():
    accumulator = AdaptiveAccumulator.for_platform("xhs", existing_identities=set(), persist_retryable_skips=False)

    accumulator.skip_candidate_failure(
        "image-temporary", failure_scope="image", detail="image_download_failed",
        error_code="image_download_retryable", attempts=3,
    )

    assert accumulator.run_only_skipped_identities == {"image-temporary"}
    assert "image-temporary" not in accumulator.seen_candidate_identities


@pytest.mark.parametrize("platform", ["bilibili", "weibo", "douyin", "zhihu"])
def test_general_platforms_keep_persisting_retryable_failures(platform):
    accumulator = AdaptiveAccumulator.for_platform(platform, existing_identities=set())

    _skip(accumulator, "retry-me", retryable=True)

    assert accumulator.persist_retryable_skips is True
    assert accumulator.run_only_skipped_identities == set()
    assert accumulator.seen_candidate_identities == {"retry-me"}
    assert accumulator.summary()["candidate_identities"] == ["retry-me"]


def test_exhaustion_stop_event_carries_source_visit_only_when_provided():
    accumulator = AdaptiveAccumulator.for_platform("bilibili", existing_identities=set())
    accumulator.mark_source_exhausted("has_more_false", source_page=3)
    assert "source_visit_id" not in accumulator.summary()

    gated = AdaptiveAccumulator.for_platform("xhs", existing_identities=set(), persist_retryable_skips=False)
    gated.mark_source_exhausted(
        "has_more_false", source_page=3, discovery_phase="full_scan", source_visit_id="ab" * 16 + ":v000042",
    )
    assert gated.summary()["source_visit_id"] == "ab" * 16 + ":v000042"
