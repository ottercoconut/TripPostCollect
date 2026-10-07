from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from trippostcollect.platforms.xhs.errors import PlatformRuntimeError
from trippostcollect.platforms.xhs.login import XiaoHongShuLogin

from .support import XiaoHongShuCrawler, config, login_ports


class FakePage:
    def __init__(self, name: str, events: list[tuple[str, object]]):
        self.name = name
        self.url = f"https://www.xiaohongshu.com/{name}"
        self.closed = False
        self.events = events

    def is_closed(self) -> bool:
        return self.closed

    async def bring_to_front(self) -> None:
        self.events.append(("front", self.name))

    async def close(self) -> None:
        self.events.append(("close", self.name))
        self.closed = True


class FakeContext:
    def __init__(self, pages: list[FakePage], events: list[tuple[str, object]]):
        self.pages = pages
        self.events = events
        self.page_handler = None
        self.closed = False

    def on(self, event: str, handler) -> None:
        assert event == "page"
        self.page_handler = handler

    def emit_page(self, page: FakePage) -> None:
        self.pages.append(page)
        assert self.page_handler is not None
        self.page_handler(page)

    async def new_page(self) -> FakePage:
        page = FakePage("crawler-owned", self.events)
        self.emit_page(page)
        return page

    async def close(self) -> None:
        self.events.append(("context_close", "context"))
        self.closed = True


def install_fake_clock(crawler: XiaoHongShuCrawler, events: list[tuple[str, object]]):
    clock = {"now": 100.0}

    def monotonic() -> float:
        return clock["now"]

    async def sleep(seconds: float) -> None:
        events.append(("sleep", seconds))
        clock["now"] += seconds

    crawler._popup_monotonic = monotonic
    crawler._popup_sleep = sleep


@pytest.mark.asyncio
async def test_unexpected_xhs_tab_is_held_and_preserved_for_login() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    popup = FakePage("security-popup", events)
    context = FakeContext([primary], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = primary
    crawler._install_new_page_guard()

    context.emit_page(popup)
    selected = await crawler._single_page_for_login()
    await asyncio.sleep(0)

    assert selected is primary
    assert crawler.context_page is primary
    assert crawler._new_pages[id(popup)][2] == "platform_opened"
    assert popup.closed is False
    assert events[0] == ("front", "security-popup")
    assert events[1][0] == "sleep"
    assert events[1][1] == pytest.approx(30.0)
    assert len(events) == 2


@pytest.mark.asyncio
async def test_extra_tab_present_at_guard_install_is_also_protected() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    popup = FakePage("startup-security-popup", events)
    context = FakeContext([primary, popup], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = primary

    crawler._install_new_page_guard()
    await crawler._single_page_for_login()
    await asyncio.sleep(0)

    assert crawler._new_pages[id(popup)][2] == "preexisting_extra"
    assert events == [
        ("front", "startup-security-popup"),
        ("sleep", pytest.approx(30.0)),
    ]
    assert popup.closed is False


@pytest.mark.asyncio
async def test_crawler_opened_xhs_tab_waits_30_seconds_after_failed_scroll() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    context = FakeContext([primary], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = primary
    crawler._install_new_page_guard()

    page = await crawler._new_guarded_page()
    events.append(("scroll_effect", False))
    await crawler._close_page_with_deadline(page, reason="creator_profile_cleanup")

    assert crawler._new_pages[id(page)][2] == "crawler_opened"
    scroll_index = events.index(("scroll_effect", False))
    sleep_index = next(index for index, event in enumerate(events) if event[0] == "sleep")
    close_index = events.index(("close", "crawler-owned"))
    assert events[sleep_index][1] == pytest.approx(30.0)
    assert scroll_index < sleep_index < close_index
    assert page.closed is True


@pytest.mark.asyncio
async def test_xhs_context_cleanup_waits_for_unexpected_tab() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    popup = FakePage("captcha-popup", events)
    context = FakeContext([primary], events)
    crawler = XiaoHongShuCrawler()
    install_fake_clock(crawler, events)
    crawler.browser_context = context
    crawler.context_page = primary
    crawler._install_new_page_guard()

    context.emit_page(popup)
    await crawler.close()

    sleep_index = next(index for index, event in enumerate(events) if event[0] == "sleep")
    close_index = events.index(("context_close", "context"))
    assert events[sleep_index][1] == pytest.approx(30.0)
    assert sleep_index < close_index
    assert events.index(("close", "captcha-popup")) < close_index


@pytest.mark.asyncio
async def test_xhs_behavior_primary_page_close_is_terminal_without_adoption(
    monkeypatch,
) -> None:
    events: list[tuple[str, object]] = []
    original = FakePage("search", events)
    replacement = FakePage("replacement", events)
    context = FakeContext([original, replacement], events)
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = context
    crawler.context_page = original
    original.closed = True
    calls: list[FakePage] = []
    lifecycle_stages: list[str] = []

    async def run_behavior(page, platform_key):
        calls.append(page)
        raise RuntimeError(
            "human_behavior_failed:xhs:TargetClosedError: "
            "Target page, context or browser has been closed"
        )

    monkeypatch.setattr(
        crawler.ports,
        "run_required_human_behavior",
        run_behavior,
    )
    crawler.xhs_client = SimpleNamespace(playwright_page=original)
    crawler.cdp_manager = SimpleNamespace(assert_alive=lifecycle_stages.append)
    crawler.launch_browser_with_cdp = AsyncMock(
        side_effect=AssertionError("must not relaunch Chrome")
    )

    with pytest.raises(RuntimeError) as exc_info:
        await crawler._run_human_behavior_on_primary_page("青岛旅游")

    assert str(exc_info.value) == "xhs_main_page_closed_unexpected:stage=behavior"
    assert calls == [original]
    assert lifecycle_stages == ["behavior_primary_page_closed"]
    assert crawler.context_page is original
    assert crawler.xhs_client.playwright_page is original
    assert replacement.closed is False
    crawler.launch_browser_with_cdp.assert_not_awaited()


@pytest.mark.asyncio
async def test_xhs_search_navigation_primary_page_close_is_terminal_without_retry() -> None:
    events: list[tuple[str, object]] = []
    original = FakePage("search", events)
    replacement = FakePage("replacement", events)
    context = FakeContext([original, replacement], events)
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = context
    crawler.context_page = original
    crawler.xhs_client = SimpleNamespace(playwright_page=original)
    crawler._open_behavior_search_page = AsyncMock(
        side_effect=RuntimeError(
            "TargetClosedError: Target page, context or browser has been closed"
        )
    )
    crawler.launch_browser_with_cdp = AsyncMock(
        side_effect=AssertionError("must not relaunch Chrome")
    )

    with pytest.raises(RuntimeError) as exc_info:
        await crawler._open_behavior_search_page_on_primary_page("青岛旅游")

    assert str(exc_info.value) == (
        "xhs_main_page_closed_unexpected:stage=search_navigation"
    )
    crawler._open_behavior_search_page.assert_awaited_once_with("青岛旅游")
    assert crawler.context_page is original
    assert crawler.xhs_client.playwright_page is original
    assert replacement.closed is False
    crawler.launch_browser_with_cdp.assert_not_awaited()


@pytest.mark.asyncio
async def test_xhs_search_navigation_network_error_is_not_mislabeled_page_close() -> None:
    events: list[tuple[str, object]] = []
    primary = FakePage("search", events)
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = FakeContext([primary], events)
    crawler.context_page = primary
    network_error = RuntimeError("net::ERR_INTERNET_DISCONNECTED")
    crawler._open_behavior_search_page = AsyncMock(side_effect=network_error)
    crawler._assert_cdp_lifecycle_alive = AsyncMock(
        side_effect=AssertionError("network errors are not page-close lifecycle events")
    )

    with pytest.raises(RuntimeError) as exc_info:
        await crawler._open_behavior_search_page_on_primary_page("青岛旅游")

    assert exc_info.value is network_error
    crawler._open_behavior_search_page.assert_awaited_once_with("青岛旅游")
    crawler._assert_cdp_lifecycle_alive.assert_not_awaited()


@pytest.mark.asyncio
async def test_standalone_xhs_login_preserves_extra_verification_tab(
    monkeypatch,
) -> None:
    events: list[tuple[str, object]] = []

    async def sleep(seconds: float) -> None:
        events.append(("sleep", seconds))

    monkeypatch.setattr("trippostcollect.platforms.xhs.login.asyncio.sleep", sleep)
    primary = FakePage("login", events)
    popup = FakePage("verification", events)
    context = FakeContext([primary, popup], events)
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=context,
        context_page=primary,
        ports=login_ports,
    )

    selected = await login._single_login_page()

    assert selected is primary
    assert events == []
    assert popup.is_closed() is False


@pytest.mark.asyncio
async def test_crawler_login_selection_does_not_replace_closed_pages() -> None:
    events: list[tuple[str, object]] = []
    closed = FakePage("closed", events)
    closed.closed = True
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = FakeContext([closed], events)
    crawler.context_page = closed
    crawler._new_guarded_page = AsyncMock(
        side_effect=AssertionError("must not create a replacement login page")
    )

    with pytest.raises(RuntimeError, match="xhs_login_browser_pages_closed"):
        await crawler._single_page_for_login()

    crawler._new_guarded_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_standalone_xhs_login_stops_on_platform_security_limit() -> None:
    class SecurityLimitPage:
        url = "https://www.xiaohongshu.com/website-login/error"

        def is_closed(self) -> bool:
            return False

        async def is_visible(self, selector: str, timeout: int) -> bool:
            return True

        async def content(self) -> str:
            return ""

    page = SecurityLimitPage()
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=SimpleNamespace(pages=[page]),
        context_page=page,
        ports=login_ports,
    )

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await login._check_login_state_once("")

    assert exc_info.value.code == "xhs_login_error_page"


@pytest.mark.asyncio
async def test_xhs_shutdown_never_snapshots_browser_storage() -> None:
    events: list[tuple[str, object]] = []
    page = FakePage("search", events)
    context = FakeContext([page], events)
    context.storage_state = AsyncMock(
        side_effect=AssertionError("XHS must not serialize login state")
    )
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = context

    await crawler._prepare_browser_shutdown()

    context.storage_state.assert_not_awaited()
    assert events == [("close", "search")]
    assert not hasattr(crawler, "_shutdown_storage_state_written")
@pytest.mark.asyncio
async def test_cdp_launch_failure_never_starts_standard_fallback(monkeypatch) -> None:
    instances = []
    cleanup_calls: list[bool] = []

    class FailingManager:
        def __init__(self) -> None:
            instances.append(self)

        async def launch_and_connect(self, **_kwargs):
            await self.cleanup(force=True)
            raise RuntimeError("cdp connect failed")

        async def cleanup(self, *, force: bool = False) -> None:
            cleanup_calls.append(force)

        async def get_browser_info(self):
            raise AssertionError("failed launch must not query browser info")

    chromium = SimpleNamespace(
        launch=AsyncMock(side_effect=AssertionError("second browser launched")),
        launch_persistent_context=AsyncMock(
            side_effect=AssertionError("second browser context launched")
        ),
    )
    playwright = SimpleNamespace(chromium=chromium)
    crawler = XiaoHongShuCrawler()
    crawler.launch_browser = AsyncMock(
        side_effect=AssertionError("standard fallback launched")
    )
    monkeypatch.setattr(
        crawler.ports,
        "browser_manager_factory",
        FailingManager,
    )

    with pytest.raises(
        RuntimeError,
        match=r"^xhs_cdp_browser_launch_failed:RuntimeError: cdp connect failed$",
    ):
        await crawler.launch_browser_with_cdp(
            playwright,
            None,
            None,
            headless=False,
        )

    assert len(instances) == 1
    assert cleanup_calls == [True]
    assert crawler.cdp_manager is None
    crawler.launch_browser.assert_not_awaited()
    chromium.launch.assert_not_awaited()
    chromium.launch_persistent_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_session_latch_prevents_concurrent_second_launch(
    monkeypatch,
) -> None:
    launch_entered = asyncio.Event()
    release_launch = asyncio.Event()

    async def blocked_launch(*_args, **_kwargs):
        launch_entered.set()
        await release_launch.wait()
        raise RuntimeError("first launch failed")

    crawler = XiaoHongShuCrawler()
    crawler.launch_browser_with_cdp = AsyncMock(side_effect=blocked_launch)
    monkeypatch.setattr(config, "ENABLE_CDP_MODE", True)
    playwright = SimpleNamespace(chromium=SimpleNamespace())

    first = asyncio.create_task(crawler._run_browser_session(playwright, None, None))
    await asyncio.wait_for(launch_entered.wait(), timeout=0.2)
    with pytest.raises(RuntimeError, match=r"^xhs_browser_session_already_started$"):
        await crawler._run_browser_session(playwright, None, None)

    release_launch.set()
    with pytest.raises(RuntimeError, match="first launch failed"):
        await first

    assert crawler._browser_session_started is True
    crawler.launch_browser_with_cdp.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_browser_session_cannot_be_restarted_sequentially(
    monkeypatch,
) -> None:
    crawler = XiaoHongShuCrawler()
    crawler.launch_browser_with_cdp = AsyncMock(
        side_effect=RuntimeError("first launch failed")
    )
    monkeypatch.setattr(config, "ENABLE_CDP_MODE", True)
    playwright = SimpleNamespace(chromium=SimpleNamespace())

    with pytest.raises(RuntimeError, match="first launch failed"):
        await crawler._run_browser_session(playwright, None, None)
    with pytest.raises(RuntimeError, match=r"^xhs_browser_session_already_started$"):
        await crawler._run_browser_session(playwright, None, None)

    crawler.launch_browser_with_cdp.assert_awaited_once()
