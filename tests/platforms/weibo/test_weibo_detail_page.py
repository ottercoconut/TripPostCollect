"""#211 微博详情：假页面、假新标签、假 show/sideUser。不访问平台、不启动浏览器。"""

from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from PIL import Image
import httpx
import pytest

from support.page_events import FakeContext, FakePage
from support.weibo_adapter import crawler as make_crawler
from support.weibo_adapter import settings
from trippostcollect.artifacts.image_candidates import content_image_candidates
from trippostcollect.platforms.weibo import detail_page as detail_mod
from trippostcollect.platforms.weibo.detail_page import (
    AUTHOR_PROFILE_MISSING,
    DETAIL_CARD_MISSING,
    DETAIL_CONTENT_EMPTY,
    DETAIL_SHOW_MISSING,
    LOGIN_REQUIRED,
    RATE_LIMITED,
    BLOCKED_OR_FORBIDDEN,
    WeiboDetailPages,
)
from trippostcollect.platforms.weibo.models import WeiboDetailFetchError, WeiboRepairNotImplemented
from trippostcollect.platforms.weibo.parser import CREATED_AT_UNPARSEABLE
from trippostcollect.platforms.weibo.search_page import WeiboPageCapture, WeiboSearchCandidate
from trippostcollect.records.formal import validate_formal_record


SHOW = "https://weibo.com/ajax/statuses/show?id=525&isGetLongText=true"
SIDE = "https://weibo.com/ajax/side/cards/sideUser?id=88"
CREATED = "Sat Jun 14 12:00:00 +0800 2025"


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 3), color="green").save(output, format="PNG")
    return output.getvalue()


def show_body(mid: str = "525", *, user_id: int = 88, text: str = "青岛栈桥", **extra) -> dict:
    payload = {
        "idstr": mid,
        "mid": mid,
        "mblogid": "Abc12",
        "created_at": CREATED,
        "text_raw": text,
        "attitudes_count": 1,
        "comments_count": 2,
        "reposts_count": 0,
        "pic_ids": ["pid-1"],
        "pic_infos": {
            "pid-1": {
                "type": "pic",
                "largest": {"url": "https://wx1.sinaimg.cn/large/pid-1.jpg"},
            }
        },
        "user": {"id": user_id},
    }
    payload.update(extra)
    return payload


def side_body(user_id: int = 88, **extra) -> dict:
    user = {
        "id": user_id,
        "screen_name": "海边",
        "followers_count": 10,
        "friends_count": 4,
        "statuses_count": 6,
        "description": "简介",
        "verified": True,
        "verified_type": 0,
        "verified_type_ext": 2,
        "verified_reason": "个人",
        "avatar_hd": "https://wx1.sinaimg.cn/avatar.jpg",
        "gender": "m",
        "location": "山东",
    }
    user.update(extra)
    return {"ok": 1, "data": {"user": user}}


class Locator:
    def __init__(self, page: "SearchPage", selector: str, index: int | None = None):
        self.page = page
        self.selector = selector
        self.index = index

    def _links(self):
        if "div.from a" not in self.selector or self.page.mid not in self.selector:
            return []
        return [self.page]

    async def count(self):
        return len(self._links())

    def nth(self, index: int):
        return Locator(self.page, self.selector, index)

    async def get_attribute(self, name: str):
        if name != "href" or not self._links():
            return None
        return self.page.href

    async def is_visible(self):
        return bool(self._links())

    async def click(self, timeout=None):
        self.page.clicks += 1
        detail = self.page.context.new_page(self.page.detail_url)
        detail.payloads = list(self.page.payloads_for(self.page.clicks))
        self.page.opened = detail


class SearchPage(FakePage):
    def __init__(self, dom: dict):
        super().__init__("https://s.weibo.com/weibo?q=qingdao&nodup=1")
        self.dom = dom
        self.mid = dom["cards"][0]["mid"] if dom.get("cards") else "525"
        self.href = "//weibo.com/88/Abc12"
        self.detail_url = "https://weibo.com/88/Abc12"
        self.clicks = 0
        self.opened = None
        self.context = None
        self.closed = False
        self._plans: list[list[dict]] = []

    def payloads_for(self, click_index: int) -> list[dict]:
        if not self._plans:
            return []
        index = min(click_index, len(self._plans)) - 1
        return list(self._plans[index])

    def plan(self, *rounds: list[dict]):
        self._plans = [list(round_) for round_ in rounds]

    def locator(self, selector: str):
        return Locator(self, selector)

    def is_closed(self):
        return self.closed

    async def wait_for_timeout(self, timeout: int):
        return None

    async def bring_to_front(self):
        return None

    async def evaluate(self, script: str, arg=None):
        if "feed_list_item" in script:
            return self.dom
        if "userAgent" in script:
            return "SyntheticAgent/1"
        if "$CONFIG" in script:
            return {"exists": True, "truthy": True}
        if "scrollHeight" in script:
            return True
        return {}


def _detail_load(page: FakePage):
    page.closed = False

    async def wait_for_load_state(state, timeout=None):
        for spec in list(getattr(page, "payloads", []) or []):
            request = page.request("GET", spec["url"], resource_type="xhr")
            page.respond(request, status=spec.get("status", 200), body=json.dumps(spec["json"]).encode())
        page.payloads = []

    async def wait_for_timeout(timeout):
        return None

    async def close():
        page.closed = True

    def is_closed():
        return page.closed

    page.wait_for_load_state = wait_for_load_state
    page.wait_for_timeout = wait_for_timeout
    page.close = close
    page.is_closed = is_closed
    return page


class Context(FakeContext):
    def new_page(self, url: str = "about:blank", *, page: FakePage | None = None) -> FakePage:
        opened = super().new_page(url, page=page)
        opened.url = url
        return _detail_load(opened)


def _candidate() -> WeiboSearchCandidate:
    return WeiboSearchCandidate(mid="525", uid="88", mblogid="Abc12")


def _bind(monkeypatch, tmp_path) -> tuple[SearchPage, WeiboPageCapture, object]:
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior.json"))
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH", raising=False)
    monkeypatch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")
    (tmp_path / "state.json").write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setattr(detail_mod.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(detail_mod, "DETAIL_RETRY_PAUSE_SECONDS", 0)
    monkeypatch.setattr(detail_mod, "DETAIL_RESPONSE_TIMEOUT_SECONDS", 0.3)
    dom = {
        "cards": [{"mid": "525", "uid": "88", "mblogid": "Abc12"}],
        "has_pager": False,
        "current_text": "",
        "page_info": "",
        "next_disabled": None,
        "max_page": None,
        "no_results": False,
    }
    page = SearchPage(dom)
    context = Context()
    page.context = context
    capture = WeiboPageCapture.open(context)
    capture.install(page)
    capture.begin_navigation()
    return page, capture, context


def _events(tmp_path: Path, kind: str) -> list[dict]:
    payload = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    return [event["details"] for event in payload["events"] if event["type"] == kind]


def _visits(tmp_path: Path) -> list[dict]:
    path = tmp_path / "behavior.content_visits.jsonl"
    return [
        item for item in (
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("{")
        )
        if item.get("id")
    ]


@pytest.mark.asyncio
async def test_click_captures_matching_show_and_side_user(monkeypatch, tmp_path):
    page, capture, _context = _bind(monkeypatch, tmp_path)
    page.plan([{
        "url": SHOW,
        "json": show_body(),
    }, {
        "url": "https://weibo.com/ajax/statuses/show?id=999",
        "json": show_body("999", user_id=7, text="其他"),
    }, {
        "url": SIDE,
        "json": side_body(),
    }, {
        "url": "https://weibo.com/ajax/side/cards/sideUser?id=7",
        "json": side_body(7, screen_name="别人"),
    }])
    # 不匹配的响应先到。
    page._plans[0] = [page._plans[0][1], page._plans[0][3], page._plans[0][0], page._plans[0][2]]
    try:
        opened = await WeiboDetailPages(capture, page).open_from_search(_candidate())
    finally:
        await capture.aclose()

    assert page.clicks == 1
    assert opened.record["note_id"] == "525"
    assert opened.record["nickname"] == "海边"
    assert opened.record["creator_hash"] == "88"
    assert "别人" not in json.dumps(opened.record)
    assert "其他" not in opened.record["content"]
    refs = opened.record["content_visit_refs"]
    visits = {line["id"]: line for line in _visits(tmp_path)}
    detail = visits[refs["note_detail"]]
    author = visits[refs["creator_profile"]]
    assert detail["rule"] == "weibo.statuses_show"
    assert detail["source"] == "page_response"
    assert detail["stage"] == "note_detail"
    assert detail["page_role"] == "detail"
    assert detail["subject_id"] == "525"
    assert author["rule"] == "weibo.side_user"
    assert author["stage"] == "creator_profile"
    assert author["page_role"] == "detail"
    assert author["subject_id"] == "88"
    assert author["trigger"] == detail["trigger"]
    trigger = visits[detail["trigger"]]
    assert trigger["action"] == "click_card"
    assert trigger["target"]["mid"] == "525"
    assert page.opened.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [(401, LOGIN_REQUIRED), (403, LOGIN_REQUIRED), (429, RATE_LIMITED), (418, BLOCKED_OR_FORBIDDEN)],
)
async def test_show_blocking_status_is_runtime(monkeypatch, tmp_path, status, code):
    page, capture, _context = _bind(monkeypatch, tmp_path)
    page.plan([{"url": SHOW, "status": status, "json": {"ok": 0}}])
    try:
        with pytest.raises(WeiboDetailFetchError) as exc_info:
            await WeiboDetailPages(capture, page).open_from_search(_candidate())
    finally:
        await capture.aclose()
    assert exc_info.value.runtime_blocking is True
    assert exc_info.value.code == code
    assert exc_info.value.attempts == 1
    assert page.clicks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,code",
    [
        ("https://passport.weibo.com/sso/signin", LOGIN_REQUIRED),
        ("https://weibo.com/visitor", LOGIN_REQUIRED),
        ("https://weibo.com/captcha", "captcha_detected"),
    ],
)
async def test_detail_tab_login_or_verify_is_runtime(monkeypatch, tmp_path, url, code):
    page, capture, _context = _bind(monkeypatch, tmp_path)
    page.detail_url = url
    page.plan([{"url": SHOW, "json": show_body()}])
    try:
        with pytest.raises(WeiboDetailFetchError) as exc_info:
            await WeiboDetailPages(capture, page).open_from_search(_candidate())
    finally:
        await capture.aclose()
    assert exc_info.value.runtime_blocking is True
    assert exc_info.value.code == code
    assert page.clicks == 1


@pytest.mark.asyncio
async def test_missing_show_or_side_user_retries_then_skips(monkeypatch, tmp_path):
    page, capture, _context = _bind(monkeypatch, tmp_path)
    page.plan([], [], [])
    crawler = make_crawler(settings(CRAWLER_MAX_SLEEP_SEC=0, ENABLE_GET_MEIDAS=False))
    crawler.context_page = page
    crawler._capture = capture
    crawler._opened_keyword = "青岛旅游"
    crawler.detail_pages = WeiboDetailPages(capture, page)
    try:
        await crawler.search()
    finally:
        await capture.aclose()
    skipped = _events(tmp_path, "candidate_skipped")
    assert skipped[-1]["error_code"] == DETAIL_SHOW_MISSING
    assert skipped[-1]["attempts"] == 3
    assert skipped[-1]["identity"] == "525"
    assert page.clicks == 3
    assert _events(tmp_path, "adaptive_search_stopped")[-1]["stop_detail"] == "single_page"


@pytest.mark.asyncio
async def test_missing_side_user_retries(monkeypatch, tmp_path):
    page, capture, _context = _bind(monkeypatch, tmp_path)
    page.plan(
        [{"url": SHOW, "json": show_body()}],
        [{"url": SHOW, "json": show_body()}],
        [{"url": SHOW, "json": show_body()}],
    )
    try:
        with pytest.raises(WeiboDetailFetchError) as exc_info:
            await WeiboDetailPages(capture, page).open_from_search(_candidate())
    finally:
        await capture.aclose()
    assert exc_info.value.code == AUTHOR_PROFILE_MISSING
    assert exc_info.value.attempts == 3
    assert exc_info.value.runtime_blocking is False


@pytest.mark.asyncio
async def test_missing_card_link_is_a_candidate_failure(monkeypatch, tmp_path):
    page, capture, _context = _bind(monkeypatch, tmp_path)
    page.mid = "other"
    try:
        with pytest.raises(WeiboDetailFetchError) as exc_info:
            await WeiboDetailPages(capture, page).open_from_search(_candidate())
    finally:
        await capture.aclose()
    assert exc_info.value.code == DETAIL_CARD_MISSING
    assert exc_info.value.attempts == 3


@pytest.mark.asyncio
async def test_empty_body_retries_and_bad_created_at_does_not(monkeypatch, tmp_path):
    page, capture, _context = _bind(monkeypatch, tmp_path)
    empty = show_body(text="  ")
    page.plan(
        [{"url": SHOW, "json": empty}, {"url": SIDE, "json": side_body()}],
        [{"url": SHOW, "json": empty}, {"url": SIDE, "json": side_body()}],
        [{"url": SHOW, "json": empty}, {"url": SIDE, "json": side_body()}],
    )
    try:
        with pytest.raises(WeiboDetailFetchError) as exc_info:
            await WeiboDetailPages(capture, page).open_from_search(_candidate())
    finally:
        await capture.aclose()
    assert exc_info.value.code == DETAIL_CONTENT_EMPTY
    assert exc_info.value.attempts == 3


@pytest.mark.asyncio
async def test_unparseable_created_at_is_not_retried(monkeypatch, tmp_path):
    page, capture, _context = _bind(monkeypatch, tmp_path)
    page.plan([{
        "url": SHOW,
        "json": show_body(**{"created_at": "not-a-date"}),
    }, {
        "url": SIDE,
        "json": side_body(),
    }])
    try:
        with pytest.raises(WeiboDetailFetchError) as exc_info:
            await WeiboDetailPages(capture, page).open_from_search(_candidate())
    finally:
        await capture.aclose()
    assert exc_info.value.code == CREATED_AT_UNPARSEABLE
    assert exc_info.value.attempts == 1
    assert page.clicks == 1


@pytest.mark.asyncio
async def test_search_writes_jsonl_manifest_and_cdn_ledger(monkeypatch, tmp_path):
    page, capture, _context = _bind(monkeypatch, tmp_path)
    page.plan([
        {"url": SHOW, "json": show_body(text="青岛栈桥一日")},
        {"url": SIDE, "json": side_body()},
    ])
    png = _png()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["referer"] == "https://weibo.com/"
        assert "cookie" not in {key.lower() for key in request.headers}
        return httpx.Response(200, content=png, request=request)

    crawler = make_crawler(settings(
        SAVE_DATA_PATH=str(tmp_path),
        ENABLE_GET_MEIDAS=True,
        CRAWLER_MAX_SLEEP_SEC=0,
    ))
    crawler.context_page = page
    crawler._capture = capture
    crawler._opened_keyword = "青岛旅游"
    crawler.source_keyword = "青岛旅游"
    crawler.detail_pages = WeiboDetailPages(capture, page)
    crawler.wb_client = detail_mod_client(crawler, handler)
    try:
        await crawler.search()
    finally:
        await capture.aclose()

    jsonl = next((tmp_path / "weibo" / "jsonl").glob("*.jsonl"))
    record = json.loads(jsonl.read_text(encoding="utf-8").splitlines()[0])
    validation = validate_formal_record(
        "weibo", record, set(), content_image_candidates=content_image_candidates,
    )
    assert validation["valid"] is True, validation["reasons"]
    manifest = [
        json.loads(line)
        for line in (tmp_path / "weibo" / "image_manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert manifest[0]["source_asset_key"] == "weibo:pid:pid-1"
    assert manifest[0]["fetch_status"] == "downloaded"
    images = [
        line for line in _visits(tmp_path)
        if line.get("kind") == "visit" and line.get("source") == "cdn_direct"
    ]
    assert images[0]["rule"] == "weibo.content_image"
    assert images[0]["asset_key"] == "weibo:pid:pid-1"
    assert images[0]["sha256"]
    assert record["content_visit_refs"]["images"] == [images[0]["id"]]
    assert "avatar" not in json.dumps(record)


def detail_mod_client(crawler, handler):
    from trippostcollect.platforms.weibo.client import WeiboClient

    ports = replace(
        crawler.ports.client,
        make_async_client=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return WeiboClient(user_agent="SyntheticAgent/1", ports=ports, timeout=5)


def test_repair_entry_is_unimplemented(monkeypatch, tmp_path):
    crawler = make_crawler(settings(CRAWLER_TYPE="detail"))
    with pytest.raises(WeiboRepairNotImplemented):
        import asyncio
        asyncio.run(crawler._run_after_browser_ready(None))
