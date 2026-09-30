from __future__ import annotations

import json

import pytest

from support.douyin import config
from dataclasses import replace
from support.douyin import make_crawler as DouYinCrawler, refresh_settings
from support.douyin import make_client as DouYinClient
from trippostcollect.platforms.douyin.models import SearchResponseError
from trippostcollect.platforms.douyin.parser import (
    DOUYIN_RESULT_LINK_SELECTOR,
    classify_empty_first_page,
    decode_douyin_json_body,
    validate_douyin_search_response,
)


class EmptySearchClient:
    async def search_info_by_keyword(self, **kwargs):
        return {
            "status_code": 0,
            "data": [],
            "has_more": 0,
            "extra": {"logid": "request-log-id"},
        }


class RotatingLogIdClient:
    def __init__(self):
        self.calls = []

    async def search_info_by_keyword(self, **kwargs):
        self.calls.append(kwargs)
        offset = kwargs["offset"]
        if offset == 0:
            return {
                "status_code": 0,
                "data": [],
                "has_more": 1,
                "extra": {"logid": "stable-search-id"},
            }
        if offset == 10:
            return {
                "status_code": 0,
                "data": [],
                "has_more": 1,
                "extra": {"logid": "request-log-id-only"},
            }
        return {
            "status_code": 0,
            "data": [],
            "has_more": 0,
            "extra": {"logid": "terminal-request-log-id"},
        }


class RefreshCoveringClient(RotatingLogIdClient):
    async def search_info_by_keyword(self, **kwargs):
        self.calls.append(kwargs)
        offset = kwargs["offset"]
        if offset == 0:
            logid = "stable-search-id"
        else:
            logid = f"request-log-id-{offset}"
        return {
            "status_code": 0,
            "data": [],
            "has_more": 0 if offset == 30 else 1,
            "extra": {"logid": logid},
        }


class FakeLocator:
    def __init__(self, *, count: int = 0, text: str = ""):
        self._count = count
        self._text = text

    async def count(self) -> int:
        return self._count

    async def inner_text(self, *, timeout: int) -> str:
        return self._text


class FakeSearchPage:
    url = "https://www.douyin.com/search/test?type=general"

    def __init__(self, *, result_count: int, visible_text: str):
        self.result_count = result_count
        self.visible_text = visible_text

    def locator(self, selector: str) -> FakeLocator:
        if selector == "body":
            return FakeLocator(text=self.visible_text)
        return FakeLocator(count=self.result_count)


class FakeBrowserResponse:
    url = (
        "https://www.douyin.com/aweme/v1/web/general/search/stream/"
        "?keyword=%E9%9D%92%E5%B2%9B%E6%B5%B7%E6%BB%A8%E6%97%85%E6%B8%B8&offset=0"
    )

    def __init__(self, payload: bytes, *, url: str | None = None):
        self.payload = payload
        if url is not None:
            self.url = url

    async def body(self) -> bytes:
        return self.payload


class FakeDirectResponse:
    content = b"not-json-or-a-chunked-stream"
    text = "not-json-or-a-chunked-stream"


class FakeAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, *args, **kwargs):
        return FakeDirectResponse()


class FakeWaterfallLocator:
    async def evaluate_all(self, script: str):
        return [str(100 + index) for index in range(10)]


class FakeWaterfallPage:
    def on(self, event: str, callback) -> None:
        assert event == "response"

    def locator(self, selector: str) -> FakeWaterfallLocator:
        assert selector == '[id^="waterfall_item_"]:visible'
        return FakeWaterfallLocator()


def prepare_empty_search(monkeypatch, tmp_path, *, result_count: int, visible_text: str):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 1000)
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "青岛崂山旅游攻略")
    monkeypatch.setattr(config, "PUBLISH_TIME_TYPE", 0)

    crawler = DouYinCrawler()
    crawler.dy_client = EmptySearchClient()
    crawler.context_page = FakeSearchPage(
        result_count=result_count,
        visible_text=visible_text,
    )
    return crawler, state_path


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"status_code": 10000, "data": [], "has_more": 0}, "search_business_status_nonzero"),
        ({"status_code": 0, "has_more": 0}, "missing_data_field"),
        ({"status_code": 0, "data": [], "has_more": "0"}, "invalid_has_more_field"),
        ({"status_code": 0, "data": [], "has_more": 0, "extra": []}, "invalid_extra_field"),
        ({"status_code": 0, "data": [], "has_more": 1}, "missing_next_search_id"),
    ],
)
def test_invalid_search_response_is_rejected(payload, reason) -> None:
    with pytest.raises(SearchResponseError) as raised:
        validate_douyin_search_response(payload)
    assert raised.value.reason == reason


def test_empty_terminal_response_remains_available_for_page_verification() -> None:
    payload = {
        "status_code": 0,
        "data": [],
        "has_more": 0,
        "extra": {"logid": "request-log-id"},
    }
    assert validate_douyin_search_response(payload) is payload


def test_verify_check_search_response_is_rejected() -> None:
    with pytest.raises(SearchResponseError) as raised:
        validate_douyin_search_response(
            {
                "status_code": 0,
                "data": [],
                "has_more": 0,
                "search_nil_info": {"search_nil_type": "verify_check"},
            }
        )
    assert raised.value.reason == "search_verify_check"


def test_raw_chunk_framed_stream_response_is_decoded() -> None:
    payload = b'{"status_code":0,"data":[],"has_more":0}'
    framed = f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
    assert decode_douyin_json_body(framed) == {
        "status_code": 0,
        "data": [],
        "has_more": 0,
    }


def test_multi_document_chunked_stream_is_merged_in_source_order() -> None:
    first = json.dumps(
        {
            "status_code": 0,
            "data": [{"aweme_info": {"aweme_id": "first"}}],
            "has_more": 1,
            "extra": {"logid": "first-log"},
        },
        separators=(",", ":"),
    ).encode()
    second = json.dumps(
        {
            "status_code": 0,
            "data": [{"aweme_info": {"aweme_id": "second"}}],
            "has_more": 1,
            "extra": {"logid": "stable-search-id"},
        },
        separators=(",", ":"),
    ).encode()
    framed = (
        f"{len(first):x}\r\n".encode()
        + first
        + b"\r\n"
        + f"{len(second):x}\r\n".encode()
        + second
        + b"\r\n0\r\n\r\n"
    )

    assert decode_douyin_json_body(framed) == {
        "status_code": 0,
        "data": [
            {"aweme_info": {"aweme_id": "first"}},
            {"aweme_info": {"aweme_id": "second"}},
        ],
        "has_more": 1,
        "extra": {"logid": "stable-search-id"},
    }


def test_multi_document_chunked_stream_preserves_verify_check() -> None:
    first = json.dumps(
        {
            "status_code": 0,
            "data": [],
            "has_more": 0,
            "search_nil_info": {"search_nil_type": "verify_check"},
        },
        separators=(",", ":"),
    ).encode()
    second = json.dumps(
        {
            "status_code": 0,
            "data": [],
            "has_more": 0,
            "search_nil_info": {"search_nil_type": "normal_empty"},
        },
        separators=(",", ":"),
    ).encode()
    framed = (
        f"{len(first):x}\r\n".encode()
        + first
        + b"\r\n"
        + f"{len(second):x}\r\n".encode()
        + second
        + b"\r\n0\r\n\r\n"
    )

    with pytest.raises(SearchResponseError) as raised:
        validate_douyin_search_response(decode_douyin_json_body(framed))
    assert raised.value.reason == "search_verify_check"


@pytest.mark.asyncio
async def test_direct_request_preserves_search_decode_failure(monkeypatch) -> None:
    client = DouYinClient(
        headers={"User-Agent": "test-agent"},
        playwright_page=None,
        cookie_dict={},
    )

    client.ports = replace(client.ports, make_async_client=lambda **kwargs: FakeAsyncClient())

    with pytest.raises(SearchResponseError) as raised:
        await client.request(method="GET", url="https://www.douyin.com/search")

    assert raised.value.reason == "invalid_stream_chunk_header"


def test_empty_first_page_classifier_prefers_visible_results() -> None:
    assert classify_empty_first_page(
        visible_result_count=4,
        visible_text="暂无搜索结果",
    ) == "visible_results"


def test_visible_result_selector_covers_new_waterfall_cards() -> None:
    assert '.search-result-card:visible' in DOUYIN_RESULT_LINK_SELECTOR
    assert '[id^="waterfall_item_"]:visible' in DOUYIN_RESULT_LINK_SELECTOR


@pytest.mark.asyncio
async def test_search_request_uses_current_single_column_contract(monkeypatch) -> None:
    captured = {}
    client = DouYinClient(
        headers={"User-Agent": "test-agent"},
        playwright_page=None,
        cookie_dict={},
    )

    async def fake_get(uri, params=None, headers=None):
        captured.update({"uri": uri, "params": params, "headers": headers})
        return {
            "status_code": 0,
            "data": [],
            "has_more": 0,
            "extra": {"logid": "request-log-id"},
        }

    monkeypatch.setattr(client, "get", fake_get)
    await client.search_info_by_keyword(keyword="青岛海滨旅游")

    assert captured["uri"] == "/aweme/v1/web/general/search/stream/"
    assert captured["params"]["count"] == "10"
    assert captured["params"]["list_type"] == "single"
    assert captured["params"]["search_source"] == "normal_search"
    assert captured["params"]["from_group_id"] == ""
    assert captured["params"]["disable_rs"] == "0"
    assert captured["params"]["need_filter_settings"] == "1"


@pytest.mark.asyncio
async def test_browser_search_response_is_reused_before_duplicate_api_request(
    monkeypatch,
) -> None:
    payload = (
        b'{"status_code":0,"data":[{"aweme_info":{"aweme_id":"123"}}],'
        b'"has_more":1,"extra":{"logid":"next-search-id"}}'
    )
    framed = f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
    client = DouYinClient(
        headers={"User-Agent": "test-agent"},
        playwright_page=None,
        cookie_dict={},
    )
    await client.capture_browser_search_response(FakeBrowserResponse(framed))

    async def duplicate_request(*args, **kwargs):
        raise AssertionError("browser-captured first page must be reused")

    monkeypatch.setattr(client, "get", duplicate_request)
    response = await client.search_info_by_keyword(keyword="青岛海滨旅游")
    assert response["data"][0]["aweme_info"]["aweme_id"] == "123"
    assert response["extra"]["logid"] == "next-search-id"


@pytest.mark.asyncio
async def test_latest_non_verify_browser_response_wins_over_prefetch_verify(
    monkeypatch,
) -> None:
    verify_payload = (
        b'{"status_code":0,"data":[],"has_more":0,'
        b'"search_nil_info":{"search_nil_type":"verify_check"}}'
    )
    healthy_payload = (
        b'{"status_code":0,"data":[{"aweme_info":{"aweme_id":"456"}}],'
        b'"has_more":1,"extra":{"logid":"healthy-search-id"}}'
    )
    client = DouYinClient(
        headers={"User-Agent": "test-agent"},
        playwright_page=None,
        cookie_dict={},
    )
    await client.capture_browser_search_response(FakeBrowserResponse(verify_payload))
    await client.capture_browser_search_response(FakeBrowserResponse(healthy_payload))

    async def duplicate_request(*args, **kwargs):
        raise AssertionError("healthy browser response must win")

    monkeypatch.setattr(client, "get", duplicate_request)
    response = await client.search_info_by_keyword(keyword="青岛海滨旅游")
    assert response["data"][0]["aweme_info"]["aweme_id"] == "456"
    assert response["extra"]["logid"] == "healthy-search-id"


@pytest.mark.parametrize("failure_reason", ["search_verify_check", "invalid_stream_json"])
@pytest.mark.asyncio
async def test_visible_first_page_rebuild_requires_healthy_next_page(
    monkeypatch,
    failure_reason,
) -> None:
    next_page_payload = (
        b'{"status_code":0,"data":[{"aweme_info":{"aweme_id":"456"}}],'
        b'"has_more":1,"extra":{"logid":"later-search-id"}}'
    )
    next_page_url = (
        "https://www.douyin.com/aweme/v1/web/general/search/single/"
        "?keyword=%E9%9D%92%E5%B2%9B%E6%B5%B7%E6%BB%A8%E6%97%85%E6%B8%B8"
        "&offset=10&search_id=first-page-search-id"
    )
    client = DouYinClient(
        headers={"User-Agent": "test-agent"},
        playwright_page=FakeWaterfallPage(),
        cookie_dict={},
    )
    await client.capture_browser_search_response(
        FakeBrowserResponse(next_page_payload, url=next_page_url)
    )

    async def unreadable_first_page(uri, params=None, headers=None):
        if failure_reason == "invalid_stream_json":
            raise SearchResponseError(failure_reason)
        return {
            "status_code": 0,
            "data": [],
            "has_more": 0,
            "search_nil_info": {"search_nil_type": "verify_check"},
        }

    async def fake_detail(aweme_id: str):
        return {"aweme_id": aweme_id, "images": [{"url_list": ["https://img.test/1"]}]}

    monkeypatch.setattr(client, "get", unreadable_first_page)
    monkeypatch.setattr(client, "get_video_by_id", fake_detail)
    response = await client.search_info_by_keyword(keyword="青岛海滨旅游")
    assert len(response["data"]) == 10
    assert response["data"][0]["aweme_info"]["aweme_id"] == "100"
    assert response["has_more"] == 1
    assert response["extra"]["logid"] == "first-page-search-id"


@pytest.mark.asyncio
async def test_visible_first_page_rebuild_does_not_mask_business_error(
    monkeypatch,
) -> None:
    client = DouYinClient(
        headers={"User-Agent": "test-agent"},
        playwright_page=FakeWaterfallPage(),
        cookie_dict={},
    )

    async def business_error(uri, params=None, headers=None):
        raise SearchResponseError("search_business_status_nonzero")

    async def forbidden_fallback(**kwargs):
        raise AssertionError("business response errors must not use visible-page fallback")

    monkeypatch.setattr(client, "get", business_error)
    monkeypatch.setattr(client, "_build_visible_first_page_fallback", forbidden_fallback)

    with pytest.raises(SearchResponseError) as raised:
        await client.search_info_by_keyword(keyword="青岛海滨旅游")

    assert raised.value.reason == "search_business_status_nonzero"


@pytest.mark.asyncio
async def test_followup_page_scrolls_for_browser_response_before_direct_request(
    monkeypatch,
) -> None:
    client = DouYinClient(
        headers={"User-Agent": "test-agent"},
        playwright_page=None,
        cookie_dict={},
    )

    async def fake_scroll(*, keyword: str, offset: int, search_id: str) -> bool:
        client._observed_search_responses.append(
            {
                "keyword": keyword,
                "offset": offset,
                "search_id": search_id,
                "payload": {
                    "status_code": 0,
                    "data": [{"aweme_info": {"aweme_id": "789"}}],
                    "has_more": 1,
                    "extra": {"logid": "request-log-id"},
                },
            }
        )
        return True

    async def duplicate_request(*args, **kwargs):
        raise AssertionError("followup page must reuse browser response after scrolling")

    monkeypatch.setattr(client, "_scroll_for_observed_search_response", fake_scroll)
    monkeypatch.setattr(client, "get", duplicate_request)
    response = await client.search_info_by_keyword(
        keyword="青岛海滨旅游",
        offset=40,
        search_id="stable-search-id",
    )
    assert response["data"][0]["aweme_info"]["aweme_id"] == "789"


@pytest.mark.asyncio
async def test_visible_results_turn_empty_first_api_page_into_runtime_failure(
    monkeypatch,
    tmp_path,
) -> None:
    crawler, state_path = prepare_empty_search(
        monkeypatch,
        tmp_path,
        result_count=4,
        visible_text="综合 视频 用户",
    )

    refresh_settings(crawler)
    await crawler.search()

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    checked = [event for event in events if event["type"] == "douyin_empty_first_page_checked"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"]
    assert checked[-1]["details"]["classification"] == "visible_results"
    assert stopped[-1]["details"]["stop_reason"] == "runtime_failed"
    assert stopped[-1]["details"]["stop_detail"] == "empty_api_response_with_visible_results"
    assert stopped[-1]["details"]["resume_page"] == 1
    assert stopped[-1]["details"]["resume_offset"] == 0


@pytest.mark.asyncio
async def test_explicit_visible_no_result_marker_can_exhaust_first_page(
    monkeypatch,
    tmp_path,
) -> None:
    crawler, state_path = prepare_empty_search(
        monkeypatch,
        tmp_path,
        result_count=0,
        visible_text="没有找到相关结果，换个关键词试试",
    )

    refresh_settings(crawler)
    await crawler.search()

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"]
    assert stopped[-1]["details"]["stop_reason"] == "source_exhausted"
    assert stopped[-1]["details"]["stop_detail"] == "verified_empty_first_page"


@pytest.mark.asyncio
async def test_ambiguous_empty_first_page_is_runtime_failure(monkeypatch, tmp_path) -> None:
    crawler, state_path = prepare_empty_search(
        monkeypatch,
        tmp_path,
        result_count=0,
        visible_text="综合 视频 用户",
    )

    refresh_settings(crawler)
    await crawler.search()

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"]
    assert stopped[-1]["details"]["stop_reason"] == "runtime_failed"
    assert stopped[-1]["details"]["stop_detail"] == "ambiguous_empty_first_page"


@pytest.mark.asyncio
async def test_followup_pages_keep_initial_search_id_when_logid_rotates(
    monkeypatch,
    tmp_path,
) -> None:
    crawler, state_path = prepare_empty_search(
        monkeypatch,
        tmp_path,
        result_count=0,
        visible_text="综合 视频 用户",
    )
    client = RotatingLogIdClient()
    crawler.dy_client = client
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)

    refresh_settings(crawler)
    await crawler.search()

    assert [call["offset"] for call in client.calls] == [0, 10, 20]
    assert [call["search_id"] for call in client.calls] == [
        "",
        "stable-search-id",
        "stable-search-id",
    ]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    observed = [event for event in events if event["type"] == "douyin_search_response_observed"]
    assert observed[1]["details"]["cursor_source"] == "request_search_id"
    assert observed[1]["details"]["response_logid_matches_cursor"] is False


@pytest.mark.asyncio
async def test_deep_frontier_rebinds_to_current_refresh_session_cursor(
    monkeypatch,
    tmp_path,
) -> None:
    crawler, state_path = prepare_empty_search(
        monkeypatch,
        tmp_path,
        result_count=0,
        visible_text="综合 视频 用户",
    )
    client = RefreshCoveringClient()
    crawler.dy_client = client
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "START_PAGE", 3)
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", "20")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", "stale-prior-session-id")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "3")

    refresh_settings(crawler)
    await crawler.search()

    assert [call["offset"] for call in client.calls] == [0, 10, 20, 30]
    assert [call["search_id"] for call in client.calls] == [
        "",
        "stable-search-id",
        "stable-search-id",
        "stable-search-id",
    ]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    rebound = [event for event in events if event["type"] == "douyin_frontier_cursor_rebound"]
    assert rebound[-1]["details"]["saved_page"] == 3
    assert rebound[-1]["details"]["saved_offset"] == 20
    assert rebound[-1]["details"]["resume_page"] == 4
    assert rebound[-1]["details"]["resume_offset"] == 30
    assert rebound[-1]["details"]["covered_by_refresh"] is True
