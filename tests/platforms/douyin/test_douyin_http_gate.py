"""#116 抖音 HTTP 状态分类、请求头与 worker 有效性门禁的离线回归。

只替换 httpx 传输与搜索客户端边界；状态分类、作者补全与写出判定走真实实现。
"""

from __future__ import annotations

from dataclasses import replace
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from support.douyin import config, make_crawler as DouYinCrawler
from trippostcollect.platforms import entry
from trippostcollect.platforms.douyin import core as douyin_core
from trippostcollect.platforms.douyin.client import DouYinClient
from trippostcollect.platforms.douyin.models import DataFetchError, PlatformRuntimeError

ARGUS_BODY = b"Blocked by ArgusSecurityPlugin Uifid Not Found"


class Transport:
    """记录每次请求并按路径返回预置响应；不访问网络。"""

    def __init__(self, responder):
        self.responder = responder
        self.requests: list[httpx.Request] = []

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        request = httpx.Request(method, url, params=kwargs.get("params"), headers=kwargs.get("headers"))
        self.requests.append(request)
        status, body = self.responder(request)
        return httpx.Response(status, content=body, request=request)


class Page:
    """__process_req_params 只读取浏览器事实与 localStorage。"""

    def on(self, *args):
        pass

    async def evaluate(self, expression, *args):
        if expression == "() => window.localStorage":
            return {"xmst": "token"}
        return {"userAgent": "Mozilla/5.0 Chrome/125.0.0.0", "language": "zh-CN", "platform": "MacIntel"}


def make_client(responder, *, cookie_dict=None, page=None):
    transport = Transport(responder)
    ports = entry.douyin_dependencies(config)["ports"].client
    client = DouYinClient(
        headers={
            "User-Agent": "Mozilla/5.0 Chrome/125.0.0.0",
            "Cookie": "sessionid=secret",
            "Origin": "https://www.douyin.com/",
            "Referer": "https://www.douyin.com/",
        },
        playwright_page=page,
        cookie_dict=cookie_dict or {},
        ports=replace(ports, make_async_client=transport),
    )
    return client, transport


async def signed_bogus(*args):
    return "fixture-bogus"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, body, code",
    [
        (401, b"unauthorized", "login_required"),
        (403, ARGUS_BODY, "blocked_or_forbidden"),
        (403, b"Blocked by gateway", "blocked_or_forbidden"),
        (429, b"too many", "rate_limited"),
    ],
)
async def test_blocking_http_status_is_classified_before_decode(status, body, code):
    client, transport = make_client(lambda request: (status, body))

    with pytest.raises(PlatformRuntimeError) as raised:
        await client.request("GET", "https://www.douyin.com/aweme/v1/web/user/profile/other/")

    assert raised.value.code == code
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status, body", [(403, b"forbidden"), (500, b"oops"), (502, b"")])
async def test_other_non_2xx_is_retryable_transport_failure(status, body):
    client, _ = make_client(lambda request: (status, body))

    with pytest.raises(DataFetchError) as raised:
        await client.request("GET", "https://www.douyin.com/aweme/v1/web/user/profile/other/")

    assert not isinstance(raised.value, PlatformRuntimeError)
    assert f"HTTP {status}" in str(raised.value)


@pytest.mark.asyncio
async def test_argus_header_only_on_detail_and_uifid_prefers_uifid(monkeypatch):
    monkeypatch.setattr("trippostcollect.platforms.douyin.client.get_a_bogus", signed_bogus)

    def responder(request):
        if "/aweme/detail/" in request.url.path:
            return 200, json.dumps({"aweme_detail": {"aweme_id": "1"}}).encode()
        if "/user/profile/other/" in request.url.path:
            return 200, json.dumps({"status_code": 0, "user": {"follower_count": 3}}).encode()
        return 200, json.dumps({"status_code": 0, "data": [], "has_more": 0}).encode()

    client, transport = make_client(
        responder,
        cookie_dict={"UIFID": "stable-uifid", "UIFID_TEMP": "temp-uifid", "s_v_web_id": "verify"},
        page=Page(),
    )

    await client._get_video_by_id("1")
    await client.get_user_info("sec-1")

    by_path = {request.url.path: request for request in transport.requests}
    detail = by_path["/aweme/v1/web/aweme/detail/"]
    profile = by_path["/aweme/v1/web/user/profile/other/"]
    # #124：搜索只经浏览器截获，客户端不再发搜索请求。
    assert set(by_path) == {"/aweme/v1/web/aweme/detail/", "/aweme/v1/web/user/profile/other/"}
    assert detail.headers["x-tt-argus"] == "1"
    assert "x-tt-argus" not in profile.headers
    assert "x-tt-argus" not in client.headers
    for request in (detail, profile):
        assert request.url.params["uifid"] == "stable-uifid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"status_code": 2053, "user": {"follower_count": 9}},
        {"status_code": 0},
        {"status_code": 0, "user": None},
        {"user": {"follower_count": 9}},
    ],
)
async def test_creator_profile_without_success_envelope_is_empty(monkeypatch, payload):
    monkeypatch.setattr("trippostcollect.platforms.douyin.client.get_a_bogus", signed_bogus)
    client, _ = make_client(lambda request: (200, json.dumps(payload).encode()), page=Page())

    assert await client.get_user_info("sec-1") == {}


@pytest.mark.asyncio
async def test_creator_profile_success_envelope_is_returned(monkeypatch):
    monkeypatch.setattr("trippostcollect.platforms.douyin.client.get_a_bogus", signed_bogus)
    payload = {"status_code": 0, "user": {"follower_count": 0}}
    client, _ = make_client(lambda request: (200, json.dumps(payload).encode()), page=Page())

    assert await client.get_user_info("sec-1") == payload


@pytest.mark.asyncio
async def test_media_request_uses_browser_ua_referer_and_image_accept_without_cookie():
    client, transport = make_client(lambda request: (200, b"\x89PNG"))

    assert await client.get_aweme_media("https://p3.test/body.jpeg") == b"\x89PNG"

    headers = transport.requests[0].headers
    assert headers["user-agent"] == "Mozilla/5.0 Chrome/125.0.0.0"
    assert headers["referer"] == "https://www.douyin.com/"
    assert headers["accept"].startswith("image/")
    assert "cookie" not in headers
    assert "python-httpx" not in headers["user-agent"]


# ----------------------------- worker 搜索流程 -----------------------------


def prepare(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH", "-1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_CREATOR_SLEEP_SECONDS", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "青岛旅游")
    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "jsonl")
    monkeypatch.setattr(douyin_core.asyncio, "sleep", AsyncMock())
    return state_path


def events_of(state_path, kind):
    return [event["details"] for event in json.loads(state_path.read_text(encoding="utf-8"))["events"]
            if event["type"] == kind]


def image_aweme(aweme_id, **overrides):
    aweme = {
        "aweme_id": aweme_id,
        "aweme_type": 68,
        "desc": "青岛图文",
        "create_time": 1_700_000_000,
        "author": {"uid": "author", "nickname": "name", "sec_uid": f"sec-{aweme_id}", "follower_count": 0},
        "statistics": {"digg_count": 1, "collect_count": 2, "comment_count": 3, "share_count": 4},
        "images": [{"uri": f"uri-{aweme_id}", "url_list": [f"https://p3.test/{aweme_id}.jpeg"]}],
    }
    aweme.update(overrides)
    return aweme


def video_aweme(aweme_id):
    return {
        "aweme_id": aweme_id,
        "aweme_type": 0,
        "desc": "青岛视频",
        "create_time": 1_700_000_000,
        "author": {"uid": "author", "nickname": "name", "sec_uid": f"sec-{aweme_id}"},
        "statistics": {"digg_count": 1, "collect_count": 2, "comment_count": 3, "share_count": 4},
        "video": {"play_addr": {"url_list": ["", "https://media.test/video.mp4"]}},
    }


class SearchClient:
    def __init__(self, items, *, profile=None, extra=None):
        self.items = items
        self.profile = profile
        self.extra = extra
        self.profile_calls: list[str] = []
        self.get_aweme_media = AsyncMock(return_value=b"")

    async def search_info_by_keyword(self, **kwargs):
        return {"status_code": 0, "data": [{"aweme_info": item} for item in self.items],
                "has_more": 0, "extra": self.extra}

    async def get_user_info(self, sec_uid):
        self.profile_calls.append(sec_uid)
        if isinstance(self.profile, Exception):
            raise self.profile
        return self.profile or {}


def search_crawler(client, stored):
    crawler = DouYinCrawler()
    crawler.dy_client = client
    crawler.get_aweme_images = AsyncMock(return_value=None)

    async def store(aweme_item, **kwargs):
        stored.append((aweme_item["aweme_id"], kwargs))

    crawler.update_douyin_aweme = store
    return crawler


@pytest.mark.asyncio
async def test_null_extra_on_terminal_page_does_not_crash(monkeypatch, tmp_path):
    state_path = prepare(monkeypatch, tmp_path)
    stored = []
    client = SearchClient([image_aweme("1")], profile={"status_code": 0, "user": {"follower_count": 5}}, extra=None)

    await search_crawler(client, stored).search()

    assert [item[0] for item in stored] == ["1"]
    assert events_of(state_path, "adaptive_search_stopped")[-1]["stop_reason"] == "source_exhausted"


@pytest.mark.asyncio
async def test_video_candidate_is_processed_without_profile_or_content_record(monkeypatch, tmp_path):
    state_path = prepare(monkeypatch, tmp_path)
    stored = []
    client = SearchClient(
        [video_aweme("video-1"), image_aweme("note-1")],
        profile={"status_code": 0, "user": {"follower_count": 5}},
        extra={"logid": "id"},
    )

    crawler = search_crawler(client, stored)
    await crawler.search()

    assert [item[0] for item in stored] == ["note-1"]
    assert stored[0][1] == {"content_detail_source": "search_aweme_info", "expected_aweme_id": "note-1"}
    assert client.profile_calls == ["sec-note-1"]
    crawler.get_aweme_images.assert_awaited_once()
    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert sorted(stopped["candidate_identities"]) == ["note-1", "video-1"]
    assert stopped["valid_new_count"] == 1


@pytest.mark.asyncio
async def test_search_author_placeholder_followers_are_not_valid_and_skip_images(monkeypatch, tmp_path):
    state_path = prepare(monkeypatch, tmp_path)
    # 作者资料请求关闭时只剩搜索作者对象的占位 0：不算有效，也不下载正文图。
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS", "0")
    stored = []
    client = SearchClient([image_aweme("1")], extra={"logid": "id"})

    crawler = search_crawler(client, stored)
    await crawler.search()

    crawler.get_aweme_images.assert_not_awaited()
    assert [item[0] for item in stored] == ["1"]
    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert stopped["valid_new_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["login_required", "blocked_or_forbidden", "rate_limited"])
async def test_creator_profile_block_is_runtime_failure_without_retry(monkeypatch, tmp_path, code):
    state_path = prepare(monkeypatch, tmp_path)
    stored = []
    client = SearchClient(
        [image_aweme("1"), image_aweme("2")],
        profile=PlatformRuntimeError("blocked", code=code),
        extra={"logid": "id"},
    )

    await search_crawler(client, stored).search()

    assert client.profile_calls == ["sec-1"]
    assert stored == []
    assert events_of(state_path, "candidate_skipped") == []
    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert stopped["stop_reason"] == "runtime_failed"
    assert stopped["stop_detail"] == code
    assert stopped["resume_offset"] == 0


@pytest.mark.asyncio
async def test_creator_profile_transport_failure_stays_retryable_candidate_skip(monkeypatch, tmp_path):
    state_path = prepare(monkeypatch, tmp_path)
    stored = []
    client = SearchClient([image_aweme("1")], profile=DataFetchError("douyin HTTP 502"), extra={"logid": "id"})

    await search_crawler(client, stored).search()

    assert client.profile_calls == ["sec-1"] * 3
    skipped = events_of(state_path, "candidate_skipped")
    assert skipped[0]["error_code"] == "detail_request_failed"
    assert skipped[0]["retryable"] is True
    assert events_of(state_path, "adaptive_search_stopped")[-1]["stop_reason"] == "source_exhausted"


@pytest.mark.asyncio
async def test_search_platform_block_marks_runtime_failure_with_code(monkeypatch, tmp_path):
    state_path = prepare(monkeypatch, tmp_path)

    class BlockedSearch:
        async def search_info_by_keyword(self, **kwargs):
            raise PlatformRuntimeError("blocked", code="blocked_or_forbidden")

    crawler = DouYinCrawler()
    crawler.dy_client = BlockedSearch()
    await crawler.search()

    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert stopped["stop_reason"] == "runtime_failed"
    assert stopped["stop_detail"] == "blocked_or_forbidden"
