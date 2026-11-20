"""#217 微博显式时间窗：解析、指纹、runner、child、搜索地址与页上限。全部离线。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from trippostcollect.platforms.weibo.search_page import (
    FILTER_SIGNATURE,
    classify_search_page,
    search_url,
)
from trippostcollect.platforms.weibo.time_window import (
    TIME_WINDOW_ARGS_INCOMPLETE,
    TIME_WINDOW_INVALID,
    TIME_WINDOW_UNSUPPORTED,
    TimeWindowError,
    parse_time_window,
    search_filter_signature,
)
from trippostcollect.scheduler.discovery import canonical_json, query_fingerprint, source_query_options


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

crawl_runner = __import__("crawl_runner")


# 改动前、没有 time_window 的默认微博 job（来源参数为空）的指纹。算法未把 time_window 排除在来源参数外。
DEFAULT_WEIBO_CANONICAL = '{"keyword":"青岛崂山旅游攻略","platform_key":"weibo","source_options":{}}'
DEFAULT_WEIBO_FINGERPRINT = "8f4f8e09b17de1f3ee799da0eeae0105aaf715bc657aa0636f493316187d0f2e"
JOB = "weibo-window"


def window(start: str = "2025-07-01", end: str = "2025-07-08"):
    return parse_time_window({"start": start, "end": end}, job_key=JOB)


def card(mid: str = "11") -> dict:
    return {"mid": mid, "uid": "88", "mblogid": "Abc"}


def dom(**overrides) -> dict:
    payload = {
        "cards": [card()],
        "has_pager": True,
        "current_text": "1",
        "page_info": "共2页/20条",
        "next_disabled": False,
        "max_page": 2,
        "no_results": False,
    }
    payload.update(overrides)
    return payload


def test_date_and_hour_forms_normalize_without_padding() -> None:
    parsed = window()
    assert parsed.start == "2025-07-01-0"
    assert parsed.end == "2025-07-08-0"
    assert parsed.timescope == "custom:2025-07-01-0:2025-07-08-0"
    padded = window("2025-07-01-09", "2025-07-01-10")
    assert (padded.start, padded.end) == ("2025-07-01-9", "2025-07-01-10")
    bare = window("2025-07-01-9", "2025-07-02")
    assert (bare.start, bare.end) == ("2025-07-01-9", "2025-07-02-0")
    zero = parse_time_window({"start": "2025-07-01-00", "end": "2025-07-01-1"}, job_key=JOB)
    assert zero.start == "2025-07-01-0"


def test_equal_instants_and_reversed_bounds_are_rejected() -> None:
    with pytest.raises(TimeWindowError) as same:
        window("2025-07-01", "2025-07-01-0")
    assert same.value.code == TIME_WINDOW_INVALID
    assert f"job={JOB}" in str(same.value)
    with pytest.raises(TimeWindowError) as later:
        window("2025-07-08", "2025-07-01")
    assert later.value.code == TIME_WINDOW_INVALID


@pytest.mark.parametrize(
    "payload",
    [
        {"start": "2025-07-01"},
        {"end": "2025-07-08"},
        {"start": "2025-07-01", "end": "2025-07-08", "tz": "Asia/Shanghai"},
        {"start": 20250701, "end": "2025-07-08"},
        {"start": "2025-07-01", "end": None},
        {"start": "2025-02-29", "end": "2025-03-01"},
        {"start": "2025-13-01", "end": "2025-13-02"},
        {"start": "2025-07-01-24", "end": "2025-07-02"},
        {"start": "2025/07/01", "end": "2025/07/08"},
        {"start": "2025-7-01", "end": "2025-07-08"},
        "2025-07-01",
        ["2025-07-01", "2025-07-08"],
        None,
    ],
)
def test_malformed_time_window_is_a_config_error(payload) -> None:
    with pytest.raises(TimeWindowError) as raised:
        parse_time_window(payload, job_key=JOB)
    assert raised.value.code == TIME_WINDOW_INVALID
    assert f"job={JOB}" in str(raised.value)


def test_default_job_fingerprint_is_unchanged_and_windows_differ() -> None:
    params = {
        "platform": "weibo",
        "keyword": "青岛崂山旅游攻略",
        "top_refresh_max_pages": 3,
        "required_fields_profile": "image_post_with_followers_v1",
        "followers_policy": "required",
        "login_type": "cookie",
        "headless": False,
    }
    payload = {
        "platform_key": "weibo",
        "keyword": params["keyword"],
        "source_options": source_query_options(params),
    }
    assert canonical_json(payload) == DEFAULT_WEIBO_CANONICAL
    assert query_fingerprint("weibo", params["keyword"], params) == DEFAULT_WEIBO_FINGERPRINT
    first = {**params, "time_window": {"start": "2025-07-01", "end": "2025-07-08"}}
    second = {**params, "time_window": {"start": "2025-07-08", "end": "2025-07-15"}}
    assert source_query_options(first)["time_window"] == {"start": "2025-07-01", "end": "2025-07-08"}
    assert query_fingerprint("weibo", first["keyword"], first) != query_fingerprint("weibo", second["keyword"], second)
    assert query_fingerprint("weibo", first["keyword"], first) != DEFAULT_WEIBO_FINGERPRINT


def test_search_url_and_filter_signature() -> None:
    parsed = window()
    assert search_url("青岛旅游") == (
        "https://s.weibo.com/weibo?q=%E9%9D%92%E5%B2%9B%E6%97%85%E6%B8%B8&nodup=1"
    )
    assert search_url("青岛旅游", parsed) == (
        "https://s.weibo.com/weibo?q=%E9%9D%92%E5%B2%9B%E6%97%85%E6%B8%B8"
        "&typeall=1&suball=1&timescope=custom:2025-07-01-0:2025-07-08-0&nodup=1"
    )
    assert search_filter_signature(None) == FILTER_SIGNATURE == "nodup=1"
    assert search_filter_signature(parsed) == "nodup=1;timescope=custom:2025-07-01-0:2025-07-08-0"
    from trippostcollect.application.reporting import _url_contains_keyword

    assert _url_contains_keyword(search_url("青岛旅游"), "青岛旅游") is True
    assert _url_contains_keyword(search_url("青岛旅游", parsed), "青岛旅游") is True


def test_page_cap_is_fifty_or_a_disabled_fiftieth_page() -> None:
    capped = classify_search_page(
        dom(current_text="2", next_disabled=True, max_page=50, page_info="共50页/500条"),
        page_number=2,
    )
    assert capped.page_cap_reached is True
    stopped = classify_search_page(
        dom(current_text="50", next_disabled=True, max_page=None, page_info=""),
        page_number=50,
    )
    assert stopped.stop_detail == "next_disabled"
    assert stopped.page_cap_reached is True
    short = classify_search_page(
        dom(current_text="3", next_disabled=True, max_page=3, page_info=""),
        page_number=3,
    )
    assert short.page_cap_reached is False


def formal_params(**extra) -> dict:
    params = {
        "platform": "weibo",
        "keyword": "青岛旅游",
        "required_fields_profile": "image_post_with_followers_v1",
        "followers_policy": "required",
        "login_type": "cookie",
    }
    params.update(extra)
    return params


def job_row(platform: str, params: dict, *, job_key: str = JOB) -> dict:
    return {
        "id": 7,
        "job_key": job_key,
        "site_key": platform,
        "target_url": "https://s.weibo.com/weibo?q=x&nodup=1",
        "job_kind": "mediacrawler_search",
        "behavior_profile_json": json.dumps({"name": "social_high_risk"}),
        "params_json": json.dumps(params),
    }


def runner_namespace(**extra) -> argparse.Namespace:
    values = dict(
        recovery_keyword=None,
        db="posts.sqlite",
        start_page=None,
        resume_summary=None,
        headful=False,
        headless=False,
        no_import=False,
    )
    values.update(extra)
    return argparse.Namespace(**values)


def test_runner_passes_normalized_bounds_only_for_a_window_job() -> None:
    command = crawl_runner.build_command(
        job_row("weibo", formal_params(time_window={"start": "2025-07-01", "end": "2025-07-08"})),
        runner_namespace(),
    )
    assert command[command.index("--time-window-start") + 1] == "2025-07-01-0"
    assert command[command.index("--time-window-end") + 1] == "2025-07-08-0"
    assert json.loads(command[command.index("--time-window-source") + 1]) == {
        "start": "2025-07-01",
        "end": "2025-07-08",
    }
    assert command[command.index("--time-window-job-key") + 1] == JOB
    assert "--start-page" not in command
    default = crawl_runner.build_command(job_row("weibo", formal_params(), job_key="weibo-default"), runner_namespace())
    assert "--time-window-start" not in default
    assert "--time-window-end" not in default


def test_runner_rejects_time_window_on_an_unsupported_platform(tmp_path: Path) -> None:
    params = formal_params(platform="zhihu", time_window={"start": "2025-07-01", "end": "2025-07-08"})
    with pytest.raises(TimeWindowError) as dispatch:
        crawl_runner.build_command(job_row("zhihu", params, job_key="zhihu-window"), runner_namespace())
    assert dispatch.value.code == TIME_WINDOW_UNSUPPORTED
    assert "job=zhihu-window" in str(dispatch.value)
    config = {
        "schema_version": 2,
        "jobs": [{
            "job_key": "zhihu-window",
            "site_key": "zhihu",
            "job_kind": "mediacrawler_search",
            "params": params,
        }],
    }
    with pytest.raises(TimeWindowError) as synced:
        crawl_runner.validate_crawl_config(config, tmp_path / "targets.json")
    assert synced.value.code == TIME_WINDOW_UNSUPPORTED
    assert "job=zhihu-window" in str(synced.value)


def test_full_scan_still_rejects_start_page_for_a_window_job(tmp_path: Path) -> None:
    params = formal_params(time_window={"start": "2025-07-01", "end": "2025-07-08"})
    with sqlite3.connect(tmp_path / "posts.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        from trippostcollect.db.bootstrap import bootstrap_connection

        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        conn.execute(
            """
            INSERT INTO crawl_jobs (
                job_key, site_key, target_url, job_kind, next_run_at, params_json, behavior_profile_json
            ) VALUES (?, 'weibo', '', 'mediacrawler_search', datetime('now'), ?, ?)
            """,
            (JOB, json.dumps(params), json.dumps({"name": "social_high_risk"})),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM crawl_jobs").fetchone()
        with pytest.raises(ValueError, match="weibo_full_scan_rejects_start_page"):
            crawl_runner.resolve_discovery_args(
                conn, row, runner_namespace(start_page=2), run_id="window-run",
            )
        _args, plan = crawl_runner.resolve_discovery_args(
            conn, row, runner_namespace(), run_id="window-run",
        )
    assert plan["full_scan"] is True
    assert plan["time_window"]["timescope"] == "custom:2025-07-01-0:2025-07-08-0"
    assert plan["time_window"]["start"] == "2025-07-01"


def test_dry_run_plan_shows_the_time_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "targets.json"
    config_path.write_text(json.dumps({
        "schema_version": 2,
        "defaults": {"schedule_jitter_ratio": 0},
        "jobs": [{
            "job_key": JOB,
            "site_key": "weibo",
            "target_url": (
                "https://s.weibo.com/weibo?q=%E9%9D%92%E5%B2%9B%E6%97%85%E6%B8%B8"
                "&typeall=1&suball=1&timescope=custom:2025-07-01-0:2025-07-08-0&nodup=1"
            ),
            "job_kind": "mediacrawler_search",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00+00:00",
            "params": formal_params(time_window={"start": "2025-07-01", "end": "2025-07-08"}),
            "behavior_profile": {"name": "social_high_risk"},
        }],
    }, ensure_ascii=False), encoding="utf-8")
    # 先同步一次，再 dry-run 不改配置。同步路径也会校验时间窗。
    monkeypatch.setattr(sys, "argv", [
        "crawl_runner.py", "--db", str(tmp_path / "posts.sqlite"),
        "--config", str(config_path), "--sync-only",
    ])
    assert crawl_runner.main() == 0
    monkeypatch.setattr(sys, "argv", [
        "crawl_runner.py",
        "--db", str(tmp_path / "posts.sqlite"),
        "--config", str(config_path),
        "--run-root", str(tmp_path / "runs"),
        "--execution-state-root", str(tmp_path / "states"),
        "--job-key", JOB,
        "--dry-run",
        "--no-sync-config",
    ])
    assert crawl_runner.main() == 0
    summary = json.loads(next((tmp_path / "runs").rglob("run_summary.json")).read_text(encoding="utf-8"))
    record = summary["records"][0]
    assert record["time_window"]["start"] == "2025-07-01"
    assert record["time_window"]["start_normalized"] == "2025-07-01-0"
    assert record["time_window"]["timescope"] == "custom:2025-07-01-0:2025-07-08-0"
    assert record["command"][record["command"].index("--time-window-start") + 1] == "2025-07-01-0"
    state = json.loads(next((tmp_path / "states").rglob("*.json")).read_text(encoding="utf-8"))
    assert state["plan"]["discovery"]["time_window"]["end_normalized"] == "2025-07-08-0"


def test_child_rejects_a_lone_bound_and_other_platforms() -> None:
    from trippostcollect.application.collection import accept_time_window_args
    from trippostcollect.application.inputs import parse_args

    lone = parse_args([
        "--platforms", "weibo",
        "--time-window-start", "2025-07-01-0",
        "--time-window-job-key", JOB,
    ])
    with pytest.raises(SystemExit) as incomplete:
        accept_time_window_args(lone, ["weibo"])
    assert TIME_WINDOW_ARGS_INCOMPLETE in str(incomplete.value)
    assert f"job={JOB}" in str(incomplete.value)
    foreign = parse_args([
        "--platforms", "zhihu",
        "--time-window-start", "2025-07-01-0",
        "--time-window-end", "2025-07-08-0",
        "--time-window-job-key", "zhihu-window",
    ])
    with pytest.raises(SystemExit) as rejected:
        accept_time_window_args(foreign, ["zhihu"])
    assert TIME_WINDOW_UNSUPPORTED in str(rejected.value)
    assert "job=zhihu-window" in str(rejected.value)
    accepted = parse_args([
        "--platforms", "weibo",
        "--time-window-start", "2025-07-01-0",
        "--time-window-end", "2025-07-08-0",
        "--time-window-source", '{"start":"2025-07-01","end":"2025-07-08"}',
        "--time-window-job-key", JOB,
    ])
    audit = accept_time_window_args(accepted, ["weibo"])
    assert audit["start"] == "2025-07-01"
    assert audit["start_normalized"] == "2025-07-01-0"
    assert audit["timescope"] == "custom:2025-07-01-0:2025-07-08-0"


def test_child_forwards_the_raw_window_to_the_worker(monkeypatch, tmp_path: Path) -> None:
    from importlib import import_module

    from trippostcollect.application import collection, reporting
    from support.worker_scenarios import parent_args

    crawl = import_module("mediacrawler_crawl")

    captured: dict = {}

    def fake_run_command(_cmd, _cwd, _timeout, _log_dir, **kwargs):
        captured["env"] = dict(kwargs.get("extra_env") or {})
        return {"returncode": 0, "timed_out": False, "stdout_tail": "", "stderr_tail": ""}

    monkeypatch.setattr(collection, "run_command", fake_run_command)
    monkeypatch.setattr(collection, "load_behavior_evidence", lambda *_args: {"status": "completed"})
    monkeypatch.setattr(collection, "behavior_evidence_valid", lambda *_args: True)
    monkeypatch.setattr(reporting, "summarize_output", lambda *_args: {
        "parse_errors": 0,
        "content_records": 0,
        "non_video_content_records": 0,
        "video_like_records": 0,
    })
    args = parent_args("weibo_search", tmp_path)
    args.time_window_audit = {
        "start": "2025-07-01",
        "end": "2025-07-08",
        "start_normalized": "2025-07-01-0",
        "end_normalized": "2025-07-08-0",
        "timescope": "custom:2025-07-01-0:2025-07-08-0",
    }
    args.time_window_job_key = JOB
    crawl._run_platform_without_policy("weibo", args, tmp_path)
    assert json.loads(captured["env"]["TRIPPOSTCOLLECT_TIME_WINDOW"]) == {
        "start": "2025-07-01",
        "end": "2025-07-08",
    }
    assert captured["env"]["TRIPPOSTCOLLECT_TIME_WINDOW_JOB_KEY"] == JOB
