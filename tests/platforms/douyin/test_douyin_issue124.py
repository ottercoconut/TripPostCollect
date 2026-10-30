"""#124 抖音每轮从首页全量翻到 has_more=false，搜索只经浏览器截获。

全部离线：假 page 只记录滚轮，不启动浏览器；任何 HTTP 方法被调用即失败。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from support.douyin import make_client as DouYinClient
from trippostcollect.platforms.douyin.models import SearchResponseError


KEYWORD = "青岛崂山旅游攻略"
MISSING = "browser_search_response_missing"


class Mouse:
    def __init__(self, page):
        self.page = page

    async def move(self, *args, **kwargs):
        pass

    async def wheel(self, *args):
        self.page.wheels += 1
        if self.page.on_wheel is not None:
            self.page.on_wheel(self.page.wheels)


class ScrollPage:
    """只实现截获监听与可见滚轮；滚轮次数用于证明“只差一屏”的预算。"""

    viewport_size = {"width": 1920, "height": 1080}

    def __init__(self, on_wheel=None):
        self.wheels = 0
        self.on_wheel = on_wheel
        self.mouse = Mouse(self)

    def on(self, event, callback):
        assert event == "response"

    async def wait_for_timeout(self, timeout):
        pass


def forbid_http(client):
    """搜索不得经浏览器外 HTTP：get/post/request 与 httpx 工厂任何一个被调用都直接失败。"""

    async def forbidden(*args, **kwargs):
        raise AssertionError("search must not issue a direct HTTP request")

    def forbidden_factory(**kwargs):
        raise AssertionError("search must not build an HTTP client")

    client.get = forbidden
    client.post = forbidden
    client.request = forbidden
    client.ports = replace(client.ports, make_async_client=forbidden_factory)
    return client


def observed(client, offset, search_id, payload):
    client._observed_search_responses.append(
        {"keyword": KEYWORD, "offset": offset, "search_id": search_id, "payload": payload}
    )


def page_payload(aweme_id, *, has_more=1, logid="next-log-id"):
    return {
        "status_code": 0,
        "data": [{"aweme_info": {"aweme_id": aweme_id}}],
        "has_more": has_more,
        "extra": {"logid": logid},
    }


@pytest.mark.asyncio
async def test_missing_followup_offset_after_scroll_budget_is_runtime_failure_without_http():
    page = ScrollPage()
    client = forbid_http(DouYinClient(headers={"User-Agent": "t124"}, playwright_page=page, cookie_dict={}))
    # 缓存里只有上一页：目标页仍须由页面滚动触发，不能拿其他 offset 顶替。
    observed(client, 10, "stable-search-id", page_payload("10"))

    with pytest.raises(SearchResponseError) as raised:
        await client.search_info_by_keyword(keyword=KEYWORD, offset=20, search_id="stable-search-id")

    assert raised.value.reason == MISSING
    assert page.wheels == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("with_page", [True, False])
async def test_missing_first_page_capture_is_runtime_failure_without_http(with_page):
    page = ScrollPage() if with_page else None
    client = forbid_http(DouYinClient(headers={"User-Agent": "t124"}, playwright_page=page, cookie_dict={}))

    with pytest.raises(SearchResponseError) as raised:
        await client.search_info_by_keyword(keyword=KEYWORD, offset=0, search_id="")

    assert raised.value.reason == MISSING
    # 首页只能来自进入搜索页时截获的 /stream/ 响应，滚动不会产生它。
    assert page is None or page.wheels == 0


@pytest.mark.asyncio
async def test_captured_target_offset_is_returned_and_verify_check_still_rejected():
    page = ScrollPage()
    client = forbid_http(DouYinClient(headers={"User-Agent": "t124"}, playwright_page=page, cookie_dict={}))
    observed(client, 0, "", page_payload("1", logid="stable-search-id"))
    observed(client, 10, "stable-search-id", {
        "status_code": 0, "data": [], "has_more": 0,
        "search_nil_info": {"search_nil_type": "verify_check"},
    })

    first = await client.search_info_by_keyword(keyword=KEYWORD, offset=0, search_id="")
    assert first["data"][0]["aweme_info"]["aweme_id"] == "1"
    with pytest.raises(SearchResponseError) as raised:
        await client.search_info_by_keyword(keyword=KEYWORD, offset=10, search_id="stable-search-id")
    assert raised.value.reason == "search_verify_check"
    assert page.wheels == 0


@pytest.mark.asyncio
async def test_followup_page_reached_by_one_scroll_is_returned_without_http():
    def emit(wheels):
        if wheels == 1:
            observed(client, 10, "stable-search-id", page_payload("11", has_more=0))

    page = ScrollPage(on_wheel=emit)
    client = forbid_http(DouYinClient(headers={"User-Agent": "t124"}, playwright_page=page, cookie_dict={}))

    response = await client.search_info_by_keyword(keyword=KEYWORD, offset=10, search_id="stable-search-id")

    assert response["data"][0]["aweme_info"]["aweme_id"] == "11"
    assert page.wheels == 1


def test_client_has_no_direct_search_or_first_page_rebuild():
    client = DouYinClient(headers={"User-Agent": "t124"}, playwright_page=None, cookie_dict={})
    assert not hasattr(client, "_build_visible_first_page_fallback")


# ----------------------------- worker search() 全量扫描 -----------------------------


import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from support.douyin import config, make_crawler as DouYinCrawler, refresh_settings  # noqa: E402
from trippostcollect.platforms.douyin import core as douyin_core  # noqa: E402


def image_aweme(aweme_id):
    return {
        "aweme_id": aweme_id, "aweme_type": 68, "desc": "青岛崂山图文", "create_time": 1_700_000_000,
        "author": {"uid": "author", "nickname": "name", "sec_uid": f"sec-{aweme_id}"},
        "statistics": {"digg_count": 1, "collect_count": 2, "comment_count": 3, "share_count": 4},
        "images": [{"uri": f"uri-{aweme_id}", "url_list": [f"https://p3.test/{aweme_id}.jpeg"]}],
    }


class PagedSearch:
    """按 offset 返回一页一条图文；last_offset 页 has_more=0。missing_offset 抛截获缺失。"""

    def __init__(self, *, last_offset, missing_offset=None):
        self.last_offset = last_offset
        self.missing_offset = missing_offset
        self.calls = []

    async def search_info_by_keyword(self, **kwargs):
        self.calls.append(kwargs)
        offset = kwargs["offset"]
        if offset == self.missing_offset:
            raise SearchResponseError(MISSING)
        return {
            "status_code": 0,
            "data": [{"aweme_info": image_aweme(str(100 + offset))}],
            "has_more": 0 if offset == self.last_offset else 1,
            # 首页给稳定 search ID，后续页每页轮换 logid：链上始终沿用首页 ID。
            "extra": {"logid": "stable-search-id" if offset == 0 else f"rotating-{offset}"},
        }

    async def get_user_info(self, sec_uid):
        return {"status_code": 0, "user": {"follower_count": 7}}


def prepare_scan(monkeypatch, tmp_path, *, legacy_inputs):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH", "-1")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    if legacy_inputs:
        # 旧三元组续跑的全部输入：本轮都不得影响起点与停止判定。
        for name, value in {
            "TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET": "285",
            "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR": "stale-prior-session-id",
            "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES": "3",
            "TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED": "1",
        }.items():
            monkeypatch.setenv(name, value)
    else:
        for name in (
            "TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR",
            "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED",
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "START_PAGE", 29 if legacy_inputs else 1)
    monkeypatch.setattr(config, "KEYWORDS", KEYWORD)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(douyin_core.asyncio, "sleep", AsyncMock())
    crawler = DouYinCrawler()
    refresh_settings(crawler)
    crawler.get_aweme_images = AsyncMock(return_value=None)
    crawler.update_douyin_aweme = AsyncMock(return_value=None)
    return crawler, state_path


def events_of(state_path, kind):
    return [event["details"] for event in json.loads(state_path.read_text(encoding="utf-8"))["events"]
            if event["type"] == kind]


def event_types(state_path):
    return {event["type"] for event in json.loads(state_path.read_text(encoding="utf-8"))["events"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_inputs", [False, True])
async def test_search_scans_from_first_page_until_has_more_false(monkeypatch, tmp_path, legacy_inputs):
    crawler, state_path = prepare_scan(monkeypatch, tmp_path, legacy_inputs=legacy_inputs)
    client = PagedSearch(last_offset=30)
    crawler.dy_client = client

    await crawler.search()

    assert [call["offset"] for call in client.calls] == [0, 10, 20, 30]
    assert [call["search_id"] for call in client.calls] == [
        "", "stable-search-id", "stable-search-id", "stable-search-id",
    ]
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert len(stopped) == 1
    assert stopped[0]["stop_reason"] == "source_exhausted"
    assert stopped[0]["stop_detail"] == "has_more_false"
    assert stopped[0]["source_offset"] == 30
    assert sorted(stopped[0]["candidate_identities"]) == ["100", "110", "120", "130"]
    batches = events_of(state_path, "adaptive_batch_completed")
    assert [batch["source_offset"] for batch in batches] == [0, 10, 20, 30]
    assert {batch["discovery_phase"] for batch in batches} == {"frontier"}
    assert not {"douyin_frontier_cursor_rebound", "discovery_frontier_reseeded"} & event_types(state_path)


@pytest.mark.asyncio
async def test_missing_capture_mid_scan_keeps_completed_batches_and_skips_missing_page(monkeypatch, tmp_path):
    crawler, state_path = prepare_scan(monkeypatch, tmp_path, legacy_inputs=False)
    client = PagedSearch(last_offset=50, missing_offset=20)
    crawler.dy_client = client

    await crawler.search()

    assert [call["offset"] for call in client.calls] == [0, 10, 20]
    batches = events_of(state_path, "adaptive_batch_completed")
    assert [batch["source_offset"] for batch in batches] == [0, 10]
    (stopped,) = events_of(state_path, "adaptive_search_stopped")
    assert stopped["stop_reason"] == "runtime_failed"
    assert stopped["stop_detail"] == MISSING
    assert stopped["source_offset"] == 20
    assert stopped["batch_complete"] is False
    # 缺失页没有任何候选进入已处理集合，下一轮全量扫描到时照常处理。
    assert sorted(stopped["candidate_identities"]) == ["100", "110"]


@pytest.mark.asyncio
async def test_fresh_first_page_empty_still_uses_visible_no_result_gate(monkeypatch, tmp_path):
    crawler, state_path = prepare_scan(monkeypatch, tmp_path, legacy_inputs=True)

    class EmptyFirstPage:
        calls = []

        async def search_info_by_keyword(self, **kwargs):
            self.calls.append(kwargs)
            return {"status_code": 0, "data": [], "has_more": 0, "extra": {"logid": "id"}}

    class Locator:
        def __init__(self, text=""):
            self.text = text

        async def count(self):
            return 0

        async def inner_text(self, **kwargs):
            return self.text

    crawler.dy_client = EmptyFirstPage()
    crawler.context_page = SimpleNamespace(
        url="https://www.douyin.com/search/x",
        locator=lambda selector: Locator("没有找到相关结果" if selector == "body" else ""),
    )

    await crawler.search()

    assert [(call["offset"], call["search_id"]) for call in crawler.dy_client.calls] == [(0, "")]
    (stopped,) = events_of(state_path, "adaptive_search_stopped")
    assert (stopped["stop_reason"], stopped["stop_detail"]) == ("source_exhausted", "verified_empty_first_page")


# ----------------------------- runner 与 child CLI -----------------------------


import sqlite3  # noqa: E402
import sys  # noqa: E402
from importlib import import_module  # noqa: E402
from pathlib import Path  # noqa: E402

from trippostcollect.db.bootstrap import bootstrap_connection  # noqa: E402
from trippostcollect.scheduler.discovery import query_fingerprint, save_checkpoint  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
crawl_runner = import_module("crawl_runner")

FORMAL_PARAMS = {
    "keyword": KEYWORD, "top_refresh_max_pages": 3,
    "required_fields_profile": "image_post_with_followers_v1", "followers_policy": "required",
    "login_type": "cookie", "headless": False,
}
FULL_SCAN_REMOVED_FLAGS = ("--start-offset", "--start-cursor", "--top-refresh-max-pages", "--discovery-source-exhausted")


def runner_args(**overrides):
    values = dict(
        recovery_keyword=None, start_page=None, resume_summary=None, db="posts.sqlite",
        headful=False, headless=False, no_import=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def insert_job(conn, platform):
    conn.execute(
        """
        INSERT INTO crawl_jobs (
            job_key, site_key, target_url, job_kind, next_run_at, params_json, behavior_profile_json
        ) VALUES (?, ?, '', 'mediacrawler_search', datetime('now'), ?, ?)
        """,
        (f"{platform}-t124", platform, json.dumps({"platform": platform, **FORMAL_PARAMS}),
         json.dumps({"name": "social_high_risk"})),
    )
    conn.commit()
    return conn.execute("SELECT * FROM crawl_jobs WHERE job_key=?", (f"{platform}-t124",)).fetchone()


def seed_checkpoint(conn, row, platform, *, status, summary_path=None):
    fingerprint = query_fingerprint(platform, KEYWORD, json.loads(row["params_json"]))
    save_checkpoint(
        conn, job_id=int(row["id"]), platform_key=platform, keyword=KEYWORD,
        query_fingerprint_value=fingerprint, resume_page=29, resume_offset=285 if platform == "douyin" else None,
        resume_cursor="stale-search-id" if platform == "douyin" else None,
        source_has_more=False if status == "exhausted" else True, last_batch_complete=True,
        last_stop_reason="source_exhausted" if status == "exhausted" else "runtime_failed",
        last_run_id="prior-run",
        last_stop_detail="has_more_false" if status == "exhausted" else "browser_search_response_missing",
    )
    if summary_path is not None:
        conn.execute(
            "UPDATE crawl_discovery_checkpoints SET last_summary_path=? WHERE job_id=? AND query_fingerprint=?",
            (str(summary_path), int(row["id"]), fingerprint),
        )
    conn.commit()


def child_command(tmp_path, platform, *, checkpoint=None, with_summary=False):
    summary_path = None
    if with_summary:
        summary_path = tmp_path / "campaign_summary.json"
        summary_path.write_text('{"records": []}', encoding="utf-8")
    with sqlite3.connect(tmp_path / f"{platform}-runner.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(conn, platform)
        if checkpoint is not None:
            seed_checkpoint(conn, row, platform, status=checkpoint, summary_path=summary_path)
        job_args, plan = crawl_runner.resolve_discovery_args(conn, row, runner_args(), run_id="t124-run")
        return crawl_runner.build_command(row, job_args), plan, summary_path


# 累计摘要只经 checkpoint 交给下一轮，所以“无 checkpoint”只有不带摘要一种。
@pytest.mark.parametrize("checkpoint, with_summary", [
    (None, False), ("active", False), ("active", True), ("exhausted", False), ("exhausted", True),
])
def test_runner_douyin_child_command_always_scans_from_first_page(tmp_path, checkpoint, with_summary):
    command, plan, summary_path = child_command(
        tmp_path, "douyin", checkpoint=checkpoint, with_summary=with_summary,
    )

    for flag in FULL_SCAN_REMOVED_FLAGS:
        assert flag not in command, flag
    assert "--start-page" not in command or command[command.index("--start-page") + 1] == "1"
    assert plan["full_scan"] is True and plan["resume_page"] == 1
    # 耗尽的 checkpoint 只是审计证据：下一轮仍从第 1 页全量扫，不退化为“只刷顶部”。
    assert plan["source_exhausted"] is False and plan["top_refresh_max_pages"] == 0
    if with_summary:
        assert command[command.index("--resume-summary") + 1] == str(summary_path.resolve())
    else:
        assert "--resume-summary" not in command


def test_runner_rejects_explicit_start_page_for_douyin(tmp_path):
    with sqlite3.connect(tmp_path / "explicit.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        row = insert_job(conn, "douyin")
        with pytest.raises(ValueError, match="douyin_full_scan_rejects_start_page"):
            crawl_runner.resolve_discovery_args(conn, row, runner_args(start_page=3), run_id="t124-run")


@pytest.mark.parametrize("platform", ["bilibili", "weibo", "zhihu"])
def test_runner_other_platforms_keep_saved_page_refresh_and_exhaustion(tmp_path, platform):
    command, plan, summary_path = child_command(tmp_path, platform, checkpoint="exhausted", with_summary=True)

    assert command[command.index("--start-page") + 1] == "29"
    assert command[command.index("--top-refresh-max-pages") + 1] == "3"
    assert "--discovery-source-exhausted" in command
    assert command[command.index("--resume-summary") + 1] == str(summary_path.resolve())
    assert "--start-offset" not in command and "--start-cursor" not in command
    assert plan["full_scan"] is False and plan["source_exhausted"] is True


@pytest.mark.parametrize("flag, value", [("--start-offset", "285"), ("--start-cursor", "stale-search-id")])
def test_child_cli_rejects_removed_douyin_resume_flags(monkeypatch, capsys, flag, value):
    from trippostcollect.application import inputs

    monkeypatch.setattr(sys, "argv", ["mediacrawler_crawl.py", "--platforms", "douyin", flag, value])
    with pytest.raises(SystemExit) as raised:
        inputs.parse_args()
    assert raised.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [
    ["--start-page", "3", "--resume-summary", "prior.json"],
    ["--top-refresh-max-pages", "3"],
    ["--discovery-source-exhausted"],
])
def test_child_rejects_douyin_resume_and_refresh_args(monkeypatch, tmp_path, extra):
    from trippostcollect.application import collection, inputs

    monkeypatch.setattr(sys, "argv", [
        "mediacrawler_crawl.py", "--platforms", "douyin", "--no-import", "--download-images",
        "--db", str(tmp_path / "posts.sqlite"), *extra,
    ])
    args = inputs.parse_args()

    def reached_worker(*args, **kwargs):
        raise AssertionError("douyin resume/refresh args must be rejected before any platform run")

    # 参数校验必须在任何平台执行（策略节流、浏览器、worker）之前拒绝；越过校验即直接失败，不等待。
    monkeypatch.setattr(collection, "run_platform", reached_worker)
    ports = SimpleNamespace(ensure_prerequisites=lambda: None, run_bilibili_article_search=reached_worker)
    with pytest.raises(SystemExit, match="douyin_full_scan_rejects_resume_args"):
        collection._run_main(args, None, ports=ports)
