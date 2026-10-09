"""TripPostCollect tests for adaptive accumulator.

T14：原用例经 fork 的 tools/trippostcollect_adaptive 薄转发（类与判据均重导出根实现，事件出口为
模块全局）运行；现直接使用根 trippostcollect.application.candidates，以显式 event_sink 捕获事件，
"从环境装配"改用正式 worker 装配函数（entry 的小红书/抖音累加器工厂）。断言不变。
"""

from __future__ import annotations

import sqlite3

from trippostcollect.application import candidates
from trippostcollect.application.worker_inputs import worker_config
from trippostcollect.platforms import entry


def _discard(*args, **kwargs) -> None:
    return None


def _accumulator(events=None, **kwargs) -> candidates.AdaptiveAccumulator:
    sink = _discard if events is None else (lambda event_type, details: events.append((event_type, details)))
    return candidates.AdaptiveAccumulator(event_sink=sink, **kwargs)


def _from_environment(platform: str) -> candidates.AdaptiveAccumulator:
    """正式 worker 装配的累加器：已知集合范围在构造时从 env 读取一次，事件出口替换为丢弃。"""
    config = worker_config()
    config.PLATFORM = platform
    if platform == "xhs":
        accumulator = entry.xhs_dependencies(config, repair=False)["ports"].accumulator_factory()
    else:
        accumulator = entry.douyin_dependencies(config)["ports"].candidates()
    accumulator.event_sink = _discard
    return accumulator


def test_stagnation_is_diagnostic_and_never_stops_discovery(monkeypatch) -> None:
    accumulator = _accumulator(
        platform="xhs",
    )

    accumulator.begin_batch()
    accumulator.consider("candidate-1", valid=False)
    assert accumulator.finish_batch(source_page=1) is False
    assert accumulator.stagnant_batches == 1

    accumulator.begin_batch()
    accumulator.consider("candidate-2", valid=False)
    assert accumulator.finish_batch(source_page=2) is False
    assert accumulator.stagnant_batches == 2
    assert accumulator.stop_reason == ""
    assert accumulator.can_continue is True


def test_new_valid_record_resets_stagnation(monkeypatch) -> None:
    accumulator = _accumulator(
        platform="xhs",
    )

    accumulator.begin_batch()
    accumulator.consider("candidate-1", valid=False)
    accumulator.finish_batch(source_page=1)
    accumulator.begin_batch()
    accumulator.consider("candidate-2", valid=True)
    accumulator.finish_batch(source_page=2)

    assert accumulator.stagnant_batches == 0


def test_candidate_and_valid_counts_never_stop_before_source_exhaustion(monkeypatch) -> None:
    accumulator = _accumulator(
        platform="xhs",
    )

    for index in range(500):
        assert accumulator.consider(f"candidate-{index}", valid=True) is False

    assert accumulator.candidate_count == 500
    assert accumulator.stop_reason == ""
    assert accumulator.can_continue is True


def test_legacy_quantity_environment_cannot_restore_quantity_stops(monkeypatch) -> None:
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "target-new-posts")
    monkeypatch.setenv("TRIPPOSTCOLLECT_TARGET_NEW_POSTS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_CANDIDATE_HARD_LIMIT", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_MAX_STAGNANT_BATCHES", "1")
    accumulator = _from_environment("xhs")

    accumulator.begin_batch()
    assert accumulator.consider("candidate-1", valid=True) is False
    assert accumulator.consider("candidate-2", valid=False) is False
    assert accumulator.finish_batch(source_page=1) is False
    assert accumulator.candidate_count == 2
    assert accumulator.can_continue is True
    assert accumulator.stop_reason == ""

    accumulator.mark_source_exhausted("empty_page", source_page=2)
    assert accumulator.stop_reason == "source_exhausted"
    assert accumulator.can_continue is False
    summary = accumulator.summary()
    assert "target_new" not in summary
    assert "hard_limit" not in summary
    assert "completion_mode" not in summary


def test_completed_batch_event_keeps_candidate_identities_for_failed_run_resume(monkeypatch) -> None:
    events = []
    accumulator = _accumulator(events, platform="xhs")

    accumulator.begin_batch()
    accumulator.consider("valid-note", valid=True)
    accumulator.consider("invalid-note", valid=False)
    accumulator.finish_batch(source_page=1, batch_complete=True)

    assert events[-1][0] == "adaptive_batch_completed"
    assert events[-1][1]["candidate_identities"] == ["invalid-note", "valid-note"]
    assert "target_new" not in events[-1][1]
    assert "hard_limit" not in events[-1][1]


def test_weibo_stagnation_tracks_candidate_identity_progress(monkeypatch) -> None:
    accumulator = _accumulator(
        platform="weibo",
        stagnation_basis="candidate_identity",
    )

    accumulator.begin_batch()
    accumulator.consider("text-only-1", valid=False)
    assert accumulator.finish_batch(source_page=1) is False
    assert accumulator.stagnant_batches == 0

    accumulator.begin_batch()
    assert accumulator.finish_batch(source_page=2) is False
    accumulator.begin_batch()
    assert accumulator.finish_batch(source_page=3) is False
    assert accumulator.stagnant_batches == 2
    assert accumulator.stop_reason == ""


def test_douyin_exhausted_cursor_reseed_requires_new_candidate_continuation() -> None:
    should_reseed = candidates.should_reseed_douyin_frontier

    assert should_reseed(
        saved_source_exhausted=True,
        refresh_has_more=True,
        refresh_next_cursor="new-search-id",
        refresh_new_candidate_count=1,
    ) is True
    assert should_reseed(
        saved_source_exhausted=True,
        refresh_has_more=True,
        refresh_next_cursor="new-search-id",
        refresh_new_candidate_count=0,
    ) is False
    assert should_reseed(
        saved_source_exhausted=True,
        refresh_has_more=False,
        refresh_next_cursor="new-search-id",
        refresh_new_candidate_count=1,
    ) is False
    assert should_reseed(
        saved_source_exhausted=True,
        refresh_has_more=True,
        refresh_next_cursor="",
        refresh_new_candidate_count=1,
    ) is False


def test_common_persisted_seen_candidate_is_loaded_before_detail(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "seen.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        conn.execute(
            """
            CREATE TABLE crawl_discovery_seen_candidates (
                job_id INTEGER,
                platform_key TEXT,
                query_fingerprint TEXT,
                platform_post_id TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO crawl_discovery_seen_candidates VALUES (24, 'douyin', 'fingerprint', 'seen-video')"
        )
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_JOB_ID", "24")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_QUERY_FINGERPRINT", "fingerprint")

    accumulator = _from_environment("douyin")

    assert accumulator.is_known("seen-video") is True
