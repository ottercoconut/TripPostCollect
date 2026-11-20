"""#222 抖音页面取数：登录、搜索账本、浮层详情、作者页。全部离线。"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import Any
from urllib.parse import quote

import pytest

from support.douyin import config, make_crawler as DouYinCrawler, refresh_settings
from trippostcollect.application.visit_gate import VISIT_GATED_PLATFORMS, VISIT_POLICIES, validate_content_visits
from trippostcollect.platforms.douyin import detail_page
from trippostcollect.platforms.douyin.detail_page import DouyinDetailFetchError, DouyinDetailPages
from trippostcollect.platforms.douyin.page_scripts import DOM_SCRIPTS, STATE_SCRIPTS
from trippostcollect.platforms.douyin.search_page import (
    DOUYIN_CAPTURE_RULES,
    DouyinPageCapture,
    blocking_code,
)
from trippostcollect.runtime.page_capture import PageCaptureChannel
from trippostcollect.runtime.visit_ledger import VisitLedger, load_ledger


T = "2026-11-20T00:00:00+00:00"
AWEME = "101"
UID = "author-1"
SEC = "MS4wLjABAAAAqingdao"


def image_aweme(aweme_id: str = AWEME, *, sec_uid: str = SEC, uid: str = UID) -> dict[str, Any]:
    return {
        "aweme_id": aweme_id,
        "aweme_type": 68,
        "desc": "青岛崂山图文",
        "create_time": 1_700_000_000,
        "author": {"uid": uid, "nickname": "作者", "sec_uid": sec_uid},
        "statistics": {"digg_count": 1, "collect_count": 2, "comment_count": 3, "share_count": 4},
        "images": [{"uri": f"uri-{aweme_id}", "url_list": [f"https://p3-pc-sign.douyinpic.com/{aweme_id}.jpeg"]}],
    }


def video_aweme(aweme_id: str = "202") -> dict[str, Any]:
    return {
        "aweme_id": aweme_id,
        "aweme_type": 0,
        "desc": "青岛视频",
        "create_time": 1_700_000_000,
        "author": {"uid": "video-author", "nickname": "视频", "sec_uid": "sec-video"},
    }


def profile_user(*, uid: str = UID, sec_uid: str = SEC, follower: int = 42) -> dict[str, Any]:
    return {
        "status_code": 0,
        "user": {
            "uid": uid,
            "sec_uid": sec_uid,
            "nickname": "作者",
            "follower_count": follower,
            "following_count": 3,
            "aweme_count": 8,
            "signature": "青岛",
        },
    }


class _Mouse:
    def __init__(self, page: Page) -> None:
        self.page = page

    async def move(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    async def wheel(self, *args: Any) -> None:
        del args


class CloseControl:
    def __init__(self, page: "Page") -> None:
        self.page = page

    async def click(self, timeout: int | None = None) -> None:
        del timeout
        self.page.clicks.append("close")
        if self.page.close_stuck:
            return
        if self.page.sidebar:
            self.page.sidebar = False
            return
        self.page.modal_id = ""


class Locator:
    def __init__(self, page: "Page", selector: str, index: int | None = None) -> None:
        self.page = page
        self.selector = selector
        self.index = index

    def _items(self) -> list[dict[str, Any]]:
        page = self.page
        if "waterfall_item_" in self.selector:
            aweme_id = self.selector.rsplit("_", 1)[-1]
            return [card for card in page.cards if card["id"] == aweme_id]
        if "video-avatar" in self.selector:
            return list(page.avatars)
        if "video-player-auto-play" in self.selector:
            return list(page.switches)
        if selector_is_author_link(self.selector):
            return list(page.author_links)
        if self.selector == '[data-e2e="feed-active-video"]':
            return [{"kind": "active", "box": page.active_box}] if page.active_box else []
        return []

    def _picked(self) -> list[dict[str, Any]]:
        items = self._items()
        if self.index is None:
            return items
        return items[self.index:self.index + 1] if self.index < len(items) else []

    async def count(self) -> int:
        return len(self._picked()) if self.index is not None else len(self._items())

    def nth(self, index: int) -> "Locator":
        return Locator(self.page, self.selector, index)

    @property
    def first(self) -> "Locator":
        return self.nth(0)

    async def is_visible(self) -> bool:
        picked = self._picked()
        if not picked:
            return False
        return bool(picked[0].get("visible", True))

    async def get_attribute(self, name: str) -> str | None:
        picked = self._picked()
        if not picked:
            return None
        if name == "href":
            return picked[0].get("href")
        return None

    async def bounding_box(self) -> dict[str, float] | None:
        picked = self._picked()
        if not picked:
            return None
        box = picked[0].get("box")
        return dict(box) if isinstance(box, dict) else None

    async def click(self, timeout: int | None = None) -> None:
        del timeout
        picked = self._picked()
        if not picked:
            raise AssertionError(self.selector)
        kind = picked[0].get("kind")
        self.page.clicks.append(kind or self.selector)
        if kind == "card":
            if self.page.open_modal:
                self.page.modal_id = picked[0]["id"]
                self.page.has_active = self.page.active_after_click
                self.page.autoplay_on = self.page.autoplay_starts_on
        elif kind == "switch":
            if not self.page.autoplay_stuck:
                self.page.autoplay_on = False
        elif kind == "avatar":
            self.page.sidebar = True
        elif kind == "author":
            self.page.context.spawn_author(picked[0])


def selector_is_author_link(selector: str) -> bool:
    return selector.startswith("a[target=")


class Page:
    def __init__(self, url: str = "https://www.douyin.com/") -> None:
        self.url = url
        self.logged_in = True
        self.risk: dict[str, Any] = {
            "hits": [],
            "verify_host": False,
            "captcha_visible": False,
            "login_panel_visible": False,
            "hidden_captcha_count": 1,
        }
        self.cards: list[dict[str, Any]] = []
        self.avatars: list[dict[str, Any]] = [{"kind": "avatar", "visible": True}]
        self.switches: list[dict[str, Any]] = [{"kind": "switch", "visible": True}]
        self.author_links: list[dict[str, Any]] = []
        self.active_box: dict[str, float] | None = {"x": 200, "y": 80, "width": 700, "height": 600}
        self.modal_id = ""
        self.has_active = False
        self.autoplay_on = False
        self.autoplay_starts_on = False
        self.autoplay_stuck = False
        self.active_after_click = True
        self.open_modal = True
        self.sidebar = False
        self.close_stuck = False
        self.close_missing = False
        self.advance_after_good_reads = 0
        self.good_reads = 0
        self.author_sec = SEC
        self.profiles: list[tuple[int, dict[str, Any]]] = []
        self.block_tab = False
        self.clicks: list[str] = []
        self.closed = False
        self.context: "Context | None" = None
        self.search_payload: dict[str, Any] | None = None
        self.search_url = ""
        self.gotos: list[str] = []
        self.viewport_size = {"width": 1450, "height": 900}
        self.mouse = _Mouse(self)
        self.listeners: dict[str, list] = {}
        self.fronts = 0

    def is_closed(self) -> bool:
        return self.closed

    def on(self, event: str, callback) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def locator(self, selector: str) -> Locator:
        return Locator(self, selector)

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        del arg
        if "HasUserLogin" in script:
            return {"logged_in": self.logged_in}
        if "modal_id" in script and "feed-active-video" in script:
            return self._modal_state()
        if "captcha" in script:
            return dict(self.risk)
        if "userAgent" in script:
            return "TestAgent/222"
        raise AssertionError(script[:80])

    def _modal_state(self) -> dict[str, Any]:
        modal_id = self.modal_id
        has_active = self.has_active
        if modal_id and has_active and modal_id != "999":
            self.good_reads += 1
            if self.advance_after_good_reads and self.good_reads >= self.advance_after_good_reads:
                self.modal_id = "999"
                modal_id = "999"
        return {
            "modal_id": modal_id,
            "has_active_video": bool(has_active and modal_id),
            "autoplay_on": self.autoplay_on,
        }

    async def evaluate_handle(self, script: str) -> CloseControl | None:
        del script
        if self.close_missing:
            return None
        return CloseControl(self)

    async def wait_for_timeout(self, timeout: int) -> None:
        del timeout

    async def bring_to_front(self) -> None:
        self.fronts += 1

    async def close(self) -> None:
        self.closed = True

    async def goto(self, url: str, **kwargs: Any) -> None:
        del kwargs
        self.url = url
        self.gotos.append(url)
        if self.search_payload is not None and "/search/" in url and self.search_url:
            self.emit(Response(self, self.search_payload, url=self.search_url))

    def emit(self, response: "Response") -> None:
        for callback in list(self.listeners.get("response", [])):
            callback(response)


class Response:
    def __init__(
        self,
        page: Page,
        payload: dict[str, Any] | bytes,
        *,
        status: int = 200,
        url: str | None = None,
    ) -> None:
        self.url = url or "https://www.douyin.com/aweme/v1/web/user/profile/other/?sec_user_id=target"
        self.status = status
        self.headers: dict[str, str] = {}
        self.request = type("Request", (), {
            "method": "GET",
            "resource_type": "fetch",
            "url": self.url,
            "frame": type("Frame", (), {"page": page})(),
            "post_data": None,
        })()
        if isinstance(payload, (bytes, bytearray)):
            self._body = bytes(payload)
        else:
            self._body = json.dumps(payload).encode()

    async def body(self) -> bytes:
        return self._body


class ExpectPage:
    def __init__(self, context: "Context") -> None:
        self.context = context
        self.page: Page | None = None

    async def __aenter__(self) -> "ExpectPage":
        self.context.pending = self
        return self

    async def __aexit__(self, *args) -> bool:
        self.context.pending = None
        return False

    @property
    def value(self):
        async def get() -> Page:
            if self.page is None:
                raise TimeoutError("author tab was not opened")
            return self.page
        return get()


class Context:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.pages = [page]
        self.pending: ExpectPage | None = None
        self._on_page = None
        page.context = self

    def on(self, event: str, callback) -> None:
        assert event == "page"
        self._on_page = callback

    def expect_page(self, timeout: int | None = None) -> ExpectPage:
        del timeout
        return ExpectPage(self)

    def spawn_author(self, link: dict[str, Any]) -> None:
        del link
        if self.page.block_tab:
            return
        author = Page(f"https://www.douyin.com/user/{self.page.author_sec}?from_tab_name=main")
        author.context = self
        self.pages.append(author)
        if self._on_page is not None:
            self._on_page(author)
        for status, payload in self.page.profiles:
            author.emit(Response(author, payload, status=status))
        if self.pending is not None:
            self.pending.page = author


def harness(tmp_path, page: Page | None = None):
    page = page or Page("https://www.douyin.com/search/qingdao?type=general")
    context = page.context or Context(page)
    ledger = VisitLedger.create(
        tmp_path / "content_visits.jsonl",
        platform="douyin",
        mode="discovery",
        clock=lambda: T,
    )
    channel = PageCaptureChannel(
        context,
        platform="douyin",
        rules=DOUYIN_CAPTURE_RULES,
        ledger=ledger,
        state_scripts=STATE_SCRIPTS,
        new_page_role="author",
        clock=lambda: T,
    )
    capture = DouyinPageCapture(channel)
    capture.install(page)
    pages = DouyinDetailPages(capture, page)
    return capture, pages, page


def lines_of(capture: DouyinPageCapture) -> list[dict[str, Any]]:
    return list(load_ledger(capture.channel.ledger.path).lines)


def test_policy_names_match_the_page_rules():
    policy = VISIT_POLICIES["douyin"]
    assert "douyin" in VISIT_GATED_PLATFORMS
    assert "repair" not in policy.chains
    assert policy.spec("discovery", "search_results").rule == "douyin.search_response"
    assert policy.spec("discovery", "search_results").source == "page_response"
    assert policy.spec("discovery", "note_detail").rule == "douyin.modal_state"
    assert policy.spec("discovery", "note_detail").source == "page_state"
    assert policy.spec("discovery", "note_detail").trigger == "click_card"
    assert policy.spec("discovery", "creator_profile").rule == "douyin.user_profile"
    assert policy.spec("discovery", "creator_profile").trigger == "click_author"
    assert policy.detail_page_role == "primary"
    assert policy.target_id_field == "aweme_id"
    assert "douyin.login_state" in STATE_SCRIPTS
    assert "douyin.user_agent" in STATE_SCRIPTS
    assert "douyin.modal_state" in STATE_SCRIPTS
    assert "fetch(" not in STATE_SCRIPTS["douyin.modal_state"]
    risk = DOM_SCRIPTS["douyin.visible_risk"]
    assert "display" in risk and "visibility" in risk
    assert "getBoundingClientRect" in risk and "innerHeight" in risk
    assert "XMLHttpRequest" not in risk and "fetch(" not in risk


def test_hidden_captcha_container_is_not_a_hit():
    hidden = {
        "hits": [],
        "verify_host": False,
        "captcha_visible": False,
        "login_panel_visible": False,
        "hidden_captcha_count": 2,
    }
    assert blocking_code(hidden) is None
    assert blocking_code({**hidden, "captcha_visible": True}) == "captcha_detected"
    assert blocking_code({**hidden, "verify_host": True}) == "captcha_detected"
    assert blocking_code({**hidden, "hits": ["验证码"]}) == "captcha_detected"
    assert blocking_code({**hidden, "hits": ["请完成下列验证"]}) == "captcha_detected"
    assert blocking_code({**hidden, "hits": ["操作过于频繁"]}) == "rate_limited"
    assert blocking_code({**hidden, "hits": ["请稍后再试"]}) == "rate_limited"
    assert blocking_code({**hidden, "login_panel_visible": True}) == "login_required"
    assert blocking_code({**hidden, "hits": ["验证码"], "login_panel_visible": True}) == "captcha_detected"


@pytest.mark.asyncio
async def test_login_state_uses_has_user_login_only(tmp_path):
    capture, _pages, page = harness(tmp_path)
    assert await capture.confirm_login(page) is None
    logged = [line for line in lines_of(capture) if line.get("stage") == "login_state"]
    assert logged[0]["source"] == "page_state"
    assert logged[0]["rule"] == "douyin.login_state"
    assert "HasUserLogin" not in json.dumps(logged[0])

    page.logged_in = False
    assert await capture.confirm_login(page) == "login_required"
    page.logged_in = True
    page.risk = {**page.risk, "captcha_visible": True}
    assert await capture.confirm_login(page) == "captcha_detected"


@pytest.mark.asyncio
async def test_search_ledger_records_each_page_and_skips_video_details(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setattr(config, "KEYWORDS", "青岛")
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    capture, pages, page = harness(tmp_path)
    opened: list[str] = []

    payload = {
        "status_code": 0,
        "data": [{"aweme_info": video_aweme()}, {"aweme_info": image_aweme()}],
        "has_more": 0,
        "extra": {"logid": "search-1"},
    }
    page.emit(Response(
        page,
        payload,
        url=f"https://www.douyin.com/aweme/v1/web/general/search/stream/?keyword={quote('青岛')}&offset=0",
    ))

    real_open = pages.open_from_search

    async def _open(aweme, *, search_visit_id: str = ""):
        opened.append(aweme["aweme_id"])
        return await real_open(aweme, search_visit_id=search_visit_id)

    pages.open_from_search = _open
    _arm_author(page)
    page.profiles = [
        (200, profile_user(uid="self-user", sec_uid="sec-self")),
        (200, profile_user()),
    ]
    crawler = DouYinCrawler()
    refresh_settings(crawler)
    crawler.capture = capture
    crawler.detail_pages = pages
    crawler.get_aweme_images = _async_none
    stored: list[dict[str, Any]] = []

    async def _store(aweme_item, **kwargs):
        del kwargs
        stored.append(aweme_item)

    crawler.update_douyin_aweme = _store
    await crawler.search()

    assert opened == [AWEME]
    assert [item["aweme_id"] for item in stored] == [AWEME]
    visits = [
        line for line in lines_of(capture)
        if line.get("stage") == "search_results" and line["kind"] == "visit"
    ]
    assert len(visits) == 1
    visit = visits[0]
    assert visit["source"] == "page_response"
    assert visit["rule"] == "douyin.search_response"
    assert visit["search_page"] == 1
    assert visit["is_target"] is True
    assert visit["has_more"] is False
    assert visit["search_session"]
    assert visit["status"] == 200
    assert {item["id"]: item["type"] for item in visit["items"]} == {AWEME: "image", "202": "video"}
    actions = [line["action"] for line in lines_of(capture) if line["kind"] == "action"]
    assert "navigate" in actions and "click_card" in actions
    assert "open_author_panel" in actions and "click_author" in actions
    assert actions.count("close_overlay") == 2
    record = {
        "aweme_id": AWEME,
        "creator_hash": UID,
        "content_visit_refs": stored[0]["content_visit_refs"],
        "image_assets": [],
    }
    result = validate_content_visits(
        [record],
        [capture.channel.ledger.path],
        platform="douyin",
        mode="discovery",
        manifests=[],
    )
    assert "R3" not in result["problem_counts"], result
    assert "R4" not in result["problem_counts"], result
    assert "R6" not in result["problem_counts"], result
    assert result["request_pacing_ok"] is True
    assert result["continuity_ok"] is True
    assert stored[0]["creator_profile"]["user"]["uid"] == UID


async def _async_none(*args, **kwargs):
    del args, kwargs


@pytest.mark.asyncio
async def test_modal_mismatch_and_missing_active_item_are_candidate_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(detail_page, "DETAIL_ATTEMPTS", 1)
    monkeypatch.setattr(detail_page, "MODAL_WAIT_SECONDS", 0)
    monkeypatch.setattr(detail_page, "DETAIL_RETRY_PAUSE_SECONDS", 0)
    _capture, pages, page = harness(tmp_path)
    page.cards = [{"kind": "card", "id": AWEME, "visible": True}]
    page.open_modal = False
    with pytest.raises(DouyinDetailFetchError) as raised:
        await pages.open_from_search(image_aweme())
    assert raised.value.code == "detail_modal_mismatch"
    assert raised.value.runtime_blocking is False

    page.open_modal = True
    page.active_after_click = False
    with pytest.raises(DouyinDetailFetchError) as raised:
        await pages.open_from_search(image_aweme())
    assert raised.value.code == "detail_modal_mismatch"


@pytest.mark.asyncio
async def test_card_missing_retries_then_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(detail_page, "DETAIL_RETRY_PAUSE_SECONDS", 0)
    _capture, pages, page = harness(tmp_path)
    page.cards = []
    with pytest.raises(DouyinDetailFetchError) as raised:
        await pages.open_from_search(image_aweme())
    assert raised.value.code == "detail_card_missing"
    assert raised.value.attempts == 3


@pytest.mark.asyncio
async def test_autoplay_is_turned_off_and_stuck_switch_blocks_the_run(tmp_path):
    capture, pages, page = harness(tmp_path)
    _arm_author(page)
    page.autoplay_starts_on = True
    opened = await pages.open_from_search(image_aweme())
    assert opened["creator_profile"]["user"]["follower_count"] == 42
    actions = [line["action"] for line in lines_of(capture) if line["kind"] == "action"]
    assert "toggle_autoplay" in actions
    assert page.autoplay_on is False

    page.autoplay_stuck = True
    page.autoplay_on = True
    page.modal_id = ""
    with pytest.raises(DouyinDetailFetchError) as raised:
        await pages.open_from_search(image_aweme())
    assert raised.value.code == "douyin_autoplay_still_on"
    assert raised.value.runtime_blocking is True


@pytest.mark.asyncio
async def test_modal_advanced_before_author_is_candidate_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(detail_page, "DETAIL_ATTEMPTS", 1)
    monkeypatch.setattr(detail_page, "DETAIL_RETRY_PAUSE_SECONDS", 0)
    _capture, pages, page = harness(tmp_path)
    page.cards = [{"kind": "card", "id": AWEME, "visible": True}]
    page.advance_after_good_reads = 2
    with pytest.raises(DouyinDetailFetchError) as raised:
        await pages.open_from_search(image_aweme())
    assert raised.value.code == "douyin_modal_advanced"
    assert "author" not in page.clicks


@pytest.mark.asyncio
async def test_overlay_close_failure_is_runtime(tmp_path):
    _capture, pages, page = harness(tmp_path)
    _arm_author(page)
    page.close_stuck = True
    with pytest.raises(DouyinDetailFetchError) as raised:
        await pages.open_from_search(image_aweme())
    assert raised.value.code == "douyin_modal_close_failed"
    assert raised.value.runtime_blocking is True
    assert page.clicks.count("close") == 3


@pytest.mark.asyncio
async def test_author_link_must_match_sec_uid_and_sit_in_the_sidebar(monkeypatch, tmp_path):
    monkeypatch.setattr(detail_page, "DETAIL_ATTEMPTS", 1)
    monkeypatch.setattr(detail_page, "DETAIL_RETRY_PAUSE_SECONDS", 0)
    _capture, pages, page = harness(tmp_path)
    page.cards = [{"kind": "card", "id": AWEME, "visible": True}]
    page.author_links = [{
        "kind": "author",
        "href": "https://www.douyin.com/user/self",
        "visible": True,
        "box": {"x": 1100, "y": 40, "width": 40, "height": 16},
    }, {
        "kind": "author",
        "href": f"https://www.douyin.com/user/{SEC}",
        "visible": True,
        "box": {"x": 220, "y": 400, "width": 80, "height": 20},
    }]
    with pytest.raises(DouyinDetailFetchError) as raised:
        await pages.open_from_search(image_aweme())
    assert raised.value.code == "author_link_missing"

    page.author_sec = "someone-else"
    page.author_links.append({
        "kind": "author",
        "href": f"https://www.douyin.com/user/{SEC}",
        "visible": True,
        "box": {"x": 1100, "y": 80, "width": 90, "height": 18},
    })
    with pytest.raises(DouyinDetailFetchError) as raised:
        await pages.open_from_search(image_aweme())
    assert raised.value.code == "author_url_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "code"),
    [
        (401, {"status_code": 0, "user": {"uid": UID, "sec_uid": SEC}}, "login_required"),
        (403, {"message": "Blocked by ArgusSecurityPlugin"}, "blocked_or_forbidden"),
        (403, b"<html>blocked</html>", "blocked_or_forbidden"),
        (429, {"status_code": 0}, "rate_limited"),
    ],
)
async def test_profile_http_status_is_runtime(tmp_path, status, payload, code):
    _capture, pages, page = harness(tmp_path)
    _arm_author(page)
    page.profiles = [(status, payload)]
    with pytest.raises(DouyinDetailFetchError) as raised:
        await pages.open_from_search(image_aweme())
    assert raised.value.runtime_blocking is True
    assert raised.value.code == code


@pytest.mark.asyncio
async def test_missing_profile_is_candidate_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(detail_page, "DETAIL_ATTEMPTS", 1)
    monkeypatch.setattr(detail_page, "AUTHOR_RESPONSE_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(detail_page, "DETAIL_RETRY_PAUSE_SECONDS", 0)
    _capture, pages, page = harness(tmp_path)
    _arm_author(page)
    page.profiles = []
    with pytest.raises(DouyinDetailFetchError) as raised:
        await pages.open_from_search(image_aweme())
    assert raised.value.code == "author_profile_missing"
    assert raised.value.runtime_blocking is False


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
ROUND_KEYWORD = "青岛崂山旅游攻略"


class _ImageClient:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self.calls = calls

    async def __aenter__(self) -> "_ImageClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    def stream(self, method: str, url: str, timeout: float, headers: dict[str, str]) -> "_ImageBody":
        del method, timeout
        self.calls.append({"url": url, "headers": dict(headers)})
        return _ImageBody(PNG)


class _ImageBody:
    def __init__(self, body: bytes) -> None:
        self.status_code = 200
        self._body = body

    async def __aenter__(self) -> "_ImageBody":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        yield self._body


class _Playwright:
    def __init__(self, context: Context) -> None:
        self._context = context

    async def __aenter__(self) -> "_Playwright":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    @property
    def chromium(self) -> "_Playwright":
        return self


def _round_page(keyword: str, payload: dict[str, Any]) -> Page:
    page = Page("https://www.douyin.com/")
    page.search_payload = payload
    page.search_url = (
        "https://www.douyin.com/aweme/v1/web/general/search/stream/"
        f"?keyword={quote(keyword)}&offset=0"
    )
    return page


def _prepare_round(monkeypatch: pytest.MonkeyPatch, tmp_path, page: Page):
    from trippostcollect.platforms.douyin import core as douyin_core

    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    evidence = tmp_path / "behavior.jsonl"
    evidence.write_text("", encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(evidence))
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "SAVE_LOGIN_STATE", True)
    monkeypatch.setattr(config, "ENABLE_CDP_MODE", False)
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_TYPE", "search")
    monkeypatch.setattr(config, "KEYWORDS", ROUND_KEYWORD)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "HEADLESS", True)
    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "jsonl")
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path / "out"))
    for name in (
        "DETAIL_DWELL_SECONDS",
        "AUTHOR_TAB_HOLD_SECONDS",
        "SEARCH_RESULT_PAUSE_SECONDS",
        "AUTHOR_CLICK_PAUSE_SECONDS",
    ):
        monkeypatch.setattr(detail_page, name, (0.0, 0.0))
    monkeypatch.setattr(douyin_core.random, "random", lambda: 0.0)
    context = Context(page)
    calls: list[dict[str, Any]] = []

    async def launch(*args: Any, **kwargs: Any) -> Context:
        del args, kwargs
        return context

    def factory(**kwargs: Any) -> _ImageClient:
        assert kwargs.get("proxy") is None
        return _ImageClient(calls)

    crawler = DouYinCrawler()
    crawler.ports = replace(
        crawler.ports,
        async_playwright=lambda: _Playwright(context),
        launch_profile_context=launch,
        client=replace(crawler.ports.client, make_async_client=factory),
    )
    return crawler, state_path, evidence, calls


def _events(state_path, kind: str) -> list[dict[str, Any]]:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    return [event["details"] for event in payload["events"] if event.get("type") == kind]


@pytest.mark.asyncio
async def test_round_trip_records_visit_chain_images_and_exhaustion(monkeypatch, tmp_path):
    """假浏览器跑通登录、搜索、浮层、作者、正文图和来源耗尽。"""

    image = image_aweme()
    video = video_aweme("202")
    page = _round_page(ROUND_KEYWORD, {
        "status_code": 0,
        "has_more": 0,
        "extra": {"logid": "round-log"},
        "data": [{"aweme_info": video}, {"aweme_info": image}],
    })
    _arm_author(page)
    crawler, state_path, evidence, calls = _prepare_round(monkeypatch, tmp_path, page)
    await crawler.start()

    records = []
    for path in (tmp_path / "out").rglob("*.jsonl"):
        if path.name == "image_manifest.jsonl":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    assert [item.get("aweme_id") for item in records] == [AWEME]
    record = records[0]
    assert record["content_detail_source"] == "search_aweme_info"
    assert record["content_detail_status"] == "detail_observed"
    assert record["creator_hash"] == UID
    assert record["image_assets"][0]["source_asset_key"].startswith("douyin:uri:")
    assert calls and "douyinpic.com" in calls[0]["url"]
    headers = {key.lower(): value for key, value in calls[0]["headers"].items()}
    assert headers["referer"] == "https://www.douyin.com/"
    assert headers["user-agent"] == "TestAgent/222"
    assert "cookie" not in headers
    assert headers["accept"].startswith("image/")

    ledger = evidence.with_name(f"{evidence.stem}.content_visits.jsonl")
    manifests = list((tmp_path / "out").rglob("image_manifest.jsonl"))
    assert manifests
    stopped = _events(state_path, "adaptive_search_stopped")
    assert stopped[-1]["stop_reason"] == "source_exhausted"
    assert stopped[-1]["stop_detail"] == "has_more_false"
    assert stopped[-1]["source_visit_id"]
    result = validate_content_visits(
        [record],
        [ledger],
        platform="douyin",
        mode="discovery",
        manifests=manifests,
        stop_event=stopped[-1],
    )
    assert result["content_visit_ok"] is True, result
    missing_images = json.loads(json.dumps(record))
    missing_images["content_visit_refs"]["images"] = []
    missing = validate_content_visits(
        [missing_images], [ledger], platform="douyin", mode="discovery",
        manifests=manifests, stop_event=stopped[-1],
    )
    assert missing["content_visit_ok"] is False and "R5" in missing["problem_counts"]
    swapped_author = json.loads(json.dumps(record))
    swapped_author["creator_hash"] = "other-author"
    swapped = validate_content_visits(
        [swapped_author], [ledger], platform="douyin", mode="discovery",
        manifests=manifests, stop_event=stopped[-1],
    )
    assert swapped["content_visit_ok"] is False and "R4" in swapped["problem_counts"]
    broken_ref = json.loads(json.dumps(record))
    broken_ref["content_visit_refs"]["note_detail"] = "missing-visit"
    broken = validate_content_visits(
        [broken_ref], [ledger], platform="douyin", mode="discovery",
        manifests=manifests, stop_event=stopped[-1],
    )
    assert broken["content_visit_ok"] is False and "R1" in broken["problem_counts"]
    search_items = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item.get("kind") == "visit" and item.get("stage") == "search_results":
            search_items = item.get("items") or []
    types = {item["id"]: item["type"] for item in search_items}
    assert types["202"] == "video"
    assert types[AWEME] == "image"
    assert "202" in stopped[-1]["candidate_identities"]
    assert AWEME in stopped[-1]["candidate_identities"]
    assert all(item.get("aweme_id") != "202" for item in records)


@pytest.mark.asyncio
async def test_round_stops_before_search_when_logged_out(monkeypatch, tmp_path):
    page = _round_page(ROUND_KEYWORD, {"status_code": 0, "data": [], "has_more": 0, "extra": {}})
    page.logged_in = False
    crawler, state_path, _evidence, calls = _prepare_round(monkeypatch, tmp_path, page)
    await crawler.start()
    assert all("/search/" not in url for url in page.gotos)
    assert calls == []
    stopped = _events(state_path, "adaptive_search_stopped")
    assert stopped[-1]["stop_detail"] == "login_required"
    assert stopped[-1]["stop_reason"] != "source_exhausted"


@pytest.mark.asyncio
async def test_round_stops_when_author_profile_is_forbidden(monkeypatch, tmp_path):
    page = _round_page(ROUND_KEYWORD, {
        "status_code": 0,
        "has_more": 1,
        "extra": {"logid": "round-log"},
        "data": [{"aweme_info": image_aweme()}],
    })
    _arm_author(page)
    page.profiles = [(403, b"<html>blocked</html>")]
    crawler, state_path, _evidence, calls = _prepare_round(monkeypatch, tmp_path, page)
    await crawler.start()
    assert calls == []
    stopped = _events(state_path, "adaptive_search_stopped")
    assert stopped[-1]["stop_reason"] == "runtime_failed"
    assert stopped[-1]["stop_detail"] == "blocked_or_forbidden"


def _arm_author(page: Page) -> None:
    page.cards = [{"kind": "card", "id": AWEME, "visible": True}]
    page.author_links = [{
        "kind": "author",
        "href": f"https://www.douyin.com/user/{SEC}",
        "visible": True,
        "box": {"x": 1100, "y": 70, "width": 80, "height": 20},
    }]
    page.profiles = [(200, profile_user())]
