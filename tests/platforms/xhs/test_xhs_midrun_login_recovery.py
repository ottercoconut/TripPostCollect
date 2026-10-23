"""轮中登录恢复：可见 UI 恢复且恢复开始之后截获到 guest=false 的 user/me 才算完成；不导航。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import trippostcollect.platforms.xhs.core as xhs_core
import trippostcollect.platforms.xhs.session as xhs_session
from support.page_events import FakeContext, FakePage
from trippostcollect.platforms.xhs.manual_wait import (
    XHSManualWaitBudget,
    XHSManualWaitBudgetExhausted,
)
from trippostcollect.runtime.visit_ledger import load_ledger

from .support import XiaoHongShuCrawler, make_capture


USER_ME = "https://edith.xiaohongshu.com/api/sns/web/v2/user/me"


class Page(FakePage):
    def __init__(self) -> None:
        super().__init__("https://www.xiaohongshu.com/search_result_ai?keyword=x&source=web")
        self.closed = False
        self.reloads = 0
        self.on_reload = None
        self.bring_to_front = AsyncMock()

    def is_closed(self) -> bool:
        return self.closed

    async def reload(self, *, wait_until: str, timeout: int) -> None:
        assert (wait_until, timeout) == ("domcontentloaded", 30_000)
        self.reloads += 1
        if self.on_reload is not None:
            self.on_reload()


def emit_user_me(page: Page, *, guest: bool = False) -> None:
    payload = {"success": True, "data": {"guest": guest, "user_id": "" if guest else "self"}}
    page.respond(page.request("GET", USER_ME), body=json.dumps(payload).encode())


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
    tmp_path,
    states: list[dict[str, object]],
    *,
    wait_seconds: int = 12,
    profile_results: list[bool] | None = None,
    user_me_on_probe: list[bool] | None = None,
) -> tuple[XiaoHongShuCrawler, Page, AsyncMock, list[float]]:
    """`user_me_on_probe[i]` 为真时，第 i 次看到登录 UI 时前端自发一次 guest=false 的 user/me。"""
    monkeypatch.setattr(xhs_session, "XHS_LOGIN_ME_CONFIRM_SECONDS", 0.05)
    crawler = XiaoHongShuCrawler()
    page = Page()
    context = FakeContext()
    context.pages.append(page)
    crawler.context_page = page
    crawler.browser_context = context
    crawler.capture = make_capture(tmp_path, context=context)
    crawler.capture.install(page)
    probes = list(profile_results) if profile_results is not None else []
    emits = list(user_me_on_probe or [])

    async def profile_ui() -> bool:
        visible = probes.pop(0) if probes else False
        if visible and emits and emits.pop(0):
            emit_user_me(page)
        return visible

    crawler._profile_ui_visible = AsyncMock(side_effect=profile_ui)
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

    crawler._clock = clock
    crawler._popup_monotonic = monotonic
    crawler._popup_sleep = sleep
    crawler._manual_wait_budget = XHSManualWaitBudget(
        limit_seconds=wait_seconds,
        monotonic=monotonic,
    )
    return crawler, page, goto, sleeps


def ledger_lines(crawler) -> list[dict]:
    return list(load_ledger(crawler.capture.ledger.path).lines)


def login_visits(crawler) -> list[dict]:
    return [line for line in ledger_lines(crawler) if line["kind"] == "visit" and line["stage"] == "login_state"]


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
async def test_sms_checkpoint_with_stale_profile_never_probes_or_reloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    crawler, page, goto, sleeps = _crawler(
        monkeypatch,
        tmp_path,
        [
            _state(manual=("SMS Verification",)),
            _state(text=""),
            _state(manual=("Parameter error",)),
        ],
        wait_seconds=6,
        profile_results=[True],
        user_me_on_probe=[True],
    )

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await crawler._wait_for_midrun_login_recovery()

    assert crawler._profile_ui_visible.await_count == 0
    assert page.reloads == 0 and goto.await_count == 0
    assert page.bring_to_front.await_count >= 2
    # #107：轮中登录恢复照常无条件置前，不读页面可见性。
    assert crawler._page_focus["samples"] == {} and crawler._page_focus["read_failures"] == {}
    assert sum(sleeps) == pytest.approx(6.0)
    assert login_visits(crawler) == []


@pytest.mark.asyncio
async def test_manual_latch_requires_two_nonblank_clear_observations(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    crawler, page, goto, _sleeps = _crawler(
        monkeypatch,
        tmp_path,
        [
            _state(manual=("验证码",)),
            _state(text=""),
            _state(text="signed-in shell"),
            _state(text="signed-in shell"),
            _state(text="signed-in shell"),
        ],
        profile_results=[True],
        user_me_on_probe=[True],
    )

    assert await crawler._wait_for_midrun_login_recovery() is True

    assert crawler._profile_ui_visible.await_count == 1
    # 恢复后不导航：调用方按当前页面继续，或按会话重置重新搜索。
    goto.assert_not_awaited()
    assert page.reloads == 0
    (visit,) = login_visits(crawler)
    assert visit["rule"] == "xhs.user_me" and visit["guest"] is False and visit["subject_id"] is None
    assert "self" not in json.dumps(visit)


@pytest.mark.asyncio
async def test_reappearing_checkpoint_resets_clear_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    crawler, _page, goto, _sleeps = _crawler(
        monkeypatch,
        tmp_path,
        [
            _state(manual=("验证码",)),
            _state(text="signed-in shell"),
            _state(manual=("安全验证",)),
            _state(text="signed-in shell"),
            _state(text="signed-in shell"),
            _state(text="signed-in shell"),
        ],
        profile_results=[True],
        user_me_on_probe=[True],
    )

    assert await crawler._wait_for_midrun_login_recovery()
    assert crawler._profile_ui_visible.await_count == 1
    goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkpoint_race_after_user_me_blocks_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    crawler, _page, goto, _sleeps = _crawler(
        monkeypatch,
        tmp_path,
        [
            _state(text="signed-in shell"),
            _state(manual=("SMS Verification",)),
        ],
        wait_seconds=6,
        profile_results=[True],
        user_me_on_probe=[True],
    )

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await crawler._wait_for_midrun_login_recovery()

    assert goto.await_count == 0
    assert login_visits(crawler) == []


@pytest.mark.asyncio
async def test_user_me_before_recovery_or_guest_true_does_not_confirm(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    crawler, page, _goto, _sleeps = _crawler(
        monkeypatch,
        tmp_path,
        [_state(text="signed-in shell")],
        profile_results=[True, True],
    )
    emit_user_me(page)
    await crawler.capture.wait_for("xhs.user_me", after_seq=0, timeout=1)
    page.on_reload = lambda: emit_user_me(page, guest=True)

    with pytest.raises(RuntimeError, match=r"^xhs_login_state_unconfirmed$"):
        await crawler._wait_for_midrun_login_recovery()

    assert page.reloads == 1
    assert login_visits(crawler) == []


@pytest.mark.asyncio
async def test_signed_in_shell_without_user_me_reloads_once_then_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    crawler, page, goto, _sleeps = _crawler(
        monkeypatch,
        tmp_path,
        [_state(text="signed-in shell")],
        wait_seconds=12,
        profile_results=[True] * 6,
    )

    with pytest.raises(RuntimeError, match=r"^xhs_login_state_unconfirmed$"):
        await crawler._wait_for_midrun_login_recovery()

    assert page.reloads == 1
    assert goto.await_count == 0
    reloads = [line for line in ledger_lines(crawler) if line["kind"] == "action"]
    assert [(line["action"], line["target"]) for line in reloads] == [("reload", {"route": "/search_result_ai"})]


@pytest.mark.asyncio
async def test_reload_after_confirmed_ui_triggers_user_me_and_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    crawler, page, _goto, _sleeps = _crawler(
        monkeypatch,
        tmp_path,
        [_state(text="signed-in shell")],
        profile_results=[True, True],
    )
    page.on_reload = lambda: emit_user_me(page)

    assert await crawler._wait_for_midrun_login_recovery()

    assert page.reloads == 1
    (visit,) = login_visits(crawler)
    reload_action = next(line for line in ledger_lines(crawler) if line["kind"] == "action")
    assert reload_action["seq"] < visit["seq"]


@pytest.mark.asyncio
async def test_terminal_restriction_preempts_wait_and_session_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    crawler, page, goto, sleeps = _crawler(
        monkeypatch,
        tmp_path,
        [_state(terminal="rate_limited")],
        profile_results=[True],
        user_me_on_probe=[True],
    )

    with pytest.raises(xhs_core.PlatformRuntimeError) as captured:
        await crawler._wait_for_midrun_login_recovery()

    assert captured.value.code == "xhs_rate_limited_terminal"
    assert crawler._profile_ui_visible.await_count == 0
    assert goto.await_count == 0 and page.reloads == 0
    assert sleeps == []


@pytest.mark.asyncio
async def test_user_me_wait_and_reload_do_not_use_manual_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    crawler, page, _goto, sleeps = _crawler(
        monkeypatch,
        tmp_path,
        [_state(text="signed-in shell")],
        wait_seconds=6,
        profile_results=[True, True],
    )

    def slow_reload() -> None:
        crawler._clock["now"] += 1000.0
        emit_user_me(page)

    page.on_reload = slow_reload

    assert await crawler._wait_for_midrun_login_recovery()

    assert sleeps == []
    assert crawler._manual_wait_budget.manual_elapsed_seconds == 0.0
    assert crawler._manual_wait_budget.remaining_seconds == 6.0


@pytest.mark.asyncio
async def test_recovery_keeps_original_qr_page_when_auxiliary_tab_closes(monkeypatch, tmp_path):
    crawler, primary, goto, _sleeps = _crawler(
        monkeypatch,
        tmp_path,
        [_state(manual=("扫码登录",)), _state(), _state(), _state()],
        profile_results=[True],
        user_me_on_probe=[True],
    )
    closed = {"auxiliary": False}
    auxiliary = SimpleNamespace(
        url="https://www.xiaohongshu.com/website-login/login",
        is_closed=lambda: closed["auxiliary"],
        bring_to_front=AsyncMock(),
    )
    crawler.browser_context.pages.append(auxiliary)
    crawler.browser_context.new_page = AsyncMock()
    observe = crawler._popup_checkpoint_state

    async def observe_and_close_auxiliary(page):
        assert page is primary
        closed["auxiliary"] = True
        return await observe(page)

    crawler._popup_checkpoint_state = observe_and_close_auxiliary
    assert await crawler._wait_for_midrun_login_recovery()
    assert crawler.context_page is primary
    assert primary.bring_to_front.await_count >= 2
    auxiliary.bring_to_front.assert_not_awaited()
    crawler.browser_context.new_page.assert_not_awaited()
    goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_original_page_fails_without_adopting_auxiliary(monkeypatch, tmp_path):
    crawler, primary, goto, _sleeps = _crawler(monkeypatch, tmp_path, [_state()])
    primary.closed = True
    crawler.browser_context.pages.append(SimpleNamespace(is_closed=lambda: False))
    crawler.browser_context.new_page = AsyncMock()
    with pytest.raises(RuntimeError, match="xhs_main_page_closed_unexpected"):
        await crawler._wait_for_midrun_login_recovery()
    assert crawler.context_page is primary
    crawler.browser_context.new_page.assert_not_awaited()
    goto.assert_not_awaited()
