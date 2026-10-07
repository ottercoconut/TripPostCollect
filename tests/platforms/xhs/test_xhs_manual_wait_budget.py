from __future__ import annotations

import time

import pytest

from trippostcollect.platforms.xhs.manual_wait import (
    XHS_MANUAL_WAIT_BUDGET_EXHAUSTED,
    XHS_MANUAL_WAIT_BUDGET_SECONDS,
    XHSManualWaitBudget,
    XHSManualWaitBudgetExhausted,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize("raw_value", ["invalid", "nan", "inf", "-inf"])
def test_invalid_manual_budget_env_uses_safe_default(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str,
) -> None:
    clock = FakeClock()
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS", raw_value)

    budget = XHSManualWaitBudget.from_environment(monotonic=clock.monotonic)

    assert budget.limit_seconds == XHS_MANUAL_WAIT_BUDGET_SECONDS


def test_zero_manual_budget_is_terminal_before_a_wait_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS", "0")
    budget = XHSManualWaitBudget.from_environment(monotonic=clock.monotonic)

    with pytest.raises(
        XHSManualWaitBudgetExhausted,
        match=f"^{XHS_MANUAL_WAIT_BUDGET_EXHAUSTED}$",
    ):
        budget.start("initial_qrcode_login")

    assert budget.manual_elapsed_seconds == 0.0
    assert budget.remaining_seconds == 0.0


def test_manual_budget_is_capped_at_six_hundred_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS", "3600")

    budget = XHSManualWaitBudget.from_environment(monotonic=clock.monotonic)

    assert budget.limit_seconds == 600.0


def test_normal_crawl_time_and_wall_clock_jumps_do_not_consume_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    budget = XHSManualWaitBudget(
        limit_seconds=600.0,
        monotonic=clock.monotonic,
    )

    clock.advance(10_000.0)
    monkeypatch.setattr(time, "time", lambda: -9_999_999.0)
    assert budget.manual_elapsed_seconds == 0.0
    assert budget.remaining_seconds == 600.0

    with budget.start("initial_qrcode_login"):
        clock.advance(25.0)

    clock.advance(50_000.0)
    monkeypatch.setattr(time, "time", lambda: 9_999_999.0)
    assert budget.manual_elapsed_seconds == 25.0
    assert budget.remaining_seconds == 575.0


def test_overlapping_and_nested_waiters_are_charged_once() -> None:
    clock = FakeClock()
    budget = XHSManualWaitBudget(limit_seconds=20.0, monotonic=clock.monotonic)

    first = budget.start("initial_qrcode_login")
    clock.advance(2.0)
    second = budget.start("manual_verification_popup")
    clock.advance(3.0)
    first.close()
    clock.advance(4.0)
    second.close()

    assert budget.manual_elapsed_seconds == 9.0
    assert budget.remaining_seconds == 11.0


def test_paused_network_wait_does_not_consume_manual_budget() -> None:
    clock = FakeClock()
    budget = XHSManualWaitBudget(limit_seconds=20.0, monotonic=clock.monotonic)

    with budget.start("midrun_login_recovery") as ticket:
        clock.advance(2.0)
        with ticket.paused():
            clock.advance(100.0)
        clock.advance(3.0)

    assert budget.manual_elapsed_seconds == 5.0
    assert budget.remaining_seconds == 15.0


def test_nested_network_pauses_resume_only_after_outer_pause_exits() -> None:
    clock = FakeClock()
    budget = XHSManualWaitBudget(limit_seconds=20.0, monotonic=clock.monotonic)

    with budget.start("midrun_login_recovery") as ticket:
        clock.advance(1.0)
        with ticket.paused():
            clock.advance(10.0)
            with ticket.paused():
                clock.advance(20.0)
            clock.advance(30.0)
        clock.advance(2.0)

    assert budget.manual_elapsed_seconds == 3.0
    assert budget.remaining_seconds == 17.0


def test_paused_waiter_does_not_hide_an_overlapping_manual_wait() -> None:
    clock = FakeClock()
    budget = XHSManualWaitBudget(limit_seconds=20.0, monotonic=clock.monotonic)

    with budget.start("midrun_login_recovery") as network_waiter:
        with network_waiter.paused():
            with budget.start("manual_verification_popup"):
                clock.advance(4.0)

    assert budget.manual_elapsed_seconds == 4.0


def test_each_popup_uses_the_existing_remainder_and_exact_boundary() -> None:
    clock = FakeClock()
    budget = XHSManualWaitBudget(limit_seconds=10.0, monotonic=clock.monotonic)

    with budget.start("manual_verification_popup:first"):
        clock.advance(4.0)
    with budget.start("manual_verification_popup:second"):
        clock.advance(5.0)
    with budget.start("manual_verification_popup:third") as final_ticket:
        assert final_ticket.remaining_seconds == 1.0
        clock.advance(1.0)
        with pytest.raises(
            XHSManualWaitBudgetExhausted,
            match=f"^{XHS_MANUAL_WAIT_BUDGET_EXHAUSTED}$",
        ):
            final_ticket.raise_if_exhausted()

    with pytest.raises(XHSManualWaitBudgetExhausted):
        budget.start("manual_verification_popup:fourth")
    assert budget.manual_elapsed_seconds == 10.0
