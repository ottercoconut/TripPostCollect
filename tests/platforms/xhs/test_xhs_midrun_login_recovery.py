from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import trippostcollect.platforms.xhs.core as xhs_core
import trippostcollect.platforms.xhs.session as xhs_session
from trippostcollect.platforms.xhs.manual_wait import (
    XHSManualWaitBudget,
    XHSManualWaitBudgetExhausted,
)

from .support import XiaoHongShuCrawler


def _state(
    *,
    text: str = "page rendered",
    manual: tuple[str, ...] = (),
    terminal: str = "",
) -> dict[str, object]:
    return {
        "closed": False,
        "visible_text": text,
        "manual_markers": list(manual),
        "terminal": terminal,
    }


def _crawler(
    monkeypatch: pytest.MonkeyPatch,
    states: list[dict[str, object]],
    *,
    wait_seconds: int = 12,
    profile_results: list[bool] | None = None,
    pong_results: list[bool] | None = None,
) -> tuple[XiaoHongShuCrawler, AsyncMock, AsyncMock, list[float]]:
    crawler = XiaoHongShuCrawler()
    page = SimpleNamespace(bring_to_front=AsyncMock(), is_closed=lambda: False)
    crawler.context_page = page
    crawler.browser_context = object()
    crawler.cookie_urls = [crawler.index_url]
    crawler._profile_ui_visible = AsyncMock(
        side_effect=profile_results if profile_results is not None else False
    )
    crawler.xhs_client = SimpleNamespace(
        update_cookies=AsyncMock(),
        pong=AsyncMock(side_effect=pong_results if pong_results is not None else False),
    )
    goto = AsyncMock()
    crawler._goto_with_deadline = goto

    observations = list(states)

    async def observe(_page: object) -> dict[str, object]:
        if len(observations) > 1:
            return observations.pop(0)
        return observations[0]

    crawler._popup_checkpoint_state = observe
    clock = {"now": 100.0}
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock["now"]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    crawler._popup_monotonic = monotonic
    crawler._popup_sleep = sleep
    crawler._manual_wait_budget = XHSManualWaitBudget(
        limit_seconds=wait_seconds,
        monotonic=monotonic,
    )
    return crawler, page.bring_to_front, goto, sleeps


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_text", ["SMS Verification", "Parameter error"])
async def test_actual_checkpoint_classifier_keeps_observed_sms_modal_manual(
    monkeypatch: pytest.MonkeyPatch,
    visible_text: str,
) -> None:
    crawler = XiaoHongShuCrawler()
    page = SimpleNamespace(is_closed=lambda: False, url="https://www.xiaohongshu.com/")
    monkeypatch.setattr(
        xhs_session,
        "inspect_visible_page_state",
        AsyncMock(return_value=(visible_text, {})),
    )

    state = await crawler._popup_checkpoint_state(page)

    assert visible_text.casefold() in state["manual_markers"]


@pytest.mark.asyncio
async def test_sms_checkpoint_with_stale_profile_never_probes_or_navigates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, foreground, goto, sleeps = _crawler(
        monkeypatch,
        [
            _state(manual=("SMS Verification",)),
            _state(text=""),
            _state(manual=("Parameter error",)),
        ],
        wait_seconds=6,
        profile_results=[True],
        pong_results=[True],
    )

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await crawler._wait_for_midrun_login_recovery("青岛太平角旅游")

    assert crawler._profile_ui_visible.await_count == 0
    assert crawler.xhs_client.pong.await_count == 0
    assert goto.await_count == 0
    assert foreground.await_count >= 2
    assert sum(sleeps) == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_manual_latch_requires_two_nonblank_clear_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _foreground, goto, _sleeps = _crawler(
        monkeypatch,
        [
            _state(manual=("验证码",)),
            _state(text=""),
            _state(text="signed-in shell"),
            _state(text="signed-in shell"),
            _state(text="signed-in shell"),
        ],
        profile_results=[True],
        pong_results=[True],
    )

    recovered = await crawler._wait_for_midrun_login_recovery("青岛太平角旅游")

    assert recovered is True
    assert crawler._profile_ui_visible.await_count == 1
    assert crawler.xhs_client.pong.await_count == 1
    goto.assert_awaited_once()
    assert goto.await_args.kwargs["stage"] == "midrun_login_recovered"
    assert "keyword=%E9%9D%92%E5%B2%9B%E5%A4%AA%E5%B9%B3%E8%A7%92%E6%97%85%E6%B8%B8" in goto.await_args.args[1]


@pytest.mark.asyncio
async def test_reappearing_checkpoint_resets_clear_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _foreground, goto, _sleeps = _crawler(
        monkeypatch,
        [
            _state(manual=("验证码",)),
            _state(text="signed-in shell"),
            _state(manual=("安全验证",)),
            _state(text="signed-in shell"),
            _state(text="signed-in shell"),
            _state(text="signed-in shell"),
        ],
        profile_results=[True],
        pong_results=[True],
    )

    assert await crawler._wait_for_midrun_login_recovery("青岛太平角旅游")
    assert crawler._profile_ui_visible.await_count == 1
    goto.assert_awaited_once()


@pytest.mark.asyncio
async def test_checkpoint_race_after_pong_blocks_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _foreground, goto, _sleeps = _crawler(
        monkeypatch,
        [
            _state(text="signed-in shell"),
            _state(manual=("SMS Verification",)),
        ],
        wait_seconds=6,
        profile_results=[True],
        pong_results=[True],
    )

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await crawler._wait_for_midrun_login_recovery("青岛太平角旅游")

    assert crawler.xhs_client.pong.await_count == 1
    assert goto.await_count == 0


@pytest.mark.asyncio
async def test_profile_shell_without_self_info_never_navigates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _foreground, goto, _sleeps = _crawler(
        monkeypatch,
        [_state(text="signed-in shell")],
        wait_seconds=12,
        profile_results=[True] * 6,
        pong_results=[False, False],
    )

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await crawler._wait_for_midrun_login_recovery("青岛太平角旅游")

    assert crawler.xhs_client.pong.await_count == 2
    assert goto.await_count == 0


@pytest.mark.asyncio
async def test_terminal_restriction_preempts_wait_and_session_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _foreground, goto, sleeps = _crawler(
        monkeypatch,
        [_state(terminal="rate_limited")],
        profile_results=[True],
        pong_results=[True],
    )

    with pytest.raises(xhs_core.PlatformRuntimeError) as captured:
        await crawler._wait_for_midrun_login_recovery("青岛太平角旅游")

    assert captured.value.code == "xhs_rate_limited_terminal"
    assert crawler._profile_ui_visible.await_count == 0
    assert crawler.xhs_client.pong.await_count == 0
    assert goto.await_count == 0
    assert sleeps == []


@pytest.mark.asyncio
async def test_recovered_session_does_not_hide_navigation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _foreground, goto, _sleeps = _crawler(
        monkeypatch,
        [_state(text="signed-in shell")],
        profile_results=[True],
        pong_results=[True],
    )
    goto.side_effect = RuntimeError("navigation still offline")

    with pytest.raises(RuntimeError, match="navigation still offline"):
        await crawler._wait_for_midrun_login_recovery("青岛太平角旅游")

    goto.assert_awaited_once()


@pytest.mark.asyncio
async def test_midrun_network_confirmation_and_navigation_do_not_use_manual_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler, _foreground, goto, sleeps = _crawler(
        monkeypatch,
        [_state(text="signed-in shell")],
        wait_seconds=6,
        profile_results=[True],
    )

    async def delayed_network_confirmation(*, stage: str) -> bool:
        assert stage == "midrun_login_confirmation"
        await crawler._popup_sleep(100.0)
        return True

    async def delayed_navigation(*_args, **_kwargs) -> None:
        await crawler._popup_sleep(1000.0)

    crawler._pong_with_network_recovery = delayed_network_confirmation
    goto.side_effect = delayed_navigation

    assert await crawler._wait_for_midrun_login_recovery("青岛太平角旅游")

    assert sleeps == [100.0, 1000.0]
    assert crawler._manual_wait_budget.manual_elapsed_seconds == 0.0
    assert crawler._manual_wait_budget.remaining_seconds == 6.0


@pytest.mark.asyncio
async def test_recovery_keeps_original_qr_page_when_auxiliary_tab_closes(monkeypatch):
    crawler, foreground, goto, _sleeps = _crawler(
        monkeypatch,
        [_state(manual=("扫码登录",)), _state(), _state(), _state()],
        profile_results=[True],
        pong_results=[True],
    )
    primary = crawler.context_page
    auxiliary = SimpleNamespace(
        url="https://www.xiaohongshu.com/website-login/login",
        is_closed=lambda: closed["auxiliary"],
        bring_to_front=AsyncMock(),
    )
    closed = {"auxiliary": False}
    crawler.browser_context = SimpleNamespace(
        pages=[primary, auxiliary], new_page=AsyncMock(),
    )
    crawler.xhs_client.playwright_page = primary
    observe = crawler._popup_checkpoint_state

    async def observe_and_close_auxiliary(page):
        assert page is primary
        closed["auxiliary"] = True
        return await observe(page)

    crawler._popup_checkpoint_state = observe_and_close_auxiliary
    assert await crawler._wait_for_midrun_login_recovery()
    assert crawler.context_page is primary
    assert crawler.xhs_client.playwright_page is primary
    assert foreground.await_count >= 2
    auxiliary.bring_to_front.assert_not_awaited()
    crawler.browser_context.new_page.assert_not_awaited()
    goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_original_page_fails_without_adopting_auxiliary(monkeypatch):
    crawler, _foreground, goto, _sleeps = _crawler(monkeypatch, [_state()])
    primary = crawler.context_page
    primary.is_closed = lambda: True
    crawler.browser_context = SimpleNamespace(
        pages=[SimpleNamespace(is_closed=lambda: False)], new_page=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="xhs_main_page_closed_unexpected"):
        await crawler._wait_for_midrun_login_recovery()
    assert crawler.context_page is primary
    crawler.browser_context.new_page.assert_not_awaited()
    goto.assert_not_awaited()
