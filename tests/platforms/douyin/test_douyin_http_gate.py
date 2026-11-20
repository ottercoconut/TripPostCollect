"""#116 抖音搜索流程的离线回归：空 extra、视频候选、作者资料与平台阻断。

不访问网络。作者资料由浮层替身提供。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from support.douyin import config, make_crawler as DouYinCrawler, install_detail_passthrough
from trippostcollect.platforms.douyin import core as douyin_core
from trippostcollect.platforms.douyin.detail_page import DouyinDetailFetchError
from trippostcollect.platforms.douyin.models import PlatformRuntimeError


# ----------------------------- worker 搜索流程 -----------------------------


def prepare(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
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

    async def search_info_by_keyword(self, **kwargs):
        return {"status_code": 0, "data": [{"aweme_info": item} for item in self.items],
                "has_more": 0, "extra": self.extra}

    async def get_user_info(self, sec_uid):
        self.profile_calls.append(sec_uid)
        if isinstance(self.profile, Exception):
            raise self.profile
        return self.profile or {}


def search_crawler(client, stored, *, with_profile: bool = True):
    crawler = DouYinCrawler()
    crawler.dy_client = client
    crawler.get_aweme_images = AsyncMock(return_value=None)
    install_detail_passthrough(crawler, with_profile=with_profile)

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
    assert client.profile_calls == []
    assert crawler.detail_pages.calls == ["note-1"]
    crawler.get_aweme_images.assert_awaited_once()
    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert sorted(stopped["candidate_identities"]) == ["note-1", "video-1"]
    assert stopped["valid_new_count"] == 1


@pytest.mark.asyncio
async def test_search_author_placeholder_followers_are_not_valid_and_skip_images(monkeypatch, tmp_path):
    state_path = prepare(monkeypatch, tmp_path)
    # 没有作者页资料时，搜索作者对象上的占位粉丝不算已观测，也不下载正文图。
    stored = []
    client = SearchClient([image_aweme("1")], extra={"logid": "id"})

    crawler = search_crawler(client, stored, with_profile=False)
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
    client = SearchClient([image_aweme("1"), image_aweme("2")], extra={"logid": "id"})
    crawler = search_crawler(client, stored)

    class _Blocked:
        calls: list[str] = []

        async def open_from_search(self, aweme, *, search_visit_id: str = ""):
            del search_visit_id
            self.calls.append(str(aweme.get("aweme_id") or ""))
            raise DouyinDetailFetchError(
                str(aweme.get("aweme_id") or ""),
                code,
                runtime_blocking=True,
                retryable=False,
            )

    crawler.detail_pages = _Blocked()
    await crawler.search()

    assert crawler.detail_pages.calls == ["1"]
    assert client.profile_calls == []
    assert stored == []
    assert events_of(state_path, "candidate_skipped") == []
    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert stopped["stop_reason"] == "runtime_failed"
    assert stopped["stop_detail"] == code
    assert stopped["resume_offset"] == 0


@pytest.mark.asyncio
async def test_author_page_miss_is_candidate_skip_without_httpx(monkeypatch, tmp_path):
    state_path = prepare(monkeypatch, tmp_path)
    stored = []
    client = SearchClient([image_aweme("1")], extra={"logid": "id"})
    crawler = search_crawler(client, stored)

    class _Missing:
        async def open_from_search(self, aweme, *, search_visit_id: str = ""):
            del search_visit_id
            raise DouyinDetailFetchError(
                str(aweme.get("aweme_id") or ""),
                "author_profile_missing",
                attempts=3,
                retryable=False,
            )

    crawler.detail_pages = _Missing()
    await crawler.search()

    assert client.profile_calls == []
    skipped = events_of(state_path, "candidate_skipped")
    assert skipped[0]["error_code"] == "author_profile_missing"
    assert skipped[0]["retryable"] is False
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
