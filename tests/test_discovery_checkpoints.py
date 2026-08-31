"""TripPostCollect tests for discovery checkpoints."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.scheduler.discovery import (
    load_candidate_exclusions,
    load_checkpoint,
    load_skipped_candidates,
    load_seen_candidates,
    query_fingerprint,
    save_candidate_exclusion,
    save_checkpoint,
    save_seen_candidates,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

crawl_runner = import_module("crawl_runner")
mediacrawler_crawl = import_module("mediacrawler_crawl")


def insert_job(conn: sqlite3.Connection, params: dict) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO crawl_jobs (
            job_key, site_key, target_url, job_kind, next_run_at, params_json
        ) VALUES ('weibo-test', 'weibo', '', 'mediacrawler_search', datetime('now'), ?)
        """,
        (json.dumps(params),),
    )
    conn.commit()
    return conn.execute("SELECT * FROM crawl_jobs WHERE job_key='weibo-test'").fetchone()


def test_query_fingerprint_ignores_runtime_budget_but_tracks_source_options() -> None:
    base = {
        "platform": "weibo",
        "keyword": "青岛旅游",
        "top_refresh_max_pages": 3,
        "timeout_per_platform": 1200,
        "search_type": "default",
    }
    changed_budget = {**base, "top_refresh_max_pages": 5, "timeout_per_platform": 2400}
    changed_source = {**base, "search_type": "real_time"}

    assert query_fingerprint("weibo", "青岛旅游", base) == query_fingerprint(
        "weibo", "青岛旅游", changed_budget
    )
    assert query_fingerprint("weibo", "青岛旅游", base) != query_fingerprint(
        "weibo", "青岛旅游", changed_source
    )


def test_checkpoint_round_trip_preserves_page_offset_and_cursor(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoint.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(conn, {"platform": "douyin", "keyword": "青岛旅游"})
        fingerprint = query_fingerprint("douyin", "青岛旅游", {})
        save_checkpoint(
            conn,
            job_id=int(row["id"]),
            platform_key="douyin",
            keyword="青岛旅游",
            query_fingerprint_value=fingerprint,
            resume_page=11,
            resume_offset=150,
            resume_cursor="cursor-10",
            source_has_more=True,
            last_batch_complete=True,
            last_stop_reason="runtime_failed",
            last_run_id="run-1",
        )
        checkpoint = load_checkpoint(
            conn,
            job_id=int(row["id"]),
            query_fingerprint_value=fingerprint,
        )

    assert checkpoint is not None
    assert checkpoint["resume_page"] == 11
    assert checkpoint["resume_offset"] == 150
    assert checkpoint["resume_cursor"] == "cursor-10"
    assert checkpoint["status"] == "active"


def test_operator_candidate_exclusion_is_auditable_and_loaded_as_skipped(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "exclusion.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(conn, {"platform": "zhihu", "keyword": "青岛旅游"})
        fingerprint = query_fingerprint("zhihu", "青岛旅游", {})
        save_candidate_exclusion(
            conn,
            job_id=int(row["id"]),
            platform_key="zhihu",
            query_fingerprint_value=fingerprint,
            platform_post_id="answer-persistent-failure",
            reason="operator_excluded_after_repeated_image_failure",
            authorized_run_id="operator-run-1",
            evidence={"attempts_per_round": 3, "rounds": 2},
        )
        conn.commit()

        assert load_candidate_exclusions(
            conn,
            job_id=int(row["id"]),
            platform_key="zhihu",
            query_fingerprint_value=fingerprint,
        ) == {"answer-persistent-failure"}
        assert load_skipped_candidates(
            conn,
            job_id=int(row["id"]),
            platform_key="zhihu",
            query_fingerprint_value=fingerprint,
        ) == {"answer-persistent-failure"}
        exclusion = conn.execute(
            "SELECT * FROM crawl_discovery_candidate_exclusions"
        ).fetchone()

    assert exclusion is not None
    assert exclusion["reason"] == "operator_excluded_after_repeated_image_failure"
    assert json.loads(exclusion["evidence_json"]) == {
        "attempts_per_round": 3,
        "rounds": 2,
    }


def test_executor_commits_cursor_from_durable_pagination_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "executor.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(conn, {"platform": "douyin", "keyword": "青岛旅游"})
        job_id = int(row["id"])
    fingerprint = query_fingerprint("douyin", "青岛旅游", {})
    args = SimpleNamespace(
        discovery_job_id=job_id,
        discovery_query_fingerprint=fingerprint,
        discovery_run_id="run-2",
        no_checkpoint_write=False,
        db=str(db_path),
        keyword="青岛旅游",
        start_page=10,
    )

    result = mediacrawler_crawl.persist_discovery_checkpoint(
        args,
        "douyin",
        {
            "stop_event": {
                "source_page": 10,
                "resume_page": 11,
                "resume_offset": 150,
                "resume_cursor": "next-cursor",
                "source_has_more": True,
                "batch_complete": True,
                "discovery_phase": "frontier",
                "stop_reason": "runtime_failed",
                "stop_detail": "process_interrupted",
                "candidate_identities": ["video-1", "image-1"],
            }
        },
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        checkpoint = load_checkpoint(
            conn,
            job_id=job_id,
            query_fingerprint_value=fingerprint,
        )
    assert result["skipped"] is False
    assert checkpoint is not None
    assert checkpoint["resume_page"] == 11
    assert checkpoint["resume_offset"] == 150
    assert checkpoint["resume_cursor"] == "next-cursor"
    assert checkpoint["last_run_id"] == "run-2"
    assert checkpoint["last_stop_detail"] == "process_interrupted"


def test_executor_keeps_first_douyin_search_id_when_response_logids_rotate(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "stable-cursor.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(conn, {"platform": "douyin", "keyword": "青岛旅游"})
        job_id = int(row["id"])
    fingerprint = query_fingerprint("douyin", "青岛旅游", {})
    args = SimpleNamespace(
        discovery_job_id=job_id,
        discovery_query_fingerprint=fingerprint,
        discovery_run_id="stable-run",
        no_checkpoint_write=False,
        db=str(db_path),
        keyword="青岛旅游",
        start_page=1,
    )

    result = mediacrawler_crawl.persist_discovery_checkpoint(
        args,
        "douyin",
        {
            "batches": [
                {
                    "platform": "douyin",
                    "discovery_phase": "frontier",
                    "source_offset": 0,
                    "source_cursor": "",
                    "next_cursor": "stable-search-id",
                },
                {
                    "platform": "douyin",
                    "discovery_phase": "frontier",
                    "source_offset": 10,
                    "source_cursor": "stable-search-id",
                    "next_cursor": "rotating-log-id",
                },
            ],
            "stop_event": {
                "platform": "douyin",
                "source_page": 3,
                "resume_page": 3,
                "resume_offset": 20,
                "resume_cursor": "rotating-log-id",
                "source_has_more": None,
                "batch_complete": False,
                "discovery_phase": "frontier",
                "stop_reason": "runtime_failed",
                "stop_detail": "search_verify_check",
            },
        },
    )

    assert result["resume_page"] == 3
    assert result["resume_offset"] == 20
    assert result["resume_cursor"] == "stable-search-id"


def test_runner_repairs_rotated_douyin_cursor_from_frozen_summary(tmp_path: Path) -> None:
    params = {
        "platform": "douyin",
        "keyword": "青岛海滨旅游",
        "top_refresh_max_pages": 3,
    }
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "pagination_evidence": {
                    "batches": [
                        {
                            "platform": "douyin",
                            "discovery_phase": "frontier",
                            "source_offset": 0,
                            "source_cursor": "",
                            "next_cursor": "stable-search-id",
                        },
                        {
                            "platform": "douyin",
                            "discovery_phase": "frontier",
                            "source_offset": 10,
                            "source_cursor": "stable-search-id",
                            "next_cursor": "rotating-log-id",
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(tmp_path / "runner-stable.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(conn, params)
        fingerprint = query_fingerprint("douyin", "青岛海滨旅游", params)
        save_checkpoint(
            conn,
            job_id=int(row["id"]),
            platform_key="douyin",
            keyword="青岛海滨旅游",
            query_fingerprint_value=fingerprint,
            resume_page=3,
            resume_offset=20,
            resume_cursor="rotating-log-id",
            source_has_more=None,
            last_batch_complete=False,
            last_stop_reason="runtime_failed",
            last_run_id="failed-run",
        )
        conn.execute(
            """
            UPDATE crawl_discovery_checkpoints
            SET last_summary_path=?
            WHERE job_id=? AND query_fingerprint=?
            """,
            (str(summary_path), int(row["id"]), fingerprint),
        )
        conn.commit()
        args = SimpleNamespace(
            recovery_keyword=None,
            start_page=None,
            resume_summary=None,
        )
        resolved, plan = crawl_runner.resolve_discovery_args(
            conn,
            row,
            args,
            run_id="retry-run",
        )

    assert resolved.start_page == 3
    assert resolved.start_offset == 20
    assert resolved.start_cursor == "stable-search-id"
    assert plan["resume_cursor_corrected_from_summary"] is True


def test_top_refresh_keeps_saved_douyin_frontier(tmp_path: Path) -> None:
    db_path = tmp_path / "refresh.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(conn, {"platform": "douyin", "keyword": "青岛旅游"})
        job_id = int(row["id"])
    fingerprint = query_fingerprint("douyin", "青岛旅游", {})
    args = SimpleNamespace(
        discovery_job_id=job_id,
        discovery_query_fingerprint=fingerprint,
        discovery_run_id="refresh-run",
        discovery_source_exhausted=True,
        no_checkpoint_write=False,
        db=str(db_path),
        keyword="青岛旅游",
        start_page=22,
        start_offset=315,
        start_cursor="saved-frontier",
    )

    result = mediacrawler_crawl.persist_discovery_checkpoint(
        args,
        "douyin",
        {
            "stop_event": {
                "source_page": 2,
                "resume_page": 3,
                "resume_offset": 30,
                "resume_cursor": "refresh-cursor",
                "source_has_more": True,
                "batch_complete": True,
                "discovery_phase": "refresh",
                "stop_reason": "continue",
            }
        },
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        checkpoint = load_checkpoint(
            conn,
            job_id=job_id,
            query_fingerprint_value=fingerprint,
        )
    assert result["refresh_only"] is True
    assert checkpoint is not None
    assert checkpoint["resume_page"] == 22
    assert checkpoint["resume_offset"] == 315
    assert checkpoint["resume_cursor"] == "saved-frontier"
    assert checkpoint["status"] == "exhausted"


def test_saved_empty_first_page_keeps_verified_exhaustion_detail(tmp_path: Path) -> None:
    db_path = tmp_path / "verified-refresh.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(
            conn,
            {"platform": "douyin", "keyword": "青岛崂山旅游攻略"},
        )
        job_id = int(row["id"])
        fingerprint = query_fingerprint("douyin", "青岛崂山旅游攻略", {})
        save_checkpoint(
            conn,
            job_id=job_id,
            platform_key="douyin",
            keyword="青岛崂山旅游攻略",
            query_fingerprint_value=fingerprint,
            resume_page=1,
            resume_offset=0,
            resume_cursor=None,
            source_has_more=False,
            last_batch_complete=True,
            last_stop_reason="source_exhausted",
            last_stop_detail="verified_empty_first_page",
            last_run_id="verified-run",
        )
        conn.commit()
    args = SimpleNamespace(
        discovery_job_id=job_id,
        discovery_query_fingerprint=fingerprint,
        discovery_run_id="refresh-run",
        discovery_source_exhausted=True,
        no_checkpoint_write=False,
        db=str(db_path),
        keyword="青岛崂山旅游攻略",
        start_page=1,
        start_offset=0,
        start_cursor=None,
    )

    result = mediacrawler_crawl.persist_discovery_checkpoint(
        args,
        "douyin",
        {
            "stop_event": {
                "source_page": 1,
                "resume_page": 1,
                "resume_offset": 0,
                "resume_cursor": None,
                "source_has_more": False,
                "batch_complete": True,
                "discovery_phase": "frontier",
                "stop_reason": "source_exhausted",
                "stop_detail": "saved_source_exhausted",
            }
        },
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        checkpoint = load_checkpoint(
            conn,
            job_id=job_id,
            query_fingerprint_value=fingerprint,
        )
    assert checkpoint is not None
    assert checkpoint["last_stop_detail"] == "verified_empty_first_page"
    assert result["last_stop_detail"] == "verified_empty_first_page"


def test_reseeded_douyin_frontier_replaces_exhausted_cursor(tmp_path: Path) -> None:
    db_path = tmp_path / "reseed.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(conn, {"platform": "douyin", "keyword": "青岛旅游"})
        job_id = int(row["id"])
    fingerprint = query_fingerprint("douyin", "青岛旅游", {})
    args = SimpleNamespace(
        discovery_job_id=job_id,
        discovery_query_fingerprint=fingerprint,
        discovery_run_id="reseed-run",
        discovery_source_exhausted=True,
        no_checkpoint_write=False,
        db=str(db_path),
        keyword="青岛旅游",
        start_page=22,
        start_offset=315,
        start_cursor="old-search-id",
    )

    result = mediacrawler_crawl.persist_discovery_checkpoint(
        args,
        "douyin",
        {
            "stop_event": {
                "source_page": 4,
                "resume_page": 5,
                "resume_offset": 60,
                "resume_cursor": "new-search-id-2",
                "source_has_more": True,
                "batch_complete": True,
                "discovery_phase": "frontier",
                "stop_reason": "continue",
                "candidate_identities": ["video-1", "image-1"],
            }
        },
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        checkpoint = load_checkpoint(
            conn,
            job_id=job_id,
            query_fingerprint_value=fingerprint,
        )
    assert result["refresh_only"] is False
    assert checkpoint is not None
    assert checkpoint["resume_page"] == 5
    assert checkpoint["resume_offset"] == 60
    assert checkpoint["resume_cursor"] == "new-search-id-2"
    assert checkpoint["status"] == "active"
    with sqlite3.connect(db_path) as conn:
        seen = conn.execute(
            """
            SELECT platform_post_id
            FROM crawl_discovery_seen_candidates
            WHERE job_id=? AND query_fingerprint=?
            ORDER BY platform_post_id
            """,
            (job_id, fingerprint),
        ).fetchall()
    assert seen == [("image-1",), ("video-1",)]
    assert result["seen_candidate_count"] == 2


def test_runner_auto_resumes_only_matching_query(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text("{}", encoding="utf-8")
    params = {
        "platform": "weibo",
        "keyword": "青岛旅游",
        "top_refresh_max_pages": 3,
        "required_fields_profile": "image_post_with_followers_v1",
        "followers_policy": "required",
    }
    with sqlite3.connect(tmp_path / "runner.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(conn, params)
        fingerprint = query_fingerprint("weibo", "青岛旅游", params)
        save_checkpoint(
            conn,
            job_id=int(row["id"]),
            platform_key="weibo",
            keyword="青岛旅游",
            query_fingerprint_value=fingerprint,
            resume_page=7,
            resume_offset=None,
            resume_cursor=None,
            source_has_more=True,
            last_batch_complete=True,
            last_stop_reason="runtime_failed",
            last_run_id="run-1",
        )
        conn.execute(
            """
            UPDATE crawl_discovery_checkpoints
            SET last_summary_path=?
            WHERE job_id=? AND query_fingerprint=?
            """,
            (str(summary_path), int(row["id"]), fingerprint),
        )
        conn.commit()
        args = SimpleNamespace(
            recovery_keyword=None,
            start_page=None,
            resume_summary=None,
        )
        resolved, plan = crawl_runner.resolve_discovery_args(
            conn,
            row,
            args,
            run_id="run-2",
        )

    assert plan is not None
    assert plan["auto_resume"] is True
    assert resolved.start_page == 7
    assert resolved.resume_summary == str(summary_path.resolve())
    assert resolved.top_refresh_max_pages == 3


def test_runner_retries_unverified_douyin_first_page_checkpoint(tmp_path: Path) -> None:
    params = {
        "platform": "douyin",
        "keyword": "青岛崂山旅游攻略",
        "top_refresh_max_pages": 3,
    }
    with sqlite3.connect(tmp_path / "runner.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(conn, params)
        fingerprint = query_fingerprint("douyin", "青岛崂山旅游攻略", params)
        save_checkpoint(
            conn,
            job_id=int(row["id"]),
            platform_key="douyin",
            keyword="青岛崂山旅游攻略",
            query_fingerprint_value=fingerprint,
            resume_page=1,
            resume_offset=0,
            resume_cursor=None,
            source_has_more=False,
            last_batch_complete=True,
            last_stop_reason="source_exhausted",
            last_run_id="legacy-run",
        )
        conn.commit()
        args = SimpleNamespace(
            recovery_keyword=None,
            start_page=None,
            resume_summary=None,
        )

        retried, retry_plan = crawl_runner.resolve_discovery_args(
            conn,
            row,
            args,
            run_id="retry-run",
        )

        assert retry_plan is not None
        assert retry_plan["unverified_first_page_checkpoint_rejected"] is True
        assert retried.discovery_source_exhausted is False
        assert retried.start_page == 1
        assert retried.start_offset == 0
        assert retried.start_cursor is None

        conn.execute(
            """
            UPDATE crawl_discovery_checkpoints
            SET last_stop_detail='verified_empty_first_page'
            WHERE job_id=? AND query_fingerprint=?
            """,
            (int(row["id"]), fingerprint),
        )
        conn.commit()
        preserved, verified_plan = crawl_runner.resolve_discovery_args(
            conn,
            row,
            args,
            run_id="verified-run",
        )

    assert verified_plan is not None
    assert verified_plan["unverified_first_page_checkpoint_rejected"] is False
    assert preserved.discovery_source_exhausted is True


def test_bilibili_frontier_starts_at_saved_page_and_skips_known_author_lookup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        conn.execute("INSERT INTO web_posts VALUES ('bilibili', 'known', NULL)")
        conn.execute(
            """
            CREATE TABLE crawl_discovery_seen_candidates (
                job_id INTEGER NOT NULL,
                platform_key TEXT NOT NULL,
                query_fingerprint TEXT NOT NULL,
                platform_post_id TEXT NOT NULL,
                first_run_id TEXT NOT NULL,
                last_run_id TEXT NOT NULL,
                first_seen_at TEXT,
                last_seen_at TEXT,
                PRIMARY KEY (job_id, query_fingerprint, platform_post_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE crawl_discovery_candidate_exclusions (
                job_id INTEGER NOT NULL,
                platform_key TEXT NOT NULL,
                query_fingerprint TEXT NOT NULL,
                platform_post_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                authorized_run_id TEXT NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (job_id, query_fingerprint, platform_post_id)
            )
            """
        )
        save_seen_candidates(
            conn,
            job_id=7,
            platform_key="bilibili",
            query_fingerprint_value="bili-fingerprint",
            platform_post_ids=["remembered"],
            run_id="prior-run",
        )
        save_candidate_exclusion(
            conn,
            job_id=7,
            platform_key="bilibili",
            query_fingerprint_value="bili-fingerprint",
            platform_post_id="operator-excluded",
            reason="operator_excluded_after_repeated_detail_failure",
            authorized_run_id="operator-run-1",
        )
    state_path = tmp_path / "state.json"
    mediacrawler_crawl.FrozenExecutionState.create(
        state_path,
        run_id="run-1",
        job_key="bili-test",
        site_key="bilibili",
        job_kind="mediacrawler_search",
        plan={},
        frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    behavior = {"ok": True}
    monkeypatch.setattr(
        mediacrawler_crawl,
        "run_bilibili_behavior_session",
        AsyncMock(return_value=({"cookie_header": ""}, behavior)),
    )
    monkeypatch.setattr(mediacrawler_crawl, "behavior_evidence_valid", lambda value: True)
    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_wbi_keys", lambda value: ("a", "b"))
    requested_pages: list[int] = []

    def fetch_page(keyword, page, **kwargs):
        requested_pages.append(page)
        if page > 4:
            return []
        return [
            {
                "id": "known",
                "title": "known",
                "desc": "body",
                "arcurl": "https://www.bilibili.com/read/cvknown/",
                "image_urls": ["https://example.test/known.jpg"],
                "pubdate": 1_700_000_000,
                "like": 1,
                "reply": 2,
                "view": 3,
                "author": "known-author",
                "mid": "known-author-id",
            },
            {
                "id": "remembered",
                "title": "remembered",
                "desc": "body",
                "arcurl": "https://www.bilibili.com/read/cvremembered/",
                "image_urls": ["https://example.test/remembered.jpg"],
                "pubdate": 1_700_000_000,
                "like": 1,
                "reply": 2,
                "view": 3,
                "author": "remembered-author",
                "mid": "remembered-author-id",
            },
            {
                "id": "operator-excluded",
                "title": "operator-excluded",
                "desc": "body",
                "arcurl": "https://www.bilibili.com/read/cvoperator-excluded/",
                "image_urls": ["https://example.test/operator-excluded.jpg"],
                "pubdate": 1_700_000_000,
                "like": 1,
                "reply": 2,
                "view": 3,
                "author": "excluded-author",
                "mid": "excluded-author-id",
            },
            {
                "id": "new",
                "title": "new",
                "desc": "body",
                "arcurl": "https://www.bilibili.com/read/cvnew/",
                "image_urls": ["https://example.test/new.jpg"],
                "pubdate": 1_700_000_000,
                "like": 1,
                "reply": 2,
                "view": 3,
                "author": "new-author",
                "mid": "new-author-id",
            },
        ]

    follower_ids: list[str] = []

    def fetch_followers(creator_id, cookie_header):
        follower_ids.append(creator_id)
        return 100

    detail_ids: list[str] = []

    def fetch_detail(post_id, cookie_header):
        detail_ids.append(post_id)
        return (
            {
                "title": "new detail title",
                    "content": "青岛完整正文",
                "image_urls": ["https://example.test/new-detail.jpg"],
                "opus": {"content": {"paragraphs": []}},
            },
            1,
            0.0,
        )

    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_article_page", fetch_page)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_article_detail_with_retry",
        fetch_detail,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_follower_count",
        fetch_followers,
    )
    monkeypatch.setattr(mediacrawler_crawl.time, "sleep", lambda value: None)
    args = SimpleNamespace(
        keyword="青岛旅游",
        db=str(db_path),
        start_page=4,
        top_refresh_max_pages=0,
        discovery_source_exhausted=False,
        discovery_job_id=7,
        discovery_query_fingerprint="bili-fingerprint",
        resume_identities_path=None,
    )

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is True
    assert requested_pages == [4, 5]
    assert detail_ids == ["new"]
    assert follower_ids == ["new-author-id"]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["resume_page"] == 5
    assert stopped["details"]["stagnation_basis"] == "candidate_identity"
    assert stopped["details"]["candidate_identities"] == ["new"]
    with sqlite3.connect(db_path) as conn:
        assert load_seen_candidates(
            conn,
            job_id=7,
            platform_key="bilibili",
            query_fingerprint_value="bili-fingerprint",
        ) == {"remembered"}


def test_checkpoint_progress_resets_failure_counter(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "attempt.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(conn, {"platform": "weibo", "keyword": "青岛旅游"})
        conn.execute(
            "UPDATE crawl_jobs SET consecutive_failures=1, max_attempts=2 WHERE id=?",
            (row["id"],),
        )
        conn.execute(
            """
            INSERT INTO crawl_attempts (
                job_id, run_id, attempt_no, status, started_at
            ) VALUES (?, 'run-1', 1, 'running', datetime('now'))
            """,
            (row["id"],),
        )
        attempt_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        row = conn.execute("SELECT * FROM crawl_jobs WHERE id=?", (row["id"],)).fetchone()
        crawl_runner.finalize_attempt(
            conn,
            row=row,
            attempt_id=attempt_id,
            completed=subprocess.CompletedProcess([], 2, "", ""),
            classification={
                "status": "retry_wait",
                "failure_type": "source_exhaustion_not_persisted",
                "retryable": True,
                "wait_seconds": 60,
                "checkpoint_progress": True,
            },
            artifact_dir="",
            capture_meta_paths=[],
            import_result={},
            config={"defaults": {"schedule_jitter_ratio": 0}},
        )
        updated = conn.execute(
            "SELECT status, consecutive_failures FROM crawl_jobs WHERE id=?",
            (row["id"],),
        ).fetchone()

    assert updated["status"] == "retry_wait"
    assert updated["consecutive_failures"] == 0
