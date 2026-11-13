"""#185 知乎搜索页面截获：假页面与假响应，不访问平台、不启动浏览器、不写真实库。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from .support import END, config, make_crawler, search_page
from trippostcollect.platforms.zhihu.models import ZhihuContent
from trippostcollect.platforms.zhihu.search_capture import (
    ZHIHU_ME_RULE, me_is_logged_in,
)
from trippostcollect.runtime.page_capture import CapturedResponse


def answer(content_id: str) -> ZhihuContent:
    return ZhihuContent(
        content_id=content_id, question_id="10", content_type="answer",
        content_text="青岛正文", content_url=f"https://www.zhihu.com/question/10/answer/{content_id}",
        created_time=1_700_000_000, creator_hash=f"author-{content_id}", user_nickname="作者",
        followers_observed=True, image_list=[f"https://pic1.zhimg.com/v2-{content_id}_r.jpg"],
        content_detail_status="detail_observed", content_detail_source="search_content",
    )


class Response:
    def __init__(self, url, status, payload, page):
        self.url = url
        self.status = status
        self.headers = {}
        self.request = SimpleNamespace(
            method="GET", resource_type="fetch", url=url, frame=SimpleNamespace(page=page),
        )
        self._body = json.dumps(payload).encode()

    async def body(self):
        return self._body


class Mouse:
    def __init__(self, page):
        self.page = page

    async def move(self, *args, **kwargs):
        return None

    async def wheel(self, dx, dy):
        if dy <= 0:
            return
        self.page.wheels += 1
        self.page.scroll_y += 100
        if self.page.on_wheel is not None:
            self.page.on_wheel(self.page)


class Page:
    def __init__(self):
        self.url = "https://www.zhihu.com/search?q=qingdao&type=content"
        self.listeners = {"response": [], "requestfailed": []}
        self.scroll_y = 0
        self.wheels = 0
        self.on_wheel = None
        self.mouse = Mouse(self)
        self.context = SimpleNamespace()

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def emit(self, url, payload, status=200):
        response = Response(url, status, payload, self)
        for callback in list(self.listeners["response"]):
            callback(response)

    async def wait_for_timeout(self, timeout):
        return None

    async def evaluate(self, script, arg=None):
        if "innerWidth" in script:
            return {"width": 1450, "height": 900}
        return {
            "window_y": self.scroll_y, "scrollable_count": 1,
            "scroll_top_sum": self.scroll_y, "max_scroll_top": self.scroll_y,
        }


class Context:
    def __init__(self, page):
        self.page = page
        self.pages = [page]
        page.context = self

    def on(self, event, callback):
        assert event == "page"


def payload(content_id, *, is_end=False):
    if content_id is None:
        return {"data": [], "paging": {"is_end": True}}
    return {
        "data": [{"type": "search_result", "object": {
            "id": content_id, "type": "answer", "question": {"id": "10"},
            "created_time": 1_700_000_000, "content": "<p>青岛正文</p>",
            "author": {"id": "author", "name": "作者", "url_token": "author", "follower_count": 3},
        }}],
        "paging": {"is_end": is_end},
    }


def prepare(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior.json"))
    monkeypatch.setenv("TRIPPOSTCOLLECT_ZHIHU_INITIAL_SETTLE_SECONDS", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "KEYWORDS", "青岛旅游")
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "START_PAGE", 9)
    page = Page()
    context = Context(page)
    crawler = make_crawler()
    crawler.context_page = page
    from trippostcollect.platforms.zhihu.search_capture import ZhihuPageCapture

    crawler._page_capture = ZhihuPageCapture.open(context)
    crawler._page_capture.install(page)
    crawler._page_capture.begin_navigation()
    crawler._opened_search_keyword = "青岛旅游"
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.set_search_referer = MagicMock()
    crawler.enrich_search_content_detail = AsyncMock(side_effect=lambda item: item)
    crawler.get_content_images = AsyncMock(return_value=None)
    crawler._store_content = AsyncMock(return_value=None)
    return crawler, page, state_path


def events_of(state_path, kind):
    return [event["details"] for event in json.loads(state_path.read_text(encoding="utf-8"))["events"]
            if event["type"] == kind]


def ledger_lines(tmp_path):
    path = tmp_path / "behavior.content_visits.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_first_page_and_scrolled_next_page_until_is_end(monkeypatch, tmp_path):
    crawler, page, state_path = prepare(monkeypatch, tmp_path)
    page.emit("https://www.zhihu.com/api/v4/search_v3?offset=0", payload("a1"))

    def on_wheel(current):
        current.emit(
            "https://www.zhihu.com/api/v4/search_v3?offset=20",
            payload(None),
        )

    page.on_wheel = on_wheel
    await crawler.search()
    await crawler._close_page_capture()

    assert page.wheels == 1
    assert [call.args[0].content_id for call in crawler.enrich_search_content_detail.await_args_list] == ["a1"]
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"], item["source_page"]) for item in stopped] == [
        ("source_exhausted", "paging_is_end", 2),
    ]
    kinds = [(line.get("kind"), line.get("action") or line.get("stage")) for line in ledger_lines(tmp_path)]
    assert ("action", "navigate") in kinds
    assert ("action", "scroll") in kinds
    assert ("pause", "search_page") in kinds
    assert ("visit", "search_results") in kinds


@pytest.mark.asyncio
async def test_capture_timeout_is_runtime_failure_without_seen(monkeypatch, tmp_path):
    import trippostcollect.platforms.zhihu.search_capture as search_capture

    monkeypatch.setattr(search_capture, "FOLLOWUP_CAPTURE_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(search_capture, "FOLLOWUP_SCROLL_ATTEMPTS", 1)
    crawler, page, state_path = prepare(monkeypatch, tmp_path)
    page.emit("https://www.zhihu.com/api/v4/search_v3?offset=0", payload("a1"))
    page.on_wheel = None

    await crawler.search()

    stopped = events_of(state_path, "adaptive_search_stopped")
    assert stopped[-1]["stop_reason"] == "runtime_failed"
    assert stopped[-1]["stop_detail"] == "browser_search_response_missing"
    # 第一页已经处理，所以 a1 在 seen；没截获到的下一页没有任何新 ID。
    assert stopped[-1]["candidate_identities"] == ["a1"]
    assert events_of(state_path, "candidate_skipped") == []
    assert page.wheels == 1


@pytest.mark.asyncio
async def test_missing_first_page_does_not_scroll_or_write_seen(monkeypatch, tmp_path):
    import trippostcollect.platforms.zhihu.search_capture as search_capture

    monkeypatch.setattr(search_capture, "FIRST_PAGE_CAPTURE_TIMEOUT_SECONDS", 0)
    crawler, page, state_path = prepare(monkeypatch, tmp_path)

    await crawler.search()

    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [
        ("runtime_failed", "browser_search_response_missing"),
    ]
    assert stopped[0]["candidate_identities"] == []
    assert page.wheels == 0
    crawler.enrich_search_content_detail.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_id_is_skipped_before_detail(monkeypatch, tmp_path):
    known = tmp_path / "known.json"
    known.write_text(json.dumps(["known-id"]), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH", str(known))
    crawler, page, state_path = prepare(monkeypatch, tmp_path)
    page.emit("https://www.zhihu.com/api/v4/search_v3?offset=0", {
        "data": [
            payload("known-id")["data"][0],
            payload("fresh-id")["data"][0],
        ],
        "paging": {"is_end": True},
    })

    await crawler.search()

    assert [call.args[0].content_id for call in crawler.enrich_search_content_detail.await_args_list] == ["fresh-id"]
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert stopped[-1]["stop_detail"] == "paging_is_end"


def test_logged_out_me_capture_is_not_a_session():
    captured = CapturedResponse(
        rule=ZHIHU_ME_RULE, seq=1, page_role="primary", route="/", query_keys=(),
        endpoint="GET www.zhihu.com /api/v4/me", status=200, verify_headers_present=False,
        request_json=None, json={"uid": "", "avatar_url": "https://pic1.zhimg.com/secret.jpg"},
        observed_at="2026-11-13T00:00:00+00:00",
    )
    assert me_is_logged_in(captured) is False
    assert me_is_logged_in(None) is False


def test_search_page_helper_still_represents_is_end():
    assert search_page([answer("a1")], is_end=True).is_end is True
    assert END.is_end is True
