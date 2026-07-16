from __future__ import annotations

import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

from trippostcollect.db.bootstrap import bootstrap_connection


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

douban_group_crawl = import_module("douban_group_crawl")
crawl_runner = import_module("crawl_runner")


def test_extract_topic_urls_normalizes_and_deduplicates() -> None:
    rendered_html = """
    <a href="/group/topic/123/?from=search">甲</a>
    <a href="https://www.douban.com/group/topic/123/">重复</a>
    <a href="https://www.douban.com/group/topic/456/#reply">乙</a>
    <a href="https://www.douban.com/people/author/">非话题</a>
    """

    assert douban_group_crawl.extract_topic_urls(
        rendered_html,
        "https://www.douban.com/group/search?cat=1013&q=青岛旅游",
    ) == [
        "https://www.douban.com/group/topic/123/",
        "https://www.douban.com/group/topic/456/",
    ]


def test_discovery_page_url_replaces_start_offset() -> None:
    assert douban_group_crawl.discovery_page_url(
        "https://www.douban.com/group/search?cat=1013&q=青岛旅游&start=0",
        100,
    ).endswith("cat=1013&q=%E9%9D%92%E5%B2%9B%E6%97%85%E6%B8%B8&start=100")


def test_runner_builds_formal_douban_search_command(tmp_path: Path) -> None:
    row = {
        "job_key": "douban_group_qingdao_search",
        "site_key": "douban_group",
        "target_url": "https://www.douban.com/group/search?cat=1013&q=青岛旅游",
        "job_kind": "douban_group_search",
        "params_json": json.dumps(
            {
                "keyword": "青岛旅游",
                "candidate_hard_limit": 300,
                "target_new_posts": 50,
                "max_stagnant_batches": 3,
                "followers_policy": "conditional_enrichment",
                "discovery_page_size": 50,
                "max_image_save": 3,
                "max_scrolls": 8,
            }
        ),
        "behavior_profile_json": json.dumps({"name": "conservative"}),
    }
    args = SimpleNamespace(db=str(tmp_path / "test.sqlite"), headless=False, no_throttle=False)

    command = crawl_runner.build_command(row, args)

    assert command[1].endswith("scripts/douban_group_crawl.py")
    assert command[command.index("--candidate-hard-limit") + 1] == "300"
    assert command[command.index("--target-new-posts") + 1] == "50"
    assert command[command.index("--keyword") + 1] == "青岛旅游"


def test_scheduler_migrates_existing_jobs_for_douban_search_kind() -> None:
    current_schema = (ROOT / "db" / "crawl_scheduler.sql").read_text(encoding="utf-8")
    legacy_schema = current_schema.replace(
        "'mediacrawler_search', 'ctf_resource_crawl', 'douban_group_search'",
        "'mediacrawler_search', 'ctf_resource_crawl'",
    )
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(legacy_schema)
        conn.execute(
            """
            INSERT INTO crawl_jobs(job_key, site_key, target_url, job_kind, next_run_at)
            VALUES ('legacy-page', 'douban_group', 'https://example.test/', 'ctf_resource_crawl', datetime('now'))
            """
        )
        job_id = int(conn.execute("SELECT id FROM crawl_jobs WHERE job_key='legacy-page'").fetchone()[0])
        conn.execute(
            """
            INSERT INTO crawl_attempts(job_id, run_id, attempt_no, status, started_at)
            VALUES (?, 'legacy-run', 1, 'completed', datetime('now'))
            """,
            (job_id,),
        )

        bootstrap_connection(conn, sync_content=False, sync_scheduler=True, sync_jobs=False)

        assert conn.execute("SELECT job_kind FROM crawl_jobs WHERE id=?", (job_id,)).fetchone()[0] == "ctf_resource_crawl"
        assert conn.execute("SELECT job_id FROM crawl_attempts WHERE run_id='legacy-run'").fetchone()[0] == job_id
        conn.execute(
            """
            INSERT INTO crawl_jobs(job_key, site_key, target_url, job_kind, next_run_at)
            VALUES ('douban-batch', 'douban_group', 'https://example.test/search', 'douban_group_search', datetime('now'))
            """
        )
        douban_job_id = int(conn.execute("SELECT id FROM crawl_jobs WHERE job_key='douban-batch'").fetchone()[0])
        conn.execute(
            """
            INSERT INTO crawl_attempts(job_id, run_id, attempt_no, status, started_at)
            VALUES (?, 'douban-run', 1, 'running', datetime('now'))
            """,
            (douban_job_id,),
        )
