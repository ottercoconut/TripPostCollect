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

crawl_runner = import_module("crawl_runner")


def command_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_enabled_bilibili_job_routes_to_formal_detail_executor(tmp_path: Path) -> None:
    config_path = ROOT / "config" / "crawl_targets.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    jobs = [
        item
        for item in config["jobs"]
        if item.get("site_key") == "bilibili" and item.get("enabled") is True
    ]

    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_kind"] == "mediacrawler_search"
    assert job["params"]["platform"] == "bilibili"
    assert job["params"]["required_fields_profile"] == "image_post_with_followers_v1"
    assert job["params"]["followers_policy"] == "required"
    assert job["behavior_profile"]["name"] == "social_high_risk"

    with sqlite3.connect(tmp_path / "scheduler.sqlite") as connection:
        connection.row_factory = sqlite3.Row
        bootstrap_connection(connection, config=config)
        row = connection.execute(
            "SELECT * FROM crawl_jobs WHERE job_key=?",
            (job["job_key"],),
        ).fetchone()
        assert row is not None
        command = crawl_runner.build_command(
            row,
            SimpleNamespace(
                recovery_keyword=None,
                completion_mode="target-new-posts",
                db=str(tmp_path / "posts.sqlite"),
                start_page=None,
                resume_summary=None,
                start_offset=None,
                start_cursor=None,
                discovery_job_id=None,
                discovery_query_fingerprint="",
                discovery_run_id="",
                top_refresh_max_pages=0,
                discovery_source_exhausted=False,
                headful=False,
                headless=False,
                no_import=False,
                no_throttle=False,
            ),
        )

    assert Path(command[1]).name == "mediacrawler_crawl.py"
    assert command_value(command, "--platforms") == "bilibili"
    assert command_value(command, "--keyword") == "青岛崂山旅游攻略"
    assert command_value(command, "--required-fields-profile") == (
        "image_post_with_followers_v1"
    )
    assert command_value(command, "--completion-mode") == "target-new-posts"
    assert "--no-import" not in command
