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
    load_checkpoint,
    query_fingerprint,
    save_checkpoint,
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


def test_query_fingerprint_ignores_run_budget_but_tracks_source_options() -> None:
    base = {
        "platform": "weibo",
        "keyword": "青岛旅游",
        "candidate_hard_limit": 300,
        "target_new_posts": 50,
        "search_type": "default",
    }
    changed_budget = {**base, "candidate_hard_limit": 600, "target_new_posts": 80}
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
            last_stop_reason="candidate_hard_limit_reached",
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
                "stop_reason": "candidate_hard_limit_reached",
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
                "stop_reason": "target_new_met",
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


def test_runner_auto_resumes_only_matching_query(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text("{}", encoding="utf-8")
    params = {
        "platform": "weibo",
        "keyword": "青岛旅游",
        "candidate_hard_limit": 300,
        "target_new_posts": 50,
        "max_stagnant_batches": 3,
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
            last_stop_reason="candidate_hard_limit_reached",
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

    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_article_page", fetch_page)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_follower_count",
        fetch_followers,
    )
    monkeypatch.setattr(mediacrawler_crawl.time, "sleep", lambda value: None)
    args = SimpleNamespace(
        keyword="青岛旅游",
        candidate_hard_limit=10,
        target_new_posts=1,
        source_candidate_hard_limit=10,
        source_target_new_posts=1,
        max_stagnant_batches=3,
        db=str(db_path),
        start_page=4,
        top_refresh_max_pages=0,
        discovery_source_exhausted=False,
        resume_identities_path=None,
    )

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is True
    assert requested_pages == [4]
    assert follower_ids == ["new-author-id"]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["resume_page"] == 5


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
                "failure_type": "target_not_met",
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
