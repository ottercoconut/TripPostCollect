"""#211 微博桌面搜索：假页面、假 locator、假账本。不访问平台、不启动浏览器、不写真实库。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace

import pytest
from playwright._impl._driver import compute_driver_executable

from support.weibo_adapter import crawler as make_crawler
from support.weibo_adapter import settings
from trippostcollect.platforms.weibo import detail_page as detail_mod
from trippostcollect.platforms.weibo.detail_page import WeiboDetailPages
from trippostcollect.platforms.weibo.page_scripts import DOM_SCRIPTS, STATE_SCRIPTS
from trippostcollect.platforms.weibo.search_page import (
    FILTER_SIGNATURE,
    HOME_URL,
    WeiboPageCapture,
    WeiboSearchPageInvalid,
    classify_search_page,
    search_url,
)


NEXT = "div.m-page2 a.next"
NODE_RUNNER = r"""
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
function element(attrs, text, queries) {
  const className = attrs.class || "";
  return {
    getAttribute: (key) => (Object.prototype.hasOwnProperty.call(attrs, key) ? attrs[key] : null),
    hasAttribute: (key) => Object.prototype.hasOwnProperty.call(attrs, key),
    className,
    classList: { contains: (name) => className.split(/\s+/).includes(name) },
    textContent: text,
    querySelector: (sel) => queries[sel] || null,
    querySelectorAll: (sel) => queries[sel] || [],
  };
}
const uidResults = input.uidCases.map((item) => {
  global.window = { $CONFIG: item.config };
  const value = eval("(" + input.uid + ")")();
  return { value, leaked: JSON.stringify(value).includes("secret-uid") };
});
const ua = (function () {
  const navigator = { userAgent: "SyntheticAgent/1" };
  return eval("(" + input.ua + ")")();
})();
const link = element({ href: "//weibo.com/88/Abc12?refer=1" }, "", {});
const card = element(
  { mid: "525", class: "card-wrap", "action-type": "feed_list_item" },
  "",
  { "div.from a": [link] },
);
const cur = element({}, "2", {});
const info = element({}, "共50页/500条", {});
const next = element({ class: "next disabled", href: "/x" }, "下一页", {});
const pageLink = element({}, "50", {});
const pager = element({ class: "m-page2" }, "", {
  "li.cur": cur,
  "span.page-info": info,
  "a.next": next,
  "a, li": [cur, next, pageLink],
});
global.document = {
  querySelectorAll: (sel) => (
    sel === 'div.card-wrap[action-type="feed_list_item"][mid]' ? [card] : []
  ),
  querySelector: (sel) => (sel === "div.m-page2" ? pager : null),
  body: { innerText: "其他文字找到0条结果" },
  scrollingElement: { scrollTop: 10, clientHeight: 20, scrollHeight: 100 },
};
const search = eval("(" + input.cards + ")")();
const bottom = eval("(" + input.bottom + ")")();
process.stdout.write(JSON.stringify({ uidResults, ua, search, bottom }));
"""


def run_scripts() -> dict:
    node, _cli = compute_driver_executable()
    payload = {
        "uid": STATE_SCRIPTS["weibo.config_uid"],
        "ua": STATE_SCRIPTS["weibo.user_agent"],
        "cards": DOM_SCRIPTS["weibo.search_cards"],
        "bottom": DOM_SCRIPTS["weibo.at_bottom"],
        "uidCases": [
            {"config": {"uid": "secret-uid"}},
            {"config": {"uid": 0}},
            {"config": {"uid": ""}},
            {"config": {"uid": "0"}},
            {"config": {}},
        ],
    }
    completed = subprocess.run(
        [str(node), "-e", NODE_RUNNER],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env={},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_page_scripts_read_uid_flag_cards_and_pager():
    result = run_scripts()

    assert result["uidResults"] == [
        {"value": {"exists": True, "truthy": True}, "leaked": False},
        {"value": {"exists": True, "truthy": False}, "leaked": False},
        {"value": {"exists": True, "truthy": False}, "leaked": False},
        {"value": {"exists": True, "truthy": True}, "leaked": False},
        {"value": {"exists": False, "truthy": False}, "leaked": False},
    ]
    assert result["ua"] == "SyntheticAgent/1"
    assert result["bottom"] is False
    assert result["search"]["cards"] == [{"mid": "525", "uid": "88", "mblogid": "Abc12"}]
    assert result["search"]["current_text"] == "2"
    assert result["search"]["page_info"] == "共50页/500条"
    assert result["search"]["next_disabled"] is True
    assert result["search"]["max_page"] == 50
    assert result["search"]["has_pager"] is True
    assert result["search"]["no_results"] is True
    assert "secret-uid" not in json.dumps(result["search"])


def test_search_url_is_desktop_nodup_without_a_page_number():
    url = search_url("青岛旅游")

    assert url.startswith("https://s.weibo.com/weibo?q=")
    assert url.endswith("&nodup=1")
    assert "page=" not in url


def card(mid: str, *, uid: str = "88", mblogid: str = "Abc") -> dict:
    return {"mid": mid, "uid": uid, "mblogid": mblogid}


def dom_page(
    cards: list,
    *,
    current: str = "1",
    info: str = "共2页/20条",
    has_pager: bool = True,
    next_disabled: bool | None = False,
    max_page: int | None = 2,
    no_results: bool = False,
) -> dict:
    return {
        "cards": cards,
        "has_pager": has_pager,
        "current_text": current,
        "page_info": info,
        "next_disabled": next_disabled,
        "max_page": max_page,
        "no_results": no_results,
    }


class Locator:
    def __init__(self, page: Page, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> Locator:
        return self

    async def count(self) -> int:
        return 1 if self.page.visible.get(self.selector) else 0

    async def is_visible(self) -> bool:
        return bool(self.page.visible.get(self.selector))

    async def click(self) -> None:
        self.page.trace.append(("click", self.selector))
        if self.selector == NEXT:
            self.page.advance()


class Navigation:
    async def __aenter__(self) -> Navigation:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class Page:
    def __init__(self, states: list[dict]) -> None:
        self.states = states or [dom_page([])]
        self.index = 0
        self.url = HOME_URL
        self.visible: dict[str, bool] = {}
        self.redirect = ""
        self.freeze_advance = False
        self.trace: list[tuple] = []
        self.gotos: list[str] = []
        self.uid = {"exists": True, "truthy": True}
        self.context = None

    def on(self, event: str, callback) -> None:
        return None

    def locator(self, selector: str) -> Locator:
        return Locator(self, selector)

    def expect_navigation(self, **kwargs) -> Navigation:
        return Navigation()

    async def evaluate(self, script: str, arg=None):
        if "feed_list_item" in script:
            return self.states[self.index]
        if "$CONFIG" in script:
            return self.uid
        if "userAgent" in script:
            return "TestAgent"
        if "scrollHeight" in script:
            return True
        return {}

    async def wait_for_timeout(self, timeout: int) -> None:
        return None

    async def goto(self, url: str, **kwargs) -> None:
        self.gotos.append(url)
        if url == HOME_URL and self.redirect:
            self.url = self.redirect
        else:
            self.url = url

    def advance(self) -> None:
        if self.freeze_advance:
            return
        if self.index + 1 < len(self.states):
            self.index += 1
        self.url = f"https://s.weibo.com/weibo?q=x&nodup=1&page={self.index + 1}"


class Context:
    def __init__(self, page: Page) -> None:
        self.pages = [page]
        page.context = self

    def on(self, event: str, callback) -> None:
        assert event == "page"


def events_of(state_path, kind: str) -> list[dict]:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    return [event["details"] for event in payload["events"] if event["type"] == kind]


def ledger_lines(tmp_path) -> list[dict]:
    path = tmp_path / "behavior.content_visits.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def install_scroll(monkeypatch, page: Page) -> None:
    async def fake_scroll(target, profile, **kwargs):
        target.trace.append(("scroll", profile.name, kwargs.get("intent")))

    monkeypatch.setattr(
        "trippostcollect.platforms.weibo.search_page.human_scroll",
        fake_scroll,
    )


def bind_state(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior.json"))
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH", raising=False)
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "4")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "1")
    return state_path


def prepare(monkeypatch, tmp_path, states: list[dict], *, keyword: str = "青岛旅游"):
    state_path = bind_state(monkeypatch, tmp_path)
    page = Page(states)
    page.url = "https://s.weibo.com/weibo?q=qingdao&nodup=1"
    context = Context(page)
    crawler = make_crawler(settings(KEYWORDS=keyword, START_PAGE=9, CRAWLER_MAX_SLEEP_SEC=0))
    capture = WeiboPageCapture.open(context)
    capture.install(page)
    capture.begin_navigation()
    crawler.context_page = page
    crawler.browser_context = context
    crawler._capture = capture
    crawler._opened_keyword = keyword
    opened: list[str] = []

    async def fake_open(candidate):
        opened.append(candidate.mid)

    crawler.detail_pages = WeiboDetailPages(capture, page)
    crawler.detail_pages.open_from_search = fake_open
    return crawler, page, state_path, opened, capture


async def search(monkeypatch, tmp_path, states):
    crawler, page, state_path, opened, capture = prepare(monkeypatch, tmp_path, states)
    await install_scroll(monkeypatch, page)
    try:
        await crawler.search()
    finally:
        await capture.aclose()
    return crawler, page, state_path, opened


@pytest.mark.asyncio
async def test_logged_in_homepage_opens_desktop_search(monkeypatch, tmp_path):
    state_path = bind_state(monkeypatch, tmp_path)
    page = Page([dom_page([])])
    context = Context(page)
    crawler = make_crawler(settings())
    crawler.context_page = page
    crawler.browser_context = context
    seen = {}

    async def behavior(target, platform):
        seen["behavior"] = (target.url, platform)

    async def client(_proxy):
        seen["client"] = True
        return None

    crawler.ports = replace(crawler.ports, run_required_human_behavior=behavior)
    crawler.create_weibo_client = client
    crawler.search = _async_marker(seen, "search")
    try:
        await crawler._run_after_browser_ready(None)
    finally:
        await crawler._close_capture()

    assert page.gotos[0] == HOME_URL
    assert page.gotos[1] == search_url("青岛旅游")
    assert "m.weibo.cn" not in "".join(page.gotos)
    assert seen["behavior"][1] == "weibo"
    assert seen["behavior"][0].startswith("https://s.weibo.com/weibo?")
    assert seen["search"] is True
    visits = [line for line in ledger_lines(tmp_path) if line.get("rule") == "weibo.config_uid"]
    assert visits[0]["source"] == "page_state" and visits[0]["stage"] == "login_state"
    assert events_of(state_path, "adaptive_search_stopped") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uid,redirect",
    [
        ({"exists": True, "truthy": False}, ""),
        ({"exists": False, "truthy": False}, ""),
        ({"exists": True, "truthy": True}, "https://passport.weibo.com/sso/signin?entry=miniblog"),
        ({"exists": True, "truthy": True}, "https://passport.weibo.com/visitor/visitor"),
        ({"exists": True, "truthy": True}, "https://weibo.com/visitor"),
    ],
)
async def test_logged_out_passport_or_visitor_does_not_search(monkeypatch, tmp_path, uid, redirect):
    state_path = bind_state(monkeypatch, tmp_path)
    page = Page([dom_page([])])
    page.uid = uid
    page.redirect = redirect
    context = Context(page)
    crawler = make_crawler(settings())
    crawler.context_page = page
    crawler.browser_context = context
    crawler.search = _async_fail("search must not start")
    try:
        await crawler._run_after_browser_ready(None)
    finally:
        await crawler._close_capture()

    assert page.gotos == [HOME_URL]
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [
        ("runtime_failed", "login_required"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector,code",
    [("text=安全验证", "captcha_detected"), ("text=访问频繁", "rate_limited")],
)
async def test_visible_verification_or_rate_limit_stops_before_search(monkeypatch, tmp_path, selector, code):
    state_path = bind_state(monkeypatch, tmp_path)
    page = Page([dom_page([])])
    page.visible[selector] = True
    context = Context(page)
    crawler = make_crawler(settings())
    crawler.context_page = page
    crawler.browser_context = context
    crawler.search = _async_fail("search must not start")
    try:
        await crawler._run_after_browser_ready(None)
    finally:
        await crawler._close_capture()

    assert page.gotos == [HOME_URL]
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert stopped[-1]["stop_detail"] == code


def _async_marker(sink: dict, key: str):
    async def marker():
        sink[key] = True

    return marker


def _async_fail(message: str):
    async def marker():
        raise AssertionError(message)

    return marker


@pytest.mark.asyncio
async def test_next_click_advances_and_disabled_last_page_sets_page_cap(monkeypatch, tmp_path):
    states = [
        dom_page([card("11")], current="1", next_disabled=False, max_page=50, info="共50页/500条"),
        dom_page([card("22")], current="2", next_disabled=True, max_page=50, info="共50页/500条"),
    ]
    _crawler, page, state_path, opened = await search(monkeypatch, tmp_path, states)

    assert opened == ["11", "22"]
    assert page.gotos == []
    assert ("scroll", "social_high_risk", "search_page") in page.trace
    assert page.trace.index(("scroll", "social_high_risk", "search_page")) < page.trace.index(("click", NEXT))
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert stopped[-1]["stop_reason"] == "source_exhausted"
    assert stopped[-1]["stop_detail"] == "next_disabled"
    assert stopped[-1]["source_page_info"] == "共50页/500条"
    assert stopped[-1]["source_max_page"] == 50
    assert stopped[-1]["page_cap_reached"] is True
    assert stopped[-1]["source_page"] == 2
    visits = [
        line for line in ledger_lines(tmp_path)
        if line.get("kind") == "visit" and line.get("stage") == "search_results"
    ]
    assert [line["search_page"] for line in visits] == [1, 2]
    assert visits[0]["source"] == "page_state" and visits[0]["rule"] == "weibo.search_cards"
    assert visits[0]["is_target"] is True and visits[0]["has_more"] is True
    assert visits[1]["is_target"] is True and visits[1]["has_more"] is False
    assert visits[0]["search_session"] == visits[1]["search_session"]
    assert visits[0]["filter_signature"] == FILTER_SIGNATURE
    assert stopped[-1]["source_visit_id"] == visits[1]["id"]
    assert ("pause", "search_page") in [
        (line.get("kind"), line.get("stage")) for line in ledger_lines(tmp_path)
    ]
    batches = events_of(state_path, "adaptive_batch_completed")
    assert batches and all(item["stagnation_basis"] == "candidate_identity" for item in batches)


@pytest.mark.asyncio
async def test_single_page_and_no_results_exhaust_without_clicking(monkeypatch, tmp_path):
    _crawler, page, state_path, opened = await search(
        monkeypatch,
        tmp_path,
        [dom_page([card("11")], has_pager=False, next_disabled=None, max_page=None, info="", current="")],
    )
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert opened == ["11"]
    assert page.trace == []
    assert stopped[-1]["stop_detail"] == "single_page"
    assert stopped[-1]["page_cap_reached"] is False
    assert stopped[-1]["source_max_page"] is None


@pytest.mark.asyncio
async def test_no_results_stops_without_opening_details(monkeypatch, tmp_path):
    _crawler, page, state_path, opened = await search(
        monkeypatch,
        tmp_path,
        [dom_page([], no_results=True, has_pager=False, next_disabled=None, max_page=None, info="", current="")],
    )

    assert opened == []
    assert page.trace == []
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert stopped[-1]["stop_detail"] == "no_results"
    assert stopped[-1]["page_cap_reached"] is False
    assert stopped[-1]["raw_batch_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second",
    [
        dom_page([card("22")], has_pager=False, current="", next_disabled=None, max_page=None, info=""),
        dom_page([], current="2", next_disabled=True, no_results=True),
    ],
)
async def test_later_page_without_pager_or_cards_is_invalid(monkeypatch, tmp_path, second):
    _crawler, page, state_path, opened = await search(
        monkeypatch,
        tmp_path,
        [dom_page([card("11")], next_disabled=False), second],
    )

    assert opened == ["11"]
    assert ("click", NEXT) in page.trace
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert stopped[-1]["stop_reason"] == "runtime_failed"
    assert stopped[-1]["stop_detail"] == "search_page_invalid"


@pytest.mark.asyncio
async def test_click_that_does_not_advance_is_invalid(monkeypatch, tmp_path):
    crawler, page, state_path, opened, capture = prepare(
        monkeypatch,
        tmp_path,
        [
            dom_page([card("11")], next_disabled=False),
            dom_page([card("22")], current="2", next_disabled=True),
        ],
    )
    page.freeze_advance = True
    await install_scroll(monkeypatch, page)
    try:
        await crawler.search()
    finally:
        await capture.aclose()

    assert opened == ["11"]
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert stopped[-1]["stop_detail"] == "search_page_invalid"


@pytest.mark.asyncio
async def test_non_numeric_mid_is_invalid_before_detail(monkeypatch, tmp_path):
    _crawler, _page, state_path, opened = await search(
        monkeypatch,
        tmp_path,
        [dom_page([{"mid": "abc", "uid": "88", "mblogid": "Abc"}])],
    )

    assert opened == []
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert stopped[-1]["stop_detail"] == "search_page_invalid"
    assert events_of(state_path, "candidate_skipped") == []


@pytest.mark.asyncio
async def test_known_ids_are_skipped_before_detail(monkeypatch, tmp_path):
    database = tmp_path / "posts.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        connection.execute("INSERT INTO web_posts VALUES ('weibo', '100', NULL)")
    resume = tmp_path / "resume.json"
    resume.write_text(json.dumps(["200"]), encoding="utf-8")
    crawler, _page, state_path, opened, capture = prepare(
        monkeypatch,
        tmp_path,
        [dom_page(
            [card("100"), card("200"), card("300"), card("300")],
            has_pager=False,
            next_disabled=None,
            max_page=None,
            info="",
            current="",
        )],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(database))
    monkeypatch.setenv("TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH", str(resume))
    await install_scroll(monkeypatch, _page)
    try:
        await crawler.search()
    finally:
        await capture.aclose()

    assert opened == ["300"]
    assert events_of(state_path, "adaptive_search_stopped")[-1]["stop_detail"] == "single_page"


@pytest.mark.asyncio
async def test_missing_time_link_is_skipped_without_mobile_detail(monkeypatch, tmp_path):
    monkeypatch.setattr(detail_mod, "DETAIL_RETRY_PAUSE_SECONDS", 0)
    monkeypatch.setattr(detail_mod.random, "uniform", lambda a, b: 0.0)
    crawler, page, state_path, _opened, capture = prepare(
        monkeypatch,
        tmp_path,
        [dom_page([card("11")], has_pager=False, next_disabled=None, max_page=None, info="", current="")],
    )
    crawler.detail_pages = WeiboDetailPages(capture, page)
    await install_scroll(monkeypatch, page)
    try:
        await crawler.search()
    finally:
        await capture.aclose()

    skipped = events_of(state_path, "candidate_skipped")
    assert skipped[-1]["identity"] == "11"
    assert skipped[-1]["error_code"] == "detail_card_missing"
    assert page.gotos == []
    assert "m.weibo.cn" not in page.url


def test_first_page_without_zero_result_text_is_invalid():
    with pytest.raises(WeiboSearchPageInvalid):
        classify_search_page(
            dom_page([], no_results=False, has_pager=False, next_disabled=None, current="", info="", max_page=None),
            page_number=1,
        )
