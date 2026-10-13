from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import trippostcollect.platforms.xhs.core as xhs_core
import trippostcollect.platforms.xhs.login as xhs_login
import trippostcollect.platforms.xhs.session as xhs_session
from support.page_events import Emitter, FakeFrame, FakeRequest, FakeResponse
from trippostcollect.application.worker_inputs import _validate_login_contract
from trippostcollect.platforms.xhs.login import XiaoHongShuLogin
from trippostcollect.platforms.xhs.manual_wait import (
    XHSManualWaitBudget,
    XHSManualWaitBudgetExhausted,
)

from trippostcollect.runtime.visit_ledger import load_ledger

from .support import XiaoHongShuCrawler, config, make_capture


_REMOVED_LOGIN_ENV_VARS = (
    "TRIPPOSTCOLLECT_XHS_RUN_SCOPED_LOGIN",
    "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH",
    "TRIPPOSTCOLLECT_COOKIES",
)


@pytest.fixture(autouse=True)
def clean_removed_login_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in _REMOVED_LOGIN_ENV_VARS:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(config, "LOGIN_TYPE", "qrcode")
    monkeypatch.setattr(config, "COOKIES", "")


@pytest.mark.parametrize("login_type", ["phone", "cookie", "unknown", ""])
def test_low_level_xhs_login_rejects_every_non_qrcode_type(
    login_type: str,
) -> None:
    with pytest.raises(ValueError, match="^xhs_login_type_must_be_qrcode$"):
        XiaoHongShuLogin(
            login_type=login_type,
            browser_context=SimpleNamespace(),
            context_page=SimpleNamespace(),
        )


@pytest.mark.parametrize("removed_parameter", ["login_phone", "cookie_str"])
def test_removed_login_constructor_parameters_fail_closed(
    removed_parameter: str,
) -> None:
    kwargs = {
        "login_type": "qrcode",
        "browser_context": SimpleNamespace(),
        "context_page": SimpleNamespace(),
        removed_parameter: "legacy-value",
    }

    with pytest.raises(TypeError, match=f"unexpected keyword argument '{removed_parameter}'"):
        XiaoHongShuLogin(**kwargs)


@pytest.mark.asyncio
async def test_xhs_login_begin_has_only_the_qrcode_branch() -> None:
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=SimpleNamespace(),
        context_page=SimpleNamespace(),
    )
    login._single_login_page = AsyncMock()
    login.login_by_qrcode = AsyncMock()

    await login.begin()

    login._single_login_page.assert_awaited_once_with()
    login.login_by_qrcode.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_qrcode_login_budget_exhaustion_writes_structured_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = SimpleNamespace()
    crawler.context_page = SimpleNamespace()
    crawler.capture = make_capture(tmp_path)
    append_event = MagicMock()

    class ExhaustedLogin:
        def __init__(self, **kwargs: object) -> None:
            self.context_page = kwargs["context_page"]

        async def begin(self) -> None:
            raise XHSManualWaitBudgetExhausted()

        def terminal_context(self) -> dict[str, object]:
            return {
                "checkpoint_kind": "qrcode_waiting",
                "manual_progress_observed": False,
                "matched_markers": [],
            }

    monkeypatch.setattr(xhs_login, "XiaoHongShuLogin", ExhaustedLogin)
    monkeypatch.setattr(crawler.ports, "append_execution_event", append_event)

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await crawler._run_qrcode_login()

    append_event.assert_called_once_with(
        "xhs_runtime_terminal",
        {
            "phase": "login",
            "failure_type": "manual_checkpoint_timeout",
            "stop_reason": "runtime_failed",
            "stop_detail": "xhs_manual_checkpoint_budget_exhausted",
            "checkpoint_kind": "qrcode_waiting",
            "manual_progress_observed": False,
            "matched_markers": [],
            "retryable": False,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_failure_type", "expected_detail"),
    [
        (
            RuntimeError("xhs_login_browser_pages_closed"),
            "browser_target_closed",
            "xhs_login_browser_pages_closed",
        ),
        (
            RuntimeError("unexpected login observation failure"),
            "login_runtime_error",
            "unexpected login observation failure",
        ),
    ],
)
async def test_qrcode_login_unknown_exception_writes_structured_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
    error: RuntimeError,
    expected_failure_type: str,
    expected_detail: str,
    tmp_path: Path,
) -> None:
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = SimpleNamespace()
    crawler.context_page = SimpleNamespace()
    crawler.capture = make_capture(tmp_path)
    append_event = MagicMock()

    class BrokenLogin:
        def __init__(self, **kwargs: object) -> None:
            self.context_page = kwargs["context_page"]

        async def begin(self) -> None:
            raise error

        def terminal_context(self) -> dict[str, object]:
            return {
                "checkpoint_kind": "unknown",
                "manual_progress_observed": False,
                "matched_markers": [],
            }

    monkeypatch.setattr(xhs_login, "XiaoHongShuLogin", BrokenLogin)
    monkeypatch.setattr(crawler.ports, "append_execution_event", append_event)

    with pytest.raises(RuntimeError, match=expected_detail):
        await crawler._run_qrcode_login()

    append_event.assert_called_once_with(
        "xhs_runtime_terminal",
        {
            "phase": "login",
            "failure_type": expected_failure_type,
            "stop_reason": "runtime_failed",
            "stop_detail": expected_detail,
            "checkpoint_kind": "unknown",
            "manual_progress_observed": False,
            "matched_markers": [],
            "retryable": False,
        },
    )


@pytest.mark.parametrize(
    (
        "text",
        "url",
        "sms_context",
        "expected_code",
        "expected_failure_type",
    ),
    [
        (
            "SMS Verification\nParameter error",
            "https://www.xiaohongshu.com/",
            True,
            "xhs_sms_verification_parameter_error",
            "sms_verification_terminal",
        ),
        (
            "今日短信验证码次数已达上限",
            "https://www.xiaohongshu.com/",
            True,
            "xhs_sms_verification_daily_limit",
            "sms_verification_terminal",
        ),
        (
            "SMS Verification\nRequests too frequent",
            "https://www.xiaohongshu.com/",
            True,
            "xhs_sms_verification_rate_limited",
            "sms_verification_terminal",
        ),
        (
            "安全限制 300011",
            "https://www.xiaohongshu.com/",
            False,
            "platform_security_limit_300011",
            "platform_security_limit",
        ),
        (
            "账号异常 300012",
            "https://www.xiaohongshu.com/",
            False,
            "ip_blocked_300012",
            "ip_blocked",
        ),
        (
            "安全限制",
            "https://www.xiaohongshu.com/",
            False,
            "xhs_platform_security_limit_unspecified",
            "platform_security_limit",
        ),
        (
            "Account exception",
            "https://www.xiaohongshu.com/",
            False,
            "xhs_account_exception",
            "platform_security_limit",
        ),
        (
            "Something went wrong",
            "https://www.xiaohongshu.com/website-login/error",
            False,
            "xhs_login_error_page",
            "platform_security_limit",
        ),
    ],
)
def test_login_terminal_classification_preserves_precise_subtype(
    text: str,
    url: str,
    sms_context: bool,
    expected_code: str,
    expected_failure_type: str,
) -> None:
    code, failure_type, _markers = XiaoHongShuLogin.classify_terminal_state(
        text=text,
        url=url,
        sms_verification_context=sms_context,
    )

    assert code == expected_code
    assert failure_type == expected_failure_type


def test_generic_parameter_error_without_sms_context_is_not_sms_terminal() -> None:
    code, failure_type, markers = XiaoHongShuLogin.classify_terminal_state(
        text="Parameter error",
        url="https://www.xiaohongshu.com/",
        sms_verification_context=False,
    )

    assert (code, failure_type, markers) == ("", "", [])


def test_visible_terminal_generic_marker_never_invents_300011() -> None:
    code, failure_type, markers = XiaoHongShuCrawler._classify_visible_terminal(
        text="page rendered",
        url="https://www.xiaohongshu.com/",
        markers={"platform_security_limit": True},
    )

    assert code == "xhs_platform_security_limit_unspecified"
    assert failure_type == "platform_security_limit"
    assert markers == ["platform_security_limit"]


@pytest.mark.asyncio
async def test_creator_generic_security_marker_raises_unspecified_subtype(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = XiaoHongShuCrawler()
    page = SimpleNamespace(url="https://www.xiaohongshu.com/user/profile/example")
    record_security = AsyncMock()
    monkeypatch.setattr(
        crawler.ports,
        "record_platform_security_limit",
        record_security,
    )

    with pytest.raises(xhs_core.PlatformRuntimeError) as captured:
        await crawler._raise_for_creator_page_terminal(
            page,
            user_id="example",
            stage="arrival",
            visible_text="安全限制",
            visible_markers={"platform_security_limit": True},
        )

    assert captured.value.code == "xhs_platform_security_limit_unspecified"
    record_security.assert_awaited_once()


@pytest.mark.parametrize(
    ("marker", "expected_code", "expected_failure_type"),
    [
        ("rate_limited", "xhs_rate_limited_terminal", "rate_limited"),
        ("blocked", "xhs_blocked_terminal", "blocked_or_forbidden"),
    ],
)
def test_visible_terminal_non_security_blockers_have_stable_codes(
    marker: str,
    expected_code: str,
    expected_failure_type: str,
) -> None:
    code, failure_type, _markers = XiaoHongShuCrawler._classify_visible_terminal(
        text="page rendered",
        url="https://www.xiaohongshu.com/",
        markers={marker: True},
    )

    assert code == expected_code
    assert failure_type == expected_failure_type


def test_xhs_login_has_no_mobile_cookie_or_redis_automation_surface() -> None:
    signature = inspect.signature(XiaoHongShuLogin)
    source = inspect.getsource(XiaoHongShuLogin)

    assert "login_phone" not in signature.parameters
    assert "cookie_str" not in signature.parameters
    assert not hasattr(XiaoHongShuLogin, "login_by_mobile")
    assert not hasattr(XiaoHongShuLogin, "login_by_cookies")
    assert not hasattr(XiaoHongShuLogin, "check_login_state")
    assert not hasattr(XiaoHongShuLogin, "wait_login_state")
    assert "CacheFactory" not in source
    assert "send_btn_ele" not in source
    assert "sms_code_key" not in source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("TRIPPOSTCOLLECT_XHS_RUN_SCOPED_LOGIN", ""),
        ("TRIPPOSTCOLLECT_XHS_RUN_SCOPED_LOGIN", "0"),
        ("TRIPPOSTCOLLECT_XHS_RUN_SCOPED_LOGIN", "1"),
        ("TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH", ""),
        ("TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH", "/tmp/legacy.json"),
        ("TRIPPOSTCOLLECT_COOKIES", ""),
        ("TRIPPOSTCOLLECT_COOKIES", "web_session=legacy"),
    ],
)
async def test_removed_login_environment_fails_before_browser_start(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
) -> None:
    monkeypatch.setenv(env_name, env_value)
    async_playwright = MagicMock(
        side_effect=AssertionError("deprecated input must fail before browser start")
    )
    crawler = XiaoHongShuCrawler()
    monkeypatch.setattr(crawler.ports, "async_playwright", async_playwright)

    with pytest.raises(
        RuntimeError,
        match=f"^xhs_deprecated_login_input:{env_name}$",
    ):
        await crawler.start()

    async_playwright.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("login_type", ["phone", "cookie", "unknown"])
async def test_removed_xhs_login_type_fails_before_browser_start(
    monkeypatch: pytest.MonkeyPatch,
    login_type: str,
) -> None:
    monkeypatch.setattr(config, "LOGIN_TYPE", login_type)
    async_playwright = MagicMock(
        side_effect=AssertionError("invalid login type must fail before browser start")
    )
    crawler = XiaoHongShuCrawler()
    monkeypatch.setattr(crawler.ports, "async_playwright", async_playwright)

    with pytest.raises(RuntimeError, match="^xhs_login_type_must_be_qrcode$"):
        await crawler.start()

    async_playwright.assert_not_called()


@pytest.mark.asyncio
async def test_xhs_cookie_login_value_fails_before_browser_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "COOKIES", "web_session=legacy")
    async_playwright = MagicMock(
        side_effect=AssertionError("cookie input must fail before browser start")
    )
    crawler = XiaoHongShuCrawler()
    monkeypatch.setattr(crawler.ports, "async_playwright", async_playwright)

    with pytest.raises(RuntimeError, match="^xhs_cookie_login_input_removed$"):
        await crawler.start()

    async_playwright.assert_not_called()


def test_current_qrcode_contract_and_temporary_profile_remain_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", str(profile_dir))

    _validate_login_contract(config)
    # 正式小红书恒为 CDP：profile 由 CDP 启动管理读取，crawler 不再有第二条启动路径。
    assert not hasattr(XiaoHongShuCrawler, "_profile_dir")
    assert not hasattr(XiaoHongShuCrawler, "launch_browser")


def test_xhs_crawler_has_no_storage_state_compatibility_surface() -> None:
    # T09：crawler 按 mixin 拆分，源码面覆盖根类与全部 mixin。
    source = "".join(
        inspect.getsource(cls)
        for cls in XiaoHongShuCrawler.__mro__
        if cls.__module__.startswith("trippostcollect.platforms.xhs.")
    )

    assert not hasattr(XiaoHongShuCrawler, "_storage_state_path")
    assert not hasattr(XiaoHongShuCrawler, "_cookie_for_restore")
    assert not hasattr(XiaoHongShuCrawler, "_restore_storage_state")
    assert not hasattr(XiaoHongShuCrawler, "_write_storage_state")
    assert "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH" not in source
    assert ".storage_state(" not in source


class _LoginFlowClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleep_calls: list[float] = []
        self.on_sleep: Callable[[float], None] | None = None

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.now)


class _LoginFlowLocator:
    def __init__(self, page: "_LoginFlowPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    async def count(self) -> int:
        return 1 if self.selector == "body" else int(
            self.selector in self.page.visible_selectors
        )

    async def inner_text(self, timeout: int) -> str:
        assert timeout > 0
        return self.page.visible_text

    async def is_visible(self, timeout: int) -> bool:
        assert timeout > 0
        return self.selector in self.page.visible_selectors

    async def click(self, timeout: int) -> None:
        raise AssertionError(f"login flow must not click unexpected selector: {self.selector}")


class _LoginFlowPage(Emitter):
    def __init__(self, *, visible_text: str = "") -> None:
        super().__init__()
        self.reloads = 0
        self.on_reload: Callable[[], None] | None = None
        self.url = "https://www.xiaohongshu.com/explore"
        self.visible_text = visible_text
        self.visible_selectors: set[str] = set()
        self.closed = False
        self.front_count = 0

    @property
    def frames(self) -> list["_LoginFlowPage"]:
        return [self]

    def locator(self, selector: str) -> _LoginFlowLocator:
        return _LoginFlowLocator(self, selector)

    async def is_visible(self, selector: str, timeout: int) -> bool:
        assert timeout > 0
        return selector in self.visible_selectors

    async def bring_to_front(self) -> None:
        self.front_count += 1

    async def reload(self, **_kwargs: object) -> None:
        self.reloads += 1
        if self.on_reload is not None:
            self.on_reload()

    def emit_user_me(self, *, guest: bool) -> None:
        request = FakeRequest(
            method="GET",
            url="https://edith.xiaohongshu.com/api/sns/web/v2/user/me",
            resource_type="xhr",
            frame=FakeFrame(self),
        )
        payload = {"success": True, "data": {"guest": guest, "user_id": "" if guest else "self"}}
        self.emit("response", FakeResponse(request, body=json.dumps(payload).encode()))

    def is_closed(self) -> bool:
        return self.closed


class _LoginFlowContext:
    def __init__(self, page: _LoginFlowPage) -> None:
        self.pages = [page]
        self.page_handler: object | None = None
        self.new_page_calls = 0

    def on(self, event: str, handler: object) -> None:
        assert event == "page"
        self.page_handler = handler

    async def new_page(self) -> _LoginFlowPage:
        self.new_page_calls += 1
        raise AssertionError("startup login must reuse the current BrowserContext page")


def _configure_browser_session_test(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    page: _LoginFlowPage,
    events: list[str],
) -> tuple[XiaoHongShuCrawler, _LoginFlowContext, SimpleNamespace]:
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior_evidence.json"))
    monkeypatch.setattr(xhs_session, "XHS_LOGIN_ME_CONFIRM_SECONDS", 0.05)
    context = _LoginFlowContext(page)
    crawler = XiaoHongShuCrawler()
    crawler.launch_browser_with_cdp = AsyncMock(return_value=context)
    crawler._goto_with_deadline = AsyncMock()
    crawler._wait_for_initial_page_settle = AsyncMock()
    crawler._wait_for_visible_page_shell = AsyncMock(return_value=True)
    crawler._open_behavior_search_page_on_primary_page = AsyncMock()
    crawler._run_human_behavior_on_primary_page = AsyncMock(
        return_value={"status": "completed"}
    )
    crawler.search = AsyncMock()

    async def update_cookies(**_kwargs: object) -> None:
        events.append("update_cookies")

    client = SimpleNamespace(
        playwright_page=page,
        update_cookies=AsyncMock(side_effect=update_cookies),
    )
    crawler.create_xhs_client = AsyncMock(return_value=client)
    monkeypatch.setattr(crawler.ports, "install_project_runtime_hints", AsyncMock())
    monkeypatch.setattr(config, "ENABLE_CDP_MODE", True)
    monkeypatch.setattr(config, "CDP_HEADLESS", False)
    monkeypatch.setattr(config, "KEYWORDS", "青岛登录测试")
    monkeypatch.setattr(config, "CRAWLER_TYPE", "search")
    return crawler, context, client


def _install_observed_login_factory(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
) -> list[XiaoHongShuLogin]:
    instances: list[XiaoHongShuLogin] = []

    def factory(**kwargs: object) -> XiaoHongShuLogin:
        login = XiaoHongShuLogin(**kwargs)
        original_begin = login.begin

        async def observed_begin() -> None:
            events.append("login.begin")
            await original_begin()

        login.begin = observed_begin  # type: ignore[method-assign]
        instances.append(login)
        return login

    monkeypatch.setattr(xhs_login, "XiaoHongShuLogin", factory)
    return instances


def _login_flow(monkeypatch: pytest.MonkeyPatch, clock: _LoginFlowClock, page: _LoginFlowPage) -> None:
    # 预算显式使用假时钟；不替换全局 time.monotonic，否则事件循环的计时器也会停摆。
    monkeypatch.setattr(xhs_login.asyncio, "sleep", clock.sleep)
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_LOGIN_POLL_SECONDS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_STABLE_LOGIN_SECONDS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS", "180")

    async def dom_qr_ready(current_page: _LoginFlowPage) -> bool:
        assert current_page is page
        return XiaoHongShuLogin._QRCODE_SELECTOR in page.visible_selectors

    monkeypatch.setattr(xhs_login, "dom_qr_ready", dom_qr_ready)


def _ledger(crawler: XiaoHongShuCrawler) -> list[dict]:
    return list(load_ledger(crawler.capture.ledger.path).lines)


@pytest.mark.asyncio
async def test_startup_enters_qrcode_login_once_and_confirms_with_user_me(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _LoginFlowClock()
    page = _LoginFlowPage(visible_text="扫码登录 打开小红书扫一扫")
    page.visible_selectors.add(XiaoHongShuLogin._QRCODE_SELECTOR)
    events: list[str] = []
    crawler, context, client = _configure_browser_session_test(
        monkeypatch,
        tmp_path,
        page=page,
        events=events,
    )
    budget = XHSManualWaitBudget(limit_seconds=600, monotonic=clock.monotonic)
    crawler._manual_wait_budget = budget
    instances = _install_observed_login_factory(monkeypatch, events)
    _login_flow(monkeypatch, clock, page)

    def complete_scan(now: float) -> None:
        if now < 1 or XiaoHongShuLogin._PROFILE_SELECTORS[0] in page.visible_selectors:
            return
        page.visible_text = "首页 我"
        page.visible_selectors.discard(XiaoHongShuLogin._QRCODE_SELECTOR)
        page.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])
        # 扫码确认后前端自动再请求一次 user/me，页面不重载。
        page.emit_user_me(guest=False)

    clock.on_sleep = complete_scan

    await crawler._run_browser_session(object(), None, None)

    assert len(instances) == 1
    assert instances[0]._manual_wait_budget is budget
    assert crawler.context_page is page
    assert context.new_page_calls == 0 and page.reloads == 0
    assert crawler.launch_browser_with_cdp.await_count == 1
    assert events.count("login.begin") == 1
    assert not hasattr(crawler, "_pong_with_network_recovery")
    visits = [line for line in _ledger(crawler) if line["kind"] == "visit"]
    assert [(line["stage"], line["rule"], line["guest"]) for line in visits] == [
        ("login_state", "xhs.user_me", False)
    ]
    assert crawler.capture.role_of(page) == "primary"
    # search 模式下首页输入、点选“图文”、翻页停顿与初始行为阶段都在全量扫描内完成。
    crawler._run_human_behavior_on_primary_page.assert_not_awaited()
    crawler.search.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("guest_user_me", [False, True])
async def test_signed_in_ui_without_guest_false_user_me_reloads_once_then_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    guest_user_me: bool,
) -> None:
    clock = _LoginFlowClock()
    page = _LoginFlowPage(visible_text="首页 我")
    page.visible_selectors.add(XiaoHongShuLogin._PROFILE_SELECTORS[0])
    events: list[str] = []
    crawler, _context, _client = _configure_browser_session_test(
        monkeypatch,
        tmp_path,
        page=page,
        events=events,
    )
    crawler._manual_wait_budget = XHSManualWaitBudget(limit_seconds=600, monotonic=clock.monotonic)
    _install_observed_login_factory(monkeypatch, events)
    _login_flow(monkeypatch, clock, page)
    if guest_user_me:
        page.on_reload = lambda: page.emit_user_me(guest=True)
    append_event = MagicMock()
    monkeypatch.setattr(crawler.ports, "append_execution_event", append_event)

    with pytest.raises(RuntimeError, match=r"^xhs_login_state_unconfirmed$"):
        await crawler._run_browser_session(object(), None, None)

    assert page.reloads == 1
    append_event.assert_called_once()
    event_type, details = append_event.call_args.args
    assert event_type == "xhs_runtime_terminal"
    assert details["failure_type"] == "login_runtime_error"
    assert details["stop_detail"] == "xhs_login_state_unconfirmed"
    assert [line["action"] for line in _ledger(crawler) if line["kind"] == "action"] == ["reload"]
    assert not [line for line in _ledger(crawler) if line["kind"] == "visit"]
    crawler.create_xhs_client.assert_not_awaited()
    crawler.search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("visible_text", "terminal_code", "failure_type", "matched_marker"),
    [
        (
            "SMS Verification\nParameter error",
            "xhs_sms_verification_parameter_error",
            "sms_verification_terminal",
            "Parameter error",
        ),
        (
            "手机号登录\n今日短信验证码次数已达上限",
            "xhs_sms_verification_daily_limit",
            "sms_verification_terminal",
            "今日短信验证码次数已达上限",
        ),
        (
            "SMS Verification\nRequests too frequent",
            "xhs_sms_verification_rate_limited",
            "sms_verification_terminal",
            "Requests too frequent",
        ),
    ],
)
async def test_startup_sms_terminal_fails_immediately_without_retry_or_business_work(
    monkeypatch: pytest.MonkeyPatch,
    visible_text: str,
    terminal_code: str,
    failure_type: str,
    matched_marker: str,
    tmp_path: Path,
) -> None:
    clock = _LoginFlowClock()
    page = _LoginFlowPage(visible_text=visible_text)
    events: list[str] = []
    crawler, context, client = _configure_browser_session_test(
        monkeypatch,
        tmp_path,
        page=page,
        events=events,
    )
    budget = XHSManualWaitBudget(limit_seconds=600, monotonic=clock.monotonic)
    crawler._manual_wait_budget = budget
    instances = _install_observed_login_factory(monkeypatch, events)
    append_event = MagicMock()
    monkeypatch.setattr(crawler.ports, "append_execution_event", append_event)
    monkeypatch.setattr(xhs_login.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(xhs_login.asyncio, "sleep", clock.sleep)

    with pytest.raises(xhs_core.PlatformRuntimeError, match=f"^{terminal_code}$"):
        await crawler._run_browser_session(object(), None, None)

    assert len(instances) == 1
    assert instances[0]._manual_wait_budget is budget
    assert events == ["login.begin"]
    assert clock.sleep_calls == []
    assert context.new_page_calls == 0
    assert crawler.launch_browser_with_cdp.await_count == 1
    append_event.assert_called_once_with(
        "xhs_runtime_terminal",
        {
            "phase": "login",
            "failure_type": failure_type,
            "stop_reason": "runtime_failed",
            "stop_detail": terminal_code,
            "checkpoint_kind": "sms_verification",
            "manual_progress_observed": True,
            "matched_markers": [matched_marker],
            "retryable": False,
        },
    )
    crawler.create_xhs_client.assert_not_awaited()
    crawler._open_behavior_search_page_on_primary_page.assert_not_awaited()
    crawler._run_human_behavior_on_primary_page.assert_not_awaited()
    crawler.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_storage_state_file_is_not_read_or_rewritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot_path = tmp_path / "storage-state.json"
    snapshot_path.write_text("legacy-sentinel\n", encoding="utf-8")
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH",
        str(snapshot_path),
    )
    async_playwright = MagicMock(
        side_effect=AssertionError("legacy snapshot must fail before browser start")
    )
    crawler = XiaoHongShuCrawler()
    monkeypatch.setattr(crawler.ports, "async_playwright", async_playwright)

    with pytest.raises(
        RuntimeError,
        match=(
            "^xhs_deprecated_login_input:"
            "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH$"
        ),
    ):
        await crawler.start()

    assert snapshot_path.read_text(encoding="utf-8") == "legacy-sentinel\n"
    assert list(tmp_path.iterdir()) == [snapshot_path]
    async_playwright.assert_not_called()


@pytest.mark.asyncio
async def test_context_cookies_still_seed_the_api_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = SimpleNamespace()
    crawler.context_page = SimpleNamespace()
    crawler._browser_identity_headers = AsyncMock(
        return_value={"user-agent": "Chromium/140"}
    )
    convert_cookies = AsyncMock(
        return_value=(
            "web_session=current; a1=signed",
            {"web_session": "current", "a1": "signed"},
        )
    )
    monkeypatch.setattr(
        xhs_core,
        "convert_browser_context_cookies",
        convert_cookies,
    )

    client = await crawler.create_xhs_client(None)

    convert_cookies.assert_awaited_once_with(
        crawler.browser_context,
        urls=crawler.cookie_urls,
    )
    assert client.headers["Cookie"] == "web_session=current; a1=signed"
    assert client.cookie_dict == {"web_session": "current", "a1": "signed"}
    assert client.playwright_page is crawler.context_page


def test_login_terminal_writes_use_the_injected_worker_event_exit() -> None:
    """自 T09 迁入：登录两处终态写出都经注入端口，装配绑定的是 worker 事件出口，平台模块不直接持有出口。"""
    import ast
    import sys

    from trippostcollect.application import events
    from trippostcollect.platforms.entry import xhs_dependencies
    from trippostcollect.application.worker_inputs import worker_config

    source = Path(xhs_login.__file__).read_text(encoding="utf-8")
    method = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_qrcode_login"
    )
    calls = [
        node for node in ast.walk(method)
        if isinstance(node, ast.Call) and node.args
        and isinstance(node.args[0], ast.Constant) and node.args[0].value == "xhs_runtime_terminal"
    ]
    assert len(calls) == 2
    assert {ast.unparse(call.func) for call in calls} == {"self.ports.append_execution_event"}
    ports = xhs_dependencies(worker_config(), repair=False)["ports"]
    assert ports.append_execution_event is events.append_worker_execution_event
    for name, module in list(sys.modules.items()):
        if name.startswith("trippostcollect.platforms.xhs"):
            assert not hasattr(module, "append_execution_event"), name
            assert not hasattr(module, "append_worker_execution_event"), name
