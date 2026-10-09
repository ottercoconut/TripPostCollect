"""#75：小红书人工等待信号；父层据此冻结无进展看门狗计时，机制与网络暂停一致。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from trippostcollect.platforms.xhs import behavior as xhs_behavior
from trippostcollect.platforms.xhs.manual_wait import (
    XHSManualWaitBudget,
    XHSManualWaitBudgetExhausted,
)
from trippostcollect.runtime import process
from trippostcollect.xhs import operator_wait
from trippostcollect.xhs.operator_wait import (
    OperatorWaitSignal,
    current_operator_wait_signal,
    operator_wait_diagnostics_path,
)


@pytest.fixture
def evidence_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "logs" / "behavior_evidence.json"
    monkeypatch.setenv(operator_wait.BEHAVIOR_EVIDENCE_ENV, str(path))
    return path


def diagnostic_state(evidence_path: Path) -> tuple[str, str]:
    payload = json.loads(operator_wait_diagnostics_path(evidence_path).read_text(encoding="utf-8"))
    return payload["state"], payload["stage"]


def parent_view(evidence_path: Path) -> str:
    return process.xhs_operator_wait_state_from_diagnostics(
        operator_wait_diagnostics_path(evidence_path)
    )


def test_nested_waits_publish_one_episode_readable_by_parent(evidence_path: Path) -> None:
    signal = current_operator_wait_signal()
    assert signal is current_operator_wait_signal()
    assert parent_view(evidence_path) == "unknown"

    signal.enter("initial_qrcode_login")
    signal.enter("pre_search_verification")
    assert diagnostic_state(evidence_path) == ("waiting", "initial_qrcode_login")
    assert parent_view(evidence_path) == "operator_waiting"
    signal.exit()
    assert parent_view(evidence_path) == "operator_waiting"
    signal.exit()
    assert diagnostic_state(evidence_path) == ("ended", "initial_qrcode_login")
    assert parent_view(evidence_path) == "operator_wait_ended"
    signal.exit()
    assert not signal.active


def test_unconfigured_signal_only_counts(tmp_path: Path) -> None:
    signal = OperatorWaitSignal(None)
    with signal.waiting("api_captcha_verification"):
        assert signal.active
    assert not signal.active
    assert list(tmp_path.iterdir()) == []


def test_long_wait_keeps_diagnostic_fresh_and_late_refresh_cannot_reopen(
    evidence_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operator_wait, "OPERATOR_WAIT_REFRESH_SECONDS", 0.05)
    signal = current_operator_wait_signal()
    path = operator_wait_diagnostics_path(evidence_path)
    signal.enter("midrun_login_recovery")
    first = json.loads(path.read_text(encoding="utf-8"))["updated_at"]
    deadline = time.monotonic() + 5
    while json.loads(path.read_text(encoding="utf-8"))["updated_at"] == first:
        assert time.monotonic() < deadline, "refresh did not rewrite the diagnostic"
        time.sleep(0.02)
    signal.exit()
    time.sleep(0.2)
    assert diagnostic_state(evidence_path) == ("ended", "midrun_login_recovery")


def test_shared_manual_budget_charging_drives_the_signal(evidence_path: Path) -> None:
    clock = [0.0]
    budget = XHSManualWaitBudget(limit_seconds=600, monotonic=lambda: clock[0])
    signal = current_operator_wait_signal()

    ticket = budget.start("api_captcha_verification")
    assert signal.active
    assert diagnostic_state(evidence_path) == ("waiting", "api_captcha_verification")
    # 自动化子操作不计人工预算，看门狗同步恢复计时。
    with ticket.paused():
        assert not signal.active
        assert diagnostic_state(evidence_path)[0] == "ended"
    assert signal.active
    overlapping = budget.start("creator_profile_verification")
    overlapping.close()
    assert signal.active
    ticket.close()
    assert not signal.active
    assert parent_view(evidence_path) == "operator_wait_ended"


def test_exhausted_budget_keeps_existing_error_and_ends_wait(evidence_path: Path) -> None:
    clock = [0.0]
    budget = XHSManualWaitBudget(limit_seconds=600, monotonic=lambda: clock[0])
    signal = current_operator_wait_signal()
    with pytest.raises(XHSManualWaitBudgetExhausted):
        with budget.start("midrun_login_recovery") as ticket:
            clock[0] = 600.0
            ticket.raise_if_exhausted()
    assert not signal.active
    assert parent_view(evidence_path) == "operator_wait_ended"
    with pytest.raises(XHSManualWaitBudgetExhausted):
        budget.start("initial_qrcode_login")
    assert not signal.active


def test_continuity_verification_wait_is_one_operator_episode(
    evidence_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal = current_operator_wait_signal()
    observed: list[bool] = []

    async def inner(*_args: object, **_kwargs: object) -> tuple[str, dict, dict]:
        observed.append(signal.active)
        raise RuntimeError("xhs_continuity_verification_timeout")

    monkeypatch.setattr(xhs_behavior, "_wait_for_xhs_continuity_verification", inner)
    with pytest.raises(RuntimeError, match="verification_timeout"):
        asyncio.run(
            xhs_behavior.wait_for_xhs_continuity_verification(
                object(),
                evidence={},
                evidence_path=evidence_path,
                stage="before_search",
                initial_challenge="captcha",
                initial_text="",
                initial_markers={},
                write_evidence=lambda *_args: None,
            )
        )
    assert observed == [True]
    assert diagnostic_state(evidence_path) == ("ended", "continuity_verification:before_search")


def test_search_ready_freezes_only_its_verification_segment(
    evidence_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal = current_operator_wait_signal()
    observed: list[bool] = []

    async def inner(_page, _events, *, timeout_seconds, operator_wait):
        observed.append(signal.active)
        operator_wait.begin()
        observed.append(signal.active)
        raise RuntimeError("page closed during verification")

    monkeypatch.setattr(xhs_behavior, "_wait_for_xhs_search_ready", inner)
    with pytest.raises(RuntimeError, match="page closed"):
        asyncio.run(xhs_behavior.wait_for_xhs_search_ready(object(), []))
    assert observed == [False, True]
    assert not signal.active
    assert diagnostic_state(evidence_path) == ("ended", "pre_search_verification")
