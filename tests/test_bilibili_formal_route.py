"""TripPostCollect tests for bilibili formal route."""

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
            ),
        )

    assert Path(command[1]).name == "mediacrawler_crawl.py"
    assert command_value(command, "--platforms") == "bilibili"
    assert command_value(command, "--keyword") == "青岛崂山旅游攻略"
    assert command_value(command, "--required-fields-profile") == (
        "image_post_with_followers_v1"
    )
    assert "--completion-mode" not in command
    assert "--target-new-posts" not in command
    assert "--candidate-hard-limit" not in command
    assert "--download-images" in command
    assert command_value(command, "--media-root") == str(
        crawl_runner.LOCAL_MEDIA_ROOT.resolve()
    )
    assert "--get-media" not in command
    assert "--no-import" not in command


def test_runner_has_no_no_throttle_option(monkeypatch) -> None:
    """#112：结构化 child 不认识 --no-throttle，runner 不再提供也不再转发这个参数。"""

    import pytest

    monkeypatch.setattr(sys, "argv", ["crawl_runner.py", "--no-throttle"])
    with pytest.raises(SystemExit) as caught:
        crawl_runner.parse_args()
    assert caught.value.code == 2


def test_every_runner_built_flag_is_accepted_by_the_structured_child(
    tmp_path: Path, monkeypatch,
) -> None:
    """runner 为每个通用平台 job 生成的参数都能被 child 的 argparse 原样解析（含 headful/no-import 分支）。"""

    from trippostcollect.application import inputs

    monkeypatch.setattr(sys, "argv", ["crawl_runner.py", "--headful", "--no-import"])
    runner_args = crawl_runner.parse_args()
    runner_args.db = str(tmp_path / "posts.sqlite")
    config = json.loads((ROOT / "config" / "crawl_targets.json").read_text(encoding="utf-8"))
    with sqlite3.connect(tmp_path / "scheduler.sqlite") as connection:
        connection.row_factory = sqlite3.Row
        bootstrap_connection(connection, config=config)
        rows = connection.execute(
            "SELECT * FROM crawl_jobs WHERE job_kind='mediacrawler_search' ORDER BY job_key"
        ).fetchall()
    assert {row["site_key"] for row in rows} >= {"bilibili", "weibo", "douyin", "zhihu"}
    for row in rows:
        command = crawl_runner.build_command(row, runner_args)
        monkeypatch.setattr(sys, "argv", ["mediacrawler_crawl.py", *command[2:]])
        parsed = inputs.parse_args()
        assert parsed.platforms == [row["site_key"]], row["job_key"]
