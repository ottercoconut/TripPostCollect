"""T14 自 fork 离线用例移植（tools/MediaCrawler/tests/test_trippostcollect_adaptive.py，按台账 target_file）。

原用例经 fork 薄转发 tools/trippostcollect_adaptive 运行：其累加器即根 AdaptiveAccumulator，默认事件出口在
事件发生时解析模块全局 append_execution_event（= 根 append_worker_execution_event），from_environment 从 env
读取已知集合范围。移植后：

- ``worker_accumulator`` 构造根累加器，事件出口同样在事件发生时解析本模块的 ``append_execution_event``，
  原用例对 fork 模块该名的 monkeypatch 改为对本模块同名；
- ``from_environment`` 改用正式 worker 的装配函数（小红书、知乎累加器工厂），不再经 fork。

用例名、参数与断言逐条不变。
"""

from __future__ import annotations

import json
import sqlite3
import sys

from trippostcollect.application.candidates import AdaptiveAccumulator, should_reseed_douyin_frontier
from trippostcollect.application.events import append_worker_execution_event
from trippostcollect.application.worker_inputs import worker_config
from trippostcollect.platforms import entry


THIS_MODULE = sys.modules[__name__]
# 正式 worker 的事件出口；用例可 monkeypatch 本模块此名（原用例 patch fork 模块同名全局）。
append_execution_event = append_worker_execution_event


def worker_accumulator(**kwargs) -> AdaptiveAccumulator:
    def sink(event_type, details):
        return THIS_MODULE.append_execution_event(event_type, details)

    return AdaptiveAccumulator(event_sink=sink, **kwargs)


def from_environment(platform: str) -> AdaptiveAccumulator:
    config = worker_config()
    config.PLATFORM = platform
    if platform == "xhs":
        return entry.xhs_dependencies(config, repair=False)["ports"].accumulator_factory()
    if platform == "zhihu":
        return entry._zhihu_dependencies(config)[1].accumulator_factory(platform)
    raise ValueError(platform)


def test_batch_event_records_pagination_metadata(monkeypatch, tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    accumulator = worker_accumulator(
        platform="xhs",
    )

    accumulator.begin_batch()
    accumulator.consider("note-1", valid=True)
    stopped = accumulator.finish_batch(
        source_page=2,
        source_cursor="search-id",
        source_has_more=True,
        raw_batch_count=20,
        resume_page=3,
        resume_cursor="next-search-id",
        batch_complete=True,
        discovery_phase="frontier",
    )

    event = json.loads(state_path.read_text(encoding="utf-8"))["events"][0]
    assert stopped is False
    assert event["details"]["source_page"] == 2
    assert event["details"]["source_cursor"] == "search-id"
    assert event["details"]["source_has_more"] is True
    assert event["details"]["raw_batch_count"] == 20
    assert event["details"]["resume_page"] == 3
    assert event["details"]["resume_cursor"] == "next-search-id"
    assert event["details"]["batch_complete"] is True


def test_runtime_failure_has_distinct_stop_reason(monkeypatch, tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    accumulator = worker_accumulator(
        platform="douyin",
    )

    accumulator.mark_runtime_failed(
        "search_request_failed",
        source_page=3,
        source_cursor="cursor-2",
    )

    event = json.loads(state_path.read_text(encoding="utf-8"))["events"][0]
    assert event["details"]["stop_reason"] == "runtime_failed"
    assert event["details"]["stop_detail"] == "search_request_failed"
    assert event["details"]["source_page"] == 3


def test_image_failed_candidate_is_recorded_seen_and_does_not_block_batch(
    monkeypatch, tmp_path
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    accumulator = worker_accumulator(
        platform="zhihu",
    )
    accumulator.begin_batch()

    assert accumulator.skip_candidate_failure(
        "answer-1",
        failure_scope="image",
        detail="image_download_failed",
        error_code="image_download_retryable",
        attempts=3,
        source_index=2,
        source_page=4,
    ) is False
    assert accumulator.is_known("answer-1") is True
    assert "answer-1" in accumulator.seen_candidate_identities
    assert accumulator.finish_batch(
        source_page=5,
        resume_page=6,
        batch_complete=True,
    ) is False

    summary = accumulator.summary()
    assert summary["stop_reason"] == "running"
    assert summary["resume_page"] == 6
    assert summary["batch_complete"] is True
    assert summary["candidate_count"] == 1
    assert summary["candidate_identities"] == ["answer-1"]
    assert summary["skipped_candidate_failures"][0]["attempts"] == 3
    assert summary["skipped_candidate_failures"][0]["retryable"] is True


def test_source_exhaustion_is_claimed_after_failed_candidate_is_skipped(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        THIS_MODULE, "append_execution_event",
        lambda *args, **kwargs: None,
    )
    accumulator = worker_accumulator(
        platform="xhs",
    )
    accumulator.skip_candidate_failure(
        "note-1",
        failure_scope="image",
        detail="image_download_failed",
        error_code="image_download_retryable",
        attempts=3,
        source_page=2,
        source_cursor="search-id",
    )

    accumulator.mark_source_exhausted(
        "has_more_false",
        source_page=4,
        source_cursor="search-id",
        source_has_more=False,
        resume_page=5,
        resume_cursor="search-id",
    )

    summary = accumulator.summary()
    assert summary["stop_reason"] == "source_exhausted"
    assert summary["stop_detail"] == "has_more_false"
    assert summary["resume_page"] == 5
    assert summary["resume_cursor"] == "search-id"
    assert summary["source_has_more"] is False
    assert summary["candidate_identities"] == ["note-1"]


def test_skipped_candidates_never_trigger_legacy_quantity_stop(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        THIS_MODULE, "append_execution_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_TARGET_NEW_POSTS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_CANDIDATE_HARD_LIMIT", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_MAX_STAGNANT_BATCHES", "1")
    accumulator = worker_accumulator(
        platform="weibo",
    )

    stopped = accumulator.skip_candidate_failure(
        "mblog-1",
        failure_scope="image",
        detail="image_download_failed",
        error_code="image_download_retryable",
        attempts=3,
        source_page=7,
    )

    assert stopped is False
    assert accumulator.summary()["stop_reason"] == "running"
    assert accumulator.summary()["candidate_identities"] == ["mblog-1"]


def test_existing_database_identity_is_counted_without_stopping(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        conn.execute("INSERT INTO web_posts VALUES ('xhs', 'existing-note', NULL)")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    accumulator = from_environment("xhs")
    accumulator.begin_batch()

    assert accumulator.consider("existing-note", valid=True) is False
    assert len(accumulator.new_valid_identities) == 0
    assert len(accumulator.existing_valid_identities) == 1
    assert accumulator.consider("new-note", valid=True) is False
    assert accumulator.stop_reason == ""


def test_resume_identity_is_counted_without_stopping(monkeypatch, tmp_path) -> None:
    resume_path = tmp_path / "resume.json"
    resume_path.write_text(json.dumps(["prior-note"]), encoding="utf-8")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setenv("TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH", str(resume_path))
    accumulator = from_environment("xhs")
    accumulator.begin_batch()

    assert accumulator.consider("prior-note", valid=True) is False
    assert accumulator.consider("new-note", valid=True) is False
    assert len(accumulator.existing_valid_identities) == 1
    assert len(accumulator.new_valid_identities) == 1


def test_known_identity_can_be_skipped_before_detail_fetch() -> None:
    accumulator = worker_accumulator(
        platform="douyin",
        existing_identities={"known-aweme"},
    )

    assert accumulator.is_known("known-aweme") is True
    assert accumulator.candidate_count == 0
    assert accumulator.is_known("new-aweme") is False


def test_xhs_persisted_seen_candidate_is_loaded_before_detail(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "xhs-seen.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        conn.execute(
            """
            CREATE TABLE xhs_discovery_seen_candidates (
                target_key TEXT,
                account_id TEXT,
                query_fingerprint TEXT,
                platform_post_id TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO xhs_discovery_seen_candidates VALUES (?, ?, ?, ?)",
            ("qingdao_travel", "xhs-a01", "fingerprint", "seen-invalid-note"),
        )
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_DISCOVERY_TARGET_KEY", "qingdao_travel")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_ACCOUNT_ID", "xhs-a01")
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_DISCOVERY_QUERY_FINGERPRINT",
        "fingerprint",
    )

    accumulator = from_environment("xhs")

    assert accumulator.is_known("seen-invalid-note") is True


def test_common_operator_exclusion_is_loaded_before_detail(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "candidate-exclusion.sqlite"
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
            """
            CREATE TABLE crawl_discovery_candidate_exclusions (
                job_id INTEGER,
                platform_key TEXT,
                query_fingerprint TEXT,
                platform_post_id TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO crawl_discovery_candidate_exclusions VALUES (51, 'zhihu', 'fingerprint', 'excluded-answer')"
        )
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_JOB_ID", "51")
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_DISCOVERY_QUERY_FINGERPRINT",
        "fingerprint",
    )

    accumulator = from_environment("zhihu")

    assert accumulator.is_known("excluded-answer") is True
    assert accumulator.candidate_count == 0


def test_refresh_batch_does_not_consume_frontier_stagnation(monkeypatch) -> None:
    monkeypatch.setattr(
        THIS_MODULE, "append_execution_event",
        lambda *args, **kwargs: None,
    )
    accumulator = worker_accumulator(
        platform="weibo",
    )
    accumulator.begin_batch()

    stopped = accumulator.finish_batch(
        source_page=1,
        resume_page=2,
        batch_complete=True,
        discovery_phase="refresh",
        count_stagnation=False,
    )

    assert stopped is False
    assert accumulator.stagnant_batches == 0


def test_candidate_identity_stagnation_allows_new_invalid_results(monkeypatch) -> None:
    monkeypatch.setattr(
        THIS_MODULE, "append_execution_event",
        lambda *args, **kwargs: None,
    )
    accumulator = worker_accumulator(
        platform="weibo",
        stagnation_basis="candidate_identity",
    )
    accumulator.begin_batch()
    accumulator.consider("text-only", valid=False)

    stopped = accumulator.finish_batch(source_page=1)

    assert stopped is False
    assert accumulator.stagnant_batches == 0


def test_douyin_reseed_requires_new_candidate_and_cursor() -> None:
    assert should_reseed_douyin_frontier(
        saved_source_exhausted=True,
        refresh_has_more=True,
        refresh_next_cursor="new-search-id",
        refresh_new_candidate_count=1,
    ) is True
    assert should_reseed_douyin_frontier(
        saved_source_exhausted=True,
        refresh_has_more=True,
        refresh_next_cursor="new-search-id",
        refresh_new_candidate_count=0,
    ) is False
