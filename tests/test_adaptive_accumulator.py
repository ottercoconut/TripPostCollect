from __future__ import annotations

import sqlite3
import sys
from importlib import import_module
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MEDIACRAWLER_TOOLS = ROOT / "tools" / "MediaCrawler" / "tools"
if str(MEDIACRAWLER_TOOLS) not in sys.path:
    sys.path.insert(0, str(MEDIACRAWLER_TOOLS))

trippostcollect_adaptive = import_module("trippostcollect_adaptive")


def test_stagnation_counts_batches_without_new_valid_records(monkeypatch) -> None:
    monkeypatch.setattr(trippostcollect_adaptive, "append_execution_event", lambda *args, **kwargs: None)
    accumulator = trippostcollect_adaptive.AdaptiveAccumulator(
        platform="xhs",
        hard_limit=20,
        target_new=5,
        max_stagnant_batches=2,
    )

    accumulator.begin_batch()
    accumulator.consider("candidate-1", valid=False)
    assert accumulator.finish_batch(source_page=1) is False
    assert accumulator.stagnant_batches == 1

    accumulator.begin_batch()
    accumulator.consider("candidate-2", valid=False)
    assert accumulator.finish_batch(source_page=2) is True
    assert accumulator.stop_reason == "stagnated"


def test_new_valid_record_resets_stagnation(monkeypatch) -> None:
    monkeypatch.setattr(trippostcollect_adaptive, "append_execution_event", lambda *args, **kwargs: None)
    accumulator = trippostcollect_adaptive.AdaptiveAccumulator(
        platform="xhs",
        hard_limit=20,
        target_new=5,
        max_stagnant_batches=3,
    )

    accumulator.begin_batch()
    accumulator.consider("candidate-1", valid=False)
    accumulator.finish_batch(source_page=1)
    accumulator.begin_batch()
    accumulator.consider("candidate-2", valid=True)
    accumulator.finish_batch(source_page=2)

    assert accumulator.stagnant_batches == 0


def test_xhs_target_stops_before_candidate_hard_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        trippostcollect_adaptive,
        "append_execution_event",
        lambda *args, **kwargs: None,
    )
    accumulator = trippostcollect_adaptive.AdaptiveAccumulator(
        platform="xhs",
        hard_limit=300,
        target_new=50,
        max_stagnant_batches=8,
    )

    for index in range(49):
        assert accumulator.consider(f"candidate-{index}", valid=True) is False

    assert accumulator.consider("candidate-49", valid=True) is True
    assert accumulator.candidate_count == 50
    assert accumulator.candidate_count < accumulator.hard_limit
    assert accumulator.stop_reason == "target_new_met"


def test_source_exhausted_mode_ignores_quantity_and_stagnation_stops(monkeypatch) -> None:
    monkeypatch.setattr(
        trippostcollect_adaptive,
        "append_execution_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_COMPLETION_MODE", "source-exhausted")
    accumulator = trippostcollect_adaptive.AdaptiveAccumulator.from_environment(
        "xhs",
        hard_limit=1,
    )
    accumulator.max_stagnant_batches = 1

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
    assert accumulator.summary()["quantity_limits_enforced"] is False


def test_weibo_stagnation_tracks_candidate_identity_progress(monkeypatch) -> None:
    monkeypatch.setattr(trippostcollect_adaptive, "append_execution_event", lambda *args, **kwargs: None)
    accumulator = trippostcollect_adaptive.AdaptiveAccumulator(
        platform="weibo",
        hard_limit=20,
        target_new=5,
        max_stagnant_batches=2,
        stagnation_basis="candidate_identity",
    )

    accumulator.begin_batch()
    accumulator.consider("text-only-1", valid=False)
    assert accumulator.finish_batch(source_page=1) is False
    assert accumulator.stagnant_batches == 0

    accumulator.begin_batch()
    assert accumulator.finish_batch(source_page=2) is False
    accumulator.begin_batch()
    assert accumulator.finish_batch(source_page=3) is True
    assert accumulator.stop_reason == "stagnated"


def test_douyin_exhausted_cursor_reseed_requires_new_candidate_continuation() -> None:
    should_reseed = trippostcollect_adaptive.should_reseed_douyin_frontier

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

    accumulator = trippostcollect_adaptive.AdaptiveAccumulator.from_environment(
        "douyin",
        hard_limit=10,
    )

    assert accumulator.is_known("seen-video") is True
