from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from trippostcollect.platforms.xhs import login as login_module
from trippostcollect.platforms.xhs.login import XiaoHongShuLogin
from trippostcollect.platforms.xhs.manual_wait import XHSManualWaitBudgetExhausted
from trippostcollect.runtime import login_helpers

from .support import config, login_ports


class FakeContext:
    async def cookies(self) -> list[dict[str, str]]:
        return [{"name": "web_session", "value": "before"}]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_login() -> tuple[XiaoHongShuLogin, SimpleNamespace]:
    page = SimpleNamespace(reload=AsyncMock())
    login = XiaoHongShuLogin(
        login_type="qrcode",
        browser_context=FakeContext(),
        context_page=page,
        ports=login_ports,
    )
    return login, page


def configure_qrcode_flow(
    monkeypatch: pytest.MonkeyPatch,
    login: XiaoHongShuLogin,
    *,
    wait_seconds: int,
) -> tuple[AsyncMock, MagicMock]:
    clock = FakeClock()
    find_qrcode = AsyncMock(return_value="qr-image")
    show_qrcode = MagicMock()
    monkeypatch.setattr(login_ports, "find_login_qrcode", find_qrcode)
    monkeypatch.setattr(login_helpers, "show_qrcode", show_qrcode)
    monkeypatch.setattr(login_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(login_module.asyncio, "sleep", clock.sleep)
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS",
        str(wait_seconds),
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS", "180")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_LOGIN_POLL_SECONDS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_STABLE_LOGIN_SECONDS", "5")

    def observe_qr() -> dict[str, object]:
        return {
            "terminal_security": [],
            "terminal_login_error": [],
            "manual_in_progress": False,
            "profile_visible": False,
            "qr_visible": True,
            "qr_expired": False,
            "pages": [{"login_or_qr": ["扫码登录"]}],
        }

    async def check_login_state(_session: str) -> bool:
        login._last_login_observation = observe_qr()
        return False

    login._check_login_state_once = AsyncMock(side_effect=check_login_state)
    return find_qrcode, show_qrcode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enable_cdp", "cdp_headless", "playwright_headless"),
    [
        (True, False, True),
        (False, True, False),
    ],
)
async def test_qrcode_never_schedules_initial_or_refreshed_os_preview(
    monkeypatch: pytest.MonkeyPatch,
    enable_cdp: bool,
    cdp_headless: bool,
    playwright_headless: bool,
) -> None:
    login, page = make_login()
    find_qrcode, show_qrcode = configure_qrcode_flow(
        monkeypatch,
        login,
        wait_seconds=182,
    )
    monkeypatch.setattr(config, "ENABLE_CDP_MODE", enable_cdp)
    monkeypatch.setattr(config, "CDP_HEADLESS", cdp_headless)
    monkeypatch.setattr(config, "HEADLESS", playwright_headless)
    get_running_loop = MagicMock(
        side_effect=AssertionError("XHS login must not schedule an OS preview")
    )
    monkeypatch.setattr(
        login_module.asyncio,
        "get_running_loop",
        get_running_loop,
    )

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    assert find_qrcode.await_count == 2
    assert page.reload.await_args_list == [
        call(wait_until="domcontentloaded", timeout=30_000)
    ]
    get_running_loop.assert_not_called()
    show_qrcode.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enable_cdp", "cdp_headless", "playwright_headless"),
    [
        (True, True, False),
        (False, False, True),
    ],
)
async def test_headless_qrcode_stays_in_browser_without_os_preview(
    monkeypatch: pytest.MonkeyPatch,
    enable_cdp: bool,
    cdp_headless: bool,
    playwright_headless: bool,
) -> None:
    login, page = make_login()
    find_qrcode, show_qrcode = configure_qrcode_flow(
        monkeypatch,
        login,
        wait_seconds=2,
    )

    get_running_loop = MagicMock(
        side_effect=AssertionError("headless XHS login must not open an OS preview")
    )
    monkeypatch.setattr(config, "ENABLE_CDP_MODE", enable_cdp)
    monkeypatch.setattr(config, "CDP_HEADLESS", cdp_headless)
    monkeypatch.setattr(config, "HEADLESS", playwright_headless)
    monkeypatch.setattr(
        login_module.asyncio,
        "get_running_loop",
        get_running_loop,
    )

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    find_qrcode.assert_awaited_once()
    page.reload.assert_not_awaited()
    get_running_loop.assert_not_called()
    show_qrcode.assert_not_called()


@pytest.mark.asyncio
async def test_removed_preview_hooks_cannot_abort_headless_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login, page = make_login()
    find_qrcode, show_qrcode = configure_qrcode_flow(
        monkeypatch,
        login,
        wait_seconds=2,
    )
    show_qrcode.side_effect = AssertionError("legacy OS preview invoked")
    get_running_loop = MagicMock(
        side_effect=AssertionError("legacy preview scheduling invoked")
    )
    monkeypatch.setattr(config, "ENABLE_CDP_MODE", True)
    monkeypatch.setattr(config, "CDP_HEADLESS", True)
    monkeypatch.setattr(config, "HEADLESS", False)
    monkeypatch.setattr(
        login_module.asyncio,
        "get_running_loop",
        get_running_loop,
    )

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await login.login_by_qrcode()

    find_qrcode.assert_awaited_once()
    get_running_loop.assert_not_called()
    show_qrcode.assert_not_called()
    page.reload.assert_not_awaited()


def test_xhs_login_class_has_no_os_qrcode_preview_path() -> None:
    source = inspect.getsource(XiaoHongShuLogin)

    assert "show_qrcode" not in source
    assert "run_in_executor" not in source
    assert "startfile" not in source
    assert "xdg-open" not in source
    assert "subprocess" not in source
    assert not hasattr(XiaoHongShuLogin, "_show_qrcode_for_headless_browser")
