"""抖音离线故障特征化（T13）：只替换 httpx 传输与搜索客户端边界，固定现行行为。

抖音请求层没有统一 Tenacity：传输层异常只发生一次即原样上抛（规格 E3"DY request/detail"）。
搜索与作者资料都只把 DataFetchError/SearchResponseError 映射为运行失败或候选失败，
传输层异常直接终止本轮，不写停止事件、不宣称耗尽。
"""

from __future__ import annotations

from dataclasses import replace
import json

import httpx
import pytest

from support.douyin import config, make_crawler as DouYinCrawler
from trippostcollect.platforms import entry
from trippostcollect.platforms.douyin import core as douyin_core
from trippostcollect.platforms.douyin.client import DouYinClient


class Transport:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        self.calls.append(httpx.URL(url).path)
        raise httpx.ConnectError("离线网络中断", request=httpx.Request(method, url))


def image_aweme(aweme_id: str) -> dict:
    return {
        "aweme_id": aweme_id,
        "aweme_type": 68,
        "desc": "青岛图文",
        "create_time": 1_700_000_000,
        "author": {"uid": "author", "nickname": "name", "sec_uid": f"sec-{aweme_id}"},
        "statistics": {"digg_count": 1, "collect_count": 2, "comment_count": 3, "share_count": 4},
        "images": [{"uri": f"uri-{aweme_id}", "url_list": [f"https://p3.test/{aweme_id}.jpeg"]}],
    }


def prepare(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 10)
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "青岛旅游")
    monkeypatch.setattr(config, "PUBLISH_TIME_TYPE", 0)
    return state_path


def events_of(state_path, kind):
    return [event["details"] for event in json.loads(state_path.read_text(encoding="utf-8"))["events"]
            if event["type"] == kind]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/aweme/v1/web/general/search/single/",
    "/aweme/v1/web/user/profile/other/",
    "/aweme/v1/web/aweme/detail/",
])
async def test_request_transport_error_is_raised_once_without_retry(path):
    transport = Transport()
    ports = entry.douyin_dependencies(config)["ports"].client
    client = DouYinClient(
        headers={"User-Agent": "t13", "Cookie": ""}, playwright_page=None, cookie_dict={},
        ports=replace(ports, make_async_client=transport),
    )

    with pytest.raises(httpx.ConnectError):
        await client.request("GET", "https://www.douyin.com" + path)

    assert transport.calls == [path]


@pytest.mark.asyncio
async def test_search_transport_error_ends_run_without_exhaustion(monkeypatch, tmp_path):
    state_path = prepare(monkeypatch, tmp_path)
    calls = []

    class Client:
        async def search_info_by_keyword(self, **kwargs):
            calls.append(kwargs["offset"])
            raise httpx.ConnectError("离线网络中断")

    crawler = DouYinCrawler()
    crawler.dy_client = Client()

    with pytest.raises(httpx.ConnectError):
        await crawler.search()

    assert calls == [0]
    assert events_of(state_path, "adaptive_search_stopped") == []
    assert events_of(state_path, "adaptive_batch_completed") == []


@pytest.mark.asyncio
async def test_creator_profile_transport_error_ends_run_without_retry_or_skip(monkeypatch, tmp_path):
    state_path = prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH", "30")
    profile_calls = []
    sleeps = []

    class Client:
        async def search_info_by_keyword(self, **kwargs):
            return {"status_code": 0, "data": [{"aweme_info": image_aweme("first")}],
                    "has_more": 0, "extra": {"logid": "fresh-search-id"}}

        async def get_user_info(self, sec_uid):
            profile_calls.append(sec_uid)
            raise httpx.ConnectError("离线网络中断")

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(douyin_core.asyncio, "sleep", sleep)
    crawler = DouYinCrawler()
    crawler.dy_client = Client()

    with pytest.raises(httpx.ConnectError):
        await crawler.search()

    # 作者资料循环只对 DataFetchError 重试（3 次）；传输层异常第一次即终止本轮，不写 candidate_skipped。
    assert profile_calls == ["sec-first"]
    assert sleeps == []
    assert events_of(state_path, "candidate_skipped") == []
    assert events_of(state_path, "adaptive_search_stopped") == []
