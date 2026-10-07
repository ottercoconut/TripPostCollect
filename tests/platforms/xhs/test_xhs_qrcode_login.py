from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from trippostcollect.platforms.xhs import login as login_module
from trippostcollect.platforms.xhs.errors import PlatformRuntimeError
from trippostcollect.platforms.xhs.login import XiaoHongShuLogin
from trippostcollect.platforms.xhs.manual_wait import (
    XHSManualWaitBudget,
    XHSManualWaitBudgetExhausted,
)

from .support import XiaoHongShuCrawler, config, login_ports


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep: Callable[[float], None] | None = None

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.now)


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    async def count(self) -> int:
        return 1 if self.selector == "body" else int(
            self.selector in self.page.visible_selectors
        )

    async def inner_text(self, timeout: int) -> str:
        assert timeout > 0
        if self.page.inner_text_hook is not None:
            self.page.inner_text_hook(self.page)
        if self.page.text_error is not None:
            raise self.page.text_error
        return self.page.visible_text

    async def is_visible(self, timeout: int) -> bool:
        assert timeout > 0
        return self.selector in self.page.visible_selectors

    async def click(self, timeout: int) -> None:
        assert timeout > 0
        self.page.events.append(("click", self.selector))
        self.page.click_times.append(self.page.clock.now)
        if self.page.click_hook is not None:
            self.page.click_hook(self.page, self.selector)


class FakePage:
    def __init__(self, clock: FakeClock, *, name: str = "login") -> None:
        self.clock = clock
        self.name = name
        self.url = f"https://www.xiaohongshu.com/{name}"
        self.visible_text = ""
        self.hidden_html = ""
        self.visible_selectors: set[str] = set()
        self.closed = False
        self.close_calls = 0
        self.reload_times: list[float] = []
        self.click_times: list[float] = []
        self.events: list[tuple[str, str]] = []
        self.inner_text_hook: Callable[[FakePage], None] | None = None
        self.click_hook: Callable[[FakePage, str], None] | None = None
        self.text_error: Exception | None = None

    @property
    def frames(self) -> list[FakePage]:
        return [self]

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def is_visible(self, selector: str, timeout: int) -> bool:
        assert timeout > 0
        return selector in self.visible_selectors

    async def content(self) -> str:
        return self.hidden_html or self.visible_text

    async def reload(self, *, wait_until: str, timeout: int) -> None:
        assert wait_until == "domcontentloaded"
        assert timeout == 30_000
        self.reload_times.append(self.clock.now)

    async def bring_to_front(self) -> None:
        self.events.append(("front", self.name))

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


class FakeContext:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages
        self.new_page_calls = 0

    async def cookies(self) -> list[dict[str, str]]:
        return [{"name": "web_session", "value": "before"}]

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1
        page = FakePage(self.pages[0].clock, name="new-login")
        self.pages.append(page)
        return page


def configure_virtual_login(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    *,
    wait_seconds: int,
    refresh_seconds: int = 180,
    stable_seconds: int = 5,
) -> None:
    monkeypatch.setattr(login_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(login_module.asyncio, "sleep", clock.sleep)

    async def find_login_qrcode(page, selector: str):
        assert selector == XiaoHongShuLogin._QRCODE_SELECTOR
        return "qr-image" if selector in page.visible_selectors else None

    monkeypatch.setattr(
        login_ports,
        "find_login_qrcode",
        find_login_qrcode,
    )
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS",
        str(wait_seconds),
    )
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS",
        str(refresh_seconds),
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_LOGIN_POLL_SECONDS", "1")
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_STABLE_LOGIN_SECONDS",
        str(stable_seconds),
    )
    monkeypatch.setattr(config, "ENABLE_CDP_MODE", True)
    monkeypatch.setattr(config, "CDP_HEADLESS", False)


def make_login(
    page: FakePage,
    *extra_pages: FakePage,
) -> XiaoHongShuLogin:
    return XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=FakeContext([page, *extra_pages]),
        context_page=page,
        ports=login_ports,
    )


@pytest.mark.asyncio
async def test_qrcode_reload_is_clamped_to_at_least_180_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录 打开小红书扫一扫"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(page)
    configure_virtual_login(
        monkeypatch,
        clock,
        wait_seconds=181,
        refresh_seconds=90,
    )

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    assert page.reload_times == [pytest.approx(180.0)]


@pytest.mark.asyncio
async def test_zero_budget_is_terminal_before_any_login_page_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    context = FakeContext([page])
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=context,
        context_page=page,
        ports=login_ports,
    )
    configure_virtual_login(monkeypatch, clock, wait_seconds=0)

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    assert clock.now == 0.0
    assert login.terminal_context() == {
        "checkpoint_kind": "initial_qrcode_login",
        "manual_progress_observed": False,
        "matched_markers": [],
    }
    assert page.reload_times == []
    assert page.click_times == []
    assert page.close_calls == 0
    assert context.new_page_calls == 0


@pytest.mark.asyncio
async def test_visible_verification_latches_and_prevents_later_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=190)

    def advance_state(now: float) -> None:
        if now >= 179:
            page.visible_text = "请输入验证码"
            page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)
            page.visible_selectors.add("input[placeholder*='验证码']")

    clock.on_sleep = advance_state

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    assert login._last_login_observation["manual_in_progress"] is True
    assert login.terminal_context()["checkpoint_kind"] == "sms_verification"
    assert login.terminal_context()["manual_progress_observed"] is True
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_captcha_budget_exhaustion_preserves_checkpoint_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "请通过安全验证，拖动滑块"
    page.visible_selectors.add("[class*='slider']")
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=2)

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    context = login.terminal_context()
    assert context["checkpoint_kind"] == "captcha"
    assert context["manual_progress_observed"] is True
    assert "安全验证" in context["matched_markers"]


@pytest.mark.asyncio
async def test_sms_verification_without_error_latches_despite_background_qr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录 SMS Verification"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=181)

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    assert login._last_login_observation["manual_in_progress"] is True
    assert page.reload_times == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "error_code"),
    [
        (
            "SMS Verification Parameter error Refresh Feedback",
            "xhs_sms_verification_parameter_error",
        ),
        (
            "SMS Verification 今日次数已达上限",
            "xhs_sms_verification_daily_limit",
        ),
        (
            "SMS Verification 操作频繁",
            "xhs_sms_verification_rate_limited",
        ),
    ],
)
async def test_sms_error_is_terminal_without_wait_refresh_or_close(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    error_code: str,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = text
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=600)

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await login.login_by_qrcode()

    assert exc_info.value.code == error_code
    assert login.terminal_context()["checkpoint_kind"] == "sms_verification"
    assert login.terminal_context()["matched_markers"]
    assert clock.now == 0
    assert page.reload_times == []
    assert page.click_times == []
    assert page.close_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "url", "expected_code", "expected_marker"),
    [
        (
            "安全限制",
            "https://www.xiaohongshu.com/login",
            "xhs_platform_security_limit_unspecified",
            "安全限制",
        ),
        (
            "账号异常",
            "https://www.xiaohongshu.com/login",
            "xhs_account_exception",
            "账号异常",
        ),
        (
            "",
            "https://www.xiaohongshu.com/website-login/error",
            "xhs_login_error_page",
            "website-login/error",
        ),
        (
            "账号异常，错误码 300011",
            "https://www.xiaohongshu.com/login",
            "platform_security_limit_300011",
            "300011",
        ),
        (
            "安全限制，错误码 300012",
            "https://www.xiaohongshu.com/login",
            "ip_blocked_300012",
            "300012",
        ),
        (
            "",
            "https://www.xiaohongshu.com/website-login/error?code=300011",
            "platform_security_limit_300011",
            "300011",
        ),
    ],
)
async def test_login_terminal_codes_are_exact_and_mutually_exclusive(
    text: str,
    url: str,
    expected_code: str,
    expected_marker: str,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = text
    page.url = url
    login = make_login(page)

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await login._check_login_state_once("before")

    assert exc_info.value.code == expected_code
    assert login._last_login_observation["terminal_code"] == expected_code
    assert login.terminal_context()["matched_markers"] == [expected_marker]


@pytest.mark.asyncio
async def test_longer_numeric_value_does_not_impersonate_security_code() -> None:
    clock = FakeClock()
    page = FakePage(clock, name="explore/note")
    page.visible_text = "游记编号 1300011 和 2300012"
    login = make_login(page)

    assert await login._check_login_state_once("before") is False
    assert login._last_login_observation["terminal_code"] == ""


@pytest.mark.asyncio
async def test_verification_appearing_at_refresh_boundary_cancels_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=185)
    observations_at_boundary = 0

    def on_inner_text(current_page: FakePage) -> None:
        nonlocal observations_at_boundary
        if clock.now == 180:
            observations_at_boundary += 1
            if observations_at_boundary == 2:
                current_page.visible_text = "请输入验证码"
                current_page.visible_selectors.discard(
                    XiaoHongShuLogin._QRCODE_SELECTOR
                )
                current_page.visible_selectors.add(
                    "input[placeholder*='验证码']"
                )

    page.inner_text_hook = on_inner_text

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    assert observations_at_boundary >= 2
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_expired_pure_qr_uses_component_refresh_before_reload_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录 打开小红书扫一扫"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    refresh_selector = XiaoHongShuLogin._QR_COMPONENT_REFRESH_SELECTORS[0]
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=220)
    component_refreshed = False

    def advance_state(now: float) -> None:
        if now >= 60 and not component_refreshed:
            page.visible_text = "二维码已过期 点击刷新 验证码"
            page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)
            page.visible_selectors.update(
                {refresh_selector, "input[placeholder*='验证码']"}
            )

    def refresh_component(current_page: FakePage, selector: str) -> None:
        nonlocal component_refreshed
        assert selector == refresh_selector
        component_refreshed = True
        current_page.visible_text = "扫码登录 打开小红书扫一扫"
        current_page.visible_selectors.discard(refresh_selector)
        current_page.visible_selectors.discard(
            "input[placeholder*='验证码']"
        )
        current_page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)

    clock.on_sleep = advance_state
    page.click_hook = refresh_component

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    assert page.click_times == [pytest.approx(60.0)]
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_initial_expired_qr_only_clicks_component_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "二维码已过期 点击刷新"
    refresh_selector = XiaoHongShuLogin._QR_COMPONENT_REFRESH_SELECTORS[0]
    page.visible_selectors.add(refresh_selector)
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=2)

    def refresh_component(current_page: FakePage, selector: str) -> None:
        assert selector == refresh_selector
        current_page.visible_text = "扫码登录"
        current_page.visible_selectors.discard(refresh_selector)
        current_page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)

    page.click_hook = refresh_component

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    assert page.events == [("click", refresh_selector)]
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_scan_transition_during_expiry_confirmation_cancels_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    refresh_selector = XiaoHongShuLogin._QR_COMPONENT_REFRESH_SELECTORS[0]
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=185)
    expiry_observations = 0
    manual_transitioned = False

    def advance_state(now: float) -> None:
        if now >= 60 and not manual_transitioned:
            page.visible_text = "二维码已过期 点击刷新"
            page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)
            page.visible_selectors.add(refresh_selector)

    def transition(current_page: FakePage) -> None:
        nonlocal expiry_observations, manual_transitioned
        if clock.now != 60:
            return
        expiry_observations += 1
        if expiry_observations == 2:
            manual_transitioned = True
            current_page.visible_text = "已扫码 请在手机上确认"
            current_page.visible_selectors.discard(refresh_selector)

    clock.on_sleep = advance_state
    page.inner_text_hook = transition

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    assert expiry_observations >= 2
    assert login._last_login_observation["manual_in_progress"] is True
    assert page.click_times == []
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_manual_latch_blocks_later_expired_qr_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    refresh_selector = XiaoHongShuLogin._QR_COMPONENT_REFRESH_SELECTORS[0]
    login = make_login(page)
    configure_virtual_login(monkeypatch, clock, wait_seconds=190)

    def advance_state(now: float) -> None:
        if 50 <= now < 70:
            page.visible_text = "请输入验证码"
            page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)
            page.visible_selectors.add("input[placeholder*='验证码']")
        elif now >= 70:
            page.visible_text = "二维码已过期 点击刷新"
            page.visible_selectors.discard("input[placeholder*='验证码']")
            page.visible_selectors.add(refresh_selector)

    clock.on_sleep = advance_state

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    assert page.click_times == []
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_visible_checkpoint_wins_over_stale_profile_button() -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "请输入验证码"
    page.visible_selectors.update(
        {
            XiaoHongShuLogin._PROFILE_SELECTORS[0],
            "input[placeholder*='验证码']",
        }
    )
    login = make_login(page)

    assert await login._check_login_state_once("before") is False
    assert login._last_login_observation["profile_visible"] is True
    assert login._last_login_observation["visible_checkpoint"] is True


@pytest.mark.asyncio
async def test_hidden_checkpoint_html_does_not_mask_visible_signed_in_ui() -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "首页 我"
    page.hidden_html = "<div hidden>请输入验证码</div>"
    page.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])
    login = make_login(page)

    assert await login._check_login_state_once("before") is True
    assert login._last_login_observation["visible_checkpoint"] is False


@pytest.mark.asyncio
async def test_plain_content_words_are_not_login_error_or_profile_evidence() -> None:
    clock = FakeClock()
    page = FakePage(clock, name="explore/note")
    page.visible_text = (
        "我在文章里讨论频繁请求、达到上限、超过上限、"
        "今日次数已达上限和 Parameter error"
    )
    login = make_login(page)

    assert await login._check_login_state_once("before") is False
    assert login._last_login_observation["terminal_code"] == ""
    assert login._last_login_observation["profile_visible"] is False


@pytest.mark.asyncio
async def test_unrelated_retained_qr_tab_does_not_block_profile_success() -> None:
    clock = FakeClock()
    signed_in = FakePage(clock, name="signed-in")
    signed_in.visible_text = "首页 我"
    signed_in.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])
    stale_qr = FakePage(clock, name="stale-qr")
    stale_qr.visible_text = "扫码登录"
    stale_qr.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    login = make_login(signed_in, stale_qr)

    assert await login._check_login_state_once("before") is True
    assert stale_qr.closed is False


@pytest.mark.asyncio
async def test_login_requires_stable_ui_after_transient_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "首页 我"
    page.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])
    login = make_login(page)
    configure_virtual_login(
        monkeypatch,
        clock,
        wait_seconds=20,
        stable_seconds=5,
    )

    def advance_state(now: float) -> None:
        if 2 <= now < 4:
            page.visible_text = "请输入验证码"
            page.visible_selectors.discard(XiaoHongShuLogin._PROFILE_SELECTORS[0])
            page.visible_selectors.add("input[placeholder*='验证码']")
        elif now >= 4:
            page.visible_text = "首页 我"
            page.visible_selectors.discard("input[placeholder*='验证码']")
            page.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])

    clock.on_sleep = advance_state

    await login.login_by_qrcode()

    assert clock.now >= 9
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_login_polling_preserves_verification_popup() -> None:
    clock = FakeClock()
    primary = FakePage(clock)
    popup = FakePage(clock, name="verification")
    login = make_login(primary, popup)

    assert await login._single_login_page() is primary
    assert popup.closed is False
    assert popup.events == []


@pytest.mark.asyncio
async def test_all_login_pages_closed_is_terminal_without_new_page() -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.closed = True
    context = FakeContext([page])
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=context,
        context_page=page,
        new_page=AsyncMock(side_effect=AssertionError("must not relaunch page")),
        ports=login_ports,
    )

    with pytest.raises(RuntimeError, match="xhs_login_browser_pages_closed"):
        await login._single_login_page()

    assert context.new_page_calls == 0
    login.new_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_page_text_failure_does_not_close_or_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    page.text_error = ConnectionError("network offline")
    context = FakeContext([page])
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=context,
        context_page=page,
        ports=login_ports,
    )
    configure_virtual_login(monkeypatch, clock, wait_seconds=3)

    def recover_page(now: float) -> None:
        if now >= 1:
            page.text_error = None
            page.visible_text = "SMS Verification"
            page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)
            page.visible_selectors.add("input[placeholder*='验证码']")

    clock.on_sleep = recover_page

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    assert login.context_page is page
    assert context.pages == [page]
    assert context.new_page_calls == 0
    assert login._last_login_observation["manual_in_progress"] is True
    assert page.close_calls == 0
    assert page.reload_times == []


@pytest.mark.asyncio
async def test_initial_login_usage_leaves_only_remainder_for_midrun_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    page = FakePage(clock)
    page.visible_text = "扫码登录"
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    context = FakeContext([page])
    budget = XHSManualWaitBudget(
        limit_seconds=10.0,
        monotonic=clock.monotonic,
    )
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=context,
        context_page=page,
        manual_wait_budget=budget,
        ports=login_ports,
    )
    configure_virtual_login(
        monkeypatch,
        clock,
        wait_seconds=600,
        stable_seconds=3,
    )

    def finish_scan(now: float) -> None:
        if now >= 1:
            page.visible_text = "首页 我"
            page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)
            page.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])

    clock.on_sleep = finish_scan

    await login.login_by_qrcode()

    assert budget.manual_elapsed_seconds == pytest.approx(4.0)
    assert budget.remaining_seconds == pytest.approx(6.0)

    crawler = XiaoHongShuCrawler()
    crawler._manual_wait_budget = budget
    crawler.context_page = page
    crawler.browser_context = context
    crawler.cookie_urls = [crawler.index_url]
    crawler._popup_monotonic = clock.monotonic
    crawler._popup_sleep = clock.sleep
    crawler._profile_ui_visible = AsyncMock(return_value=False)
    crawler._popup_checkpoint_state = AsyncMock(
        return_value={
            "closed": False,
            "visible_text": "SMS Verification",
            "manual_markers": ["SMS Verification"],
            "terminal": "",
        }
    )
    crawler.xhs_client = SimpleNamespace(
        update_cookies=AsyncMock(),
        pong=AsyncMock(return_value=False),
    )
    crawler._goto_with_deadline = AsyncMock()

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await crawler._wait_for_midrun_login_recovery("青岛旅游")

    assert clock.now == pytest.approx(10.0)
    assert budget.manual_elapsed_seconds == pytest.approx(10.0)
    assert page.reload_times == []
    assert page.close_calls == 0
    assert context.new_page_calls == 0
    crawler._goto_with_deadline.assert_not_awaited()
