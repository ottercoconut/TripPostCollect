"""TripPostCollect tests for failure classifier."""

from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

failure_classifier = import_module("trippostcollect.application.failures")


@pytest.mark.parametrize("detail", ["xhs_batch_checkpoint_filenotfounderror", "xhs_batch_checkpoint_ack_timeout"])
def test_checkpoint_failure_beats_incidental_traceback_timeout(detail: str) -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr=f"run(main, cleanup_timeout_seconds=15.0)\nRuntimeError: {detail}\n",
        meta={"platform": "xhs"},
    )
    assert result["failure_type"] == "runtime_failed"
    assert result["reason"] == detail
    assert result["retryable"] is False


def test_source_exhaustion_failure_beats_incidental_rate_text() -> None:
    summary = {
        "import_completion_met": False,
        "failure_reason": "source_exhaustion_not_persisted",
        "formal_validation": {
            "stop_reason": "source_not_exhausted",
            "invalid_reason_counts": {"raw_text_containing_429": 1},
        },
    }

    result = failure_classifier.classify_attempt(exit_code=2, summary=summary)

    assert result["status"] == "retry_wait"
    assert result["failure_type"] == "source_exhaustion_not_persisted"
    assert result["reason"] == "formal_source_exhaustion_not_persisted:source_not_exhausted"


def test_sms_code_login_text_is_login_required_not_captcha() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr="手机号登录 获取验证码 Login state result: False",
    )

    assert result["status"] == "login_required"
    assert result["failure_type"] == "login_required"


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("SMS Verification Parameter error Refresh", "xhs_sms_verification_parameter_error"),
        ("短信验证：参数错误，请刷新", "xhs_sms_verification_parameter_error"),
        ("今日短信验证码次数已达上限", "xhs_sms_verification_daily_limit"),
        ("Daily SMS quota exhausted", "xhs_sms_verification_daily_limit"),
        ("获取验证码操作过于频繁，请稍后再试", "xhs_sms_verification_rate_limited"),
        ("SMS verification requests are too frequent", "xhs_sms_verification_rate_limited"),
    ],
)
def test_xhs_sms_terminal_challenges_are_not_retryable(
    message: str,
    reason: str,
) -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr=message,
        meta={"platform": "xhs"},
    )

    assert result == {
        "status": "blocked",
        "failure_type": "sms_verification_terminal",
        "retryable": False,
        "wait_seconds": 0,
        "reason": reason,
    }


def test_xhs_stable_parameter_error_beats_incidental_login_trace() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr=(
            "login_by_qrcode check_login_state_once 手机号登录 验证码\n"
            "RuntimeError: xhs_login_verification_terminal:Parameter error"
        ),
        meta={"platform": "xhs"},
    )

    assert result == {
        "status": "blocked",
        "failure_type": "sms_verification_terminal",
        "retryable": False,
        "wait_seconds": 0,
        "reason": "xhs_sms_verification_parameter_error",
    }


def test_xhs_runtime_terminal_event_beats_incidental_stderr_markers() -> None:
    summary = (
        {
            "runtime_blocker": {
                "source": "xhs_runtime_terminal",
                "status": "failed_final",
                "failure_type": "browser_target_closed",
                "stop_reason": "runtime_failed",
                "reason": "xhs_login_browser_pages_closed",
                "retryable": False,
            },
            "records": [
                {
                    "failure_classification": {
                        "status": "login_required",
                        "failure_type": "login_required",
                        "reason": "login_or_profile_refresh_required",
                    }
                }
            ],
        }
    )

    result = failure_classifier.classify_attempt(
        exit_code=1,
        summary=summary,
        stderr="SMS Verification Parameter error 安全限制 300011 扫码登录",
        meta={"platform": "xhs"},
    )

    assert result == {
        "status": "failed_final",
        "failure_type": "browser_target_closed",
        "retryable": False,
        "wait_seconds": 0,
        "reason": "xhs_login_browser_pages_closed",
    }


@pytest.mark.parametrize(
    ("numeric_code", "failure_type", "reason"),
    [
        ("300011", "platform_security_limit", "platform_security_limit_300011"),
        ("300012", "ip_blocked", "ip_blocked_300012"),
    ],
)
def test_explicit_numeric_security_code_beats_mixed_sms_text(
    numeric_code: str,
    failure_type: str,
    reason: str,
) -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr=(
            "SMS Verification Parameter error; "
            f"账号异常，错误码 {numeric_code}"
        ),
        meta={"platform": "xhs"},
    )

    assert result["failure_type"] == failure_type
    assert result["reason"] == reason


def test_legacy_terminal_suffix_does_not_consume_next_log_line() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr=(
            "xhs_login_verification_terminal:Unknown marker\n"
            "Parameter error while parsing an unrelated payload"
        ),
        meta={"platform": "xhs"},
    )

    assert result["failure_type"] == "sms_verification_terminal"
    assert result["reason"] == "xhs_sms_verification_terminal"


def test_xhs_manual_checkpoint_budget_is_not_a_retryable_tool_error() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr="PlatformRuntimeError: xhs_manual_checkpoint_budget_exhausted",
        meta={"platform": "xhs"},
    )

    assert result["status"] == "blocked"
    assert result["failure_type"] == "manual_checkpoint_timeout"
    assert result["reason"] == "xhs_manual_checkpoint_budget_exhausted"
    assert result["retryable"] is False


def test_xhs_300012_is_distinct_from_300011() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr="RuntimeError: ip_blocked_300012",
        meta={"platform": "xhs"},
    )

    assert result["failure_type"] == "ip_blocked"
    assert result["reason"] == "ip_blocked_300012"


@pytest.mark.parametrize(
    "message",
    [
        "SMS Verification Enter verification code",
        "Parameter error while parsing an unrelated query",
        "今天可以获取验证码",
        "频繁旅行不代表请求验证码受限",
    ],
)
def test_similar_sms_text_does_not_claim_terminal_limit(message: str) -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr=message,
        meta={"platform": "xhs"},
    )

    assert result["failure_type"] != "sms_verification_terminal"


def test_nested_sms_terminal_beats_all_retryable_and_success_signals() -> None:
    stdout = json.dumps(
        {
            "blocked_by_policy": True,
            "wait_seconds": 7200,
            "records": [
                {
                    "nested_exception": {
                        "message": "SMS Verification: Parameter error"
                    }
                }
            ],
        }
    )

    result = failure_classifier.classify_attempt(
        exit_code=0,
        stdout=stdout,
        stderr="BrowserType.launch Timeout",
        meta={"platform": "xhs", "blocked_by_policy": True},
    )

    assert result["failure_type"] == "sms_verification_terminal"
    assert result["retryable"] is False


def test_prior_sms_terminal_record_does_not_override_latest_success() -> None:
    stdout = json.dumps(
        {
            "records": [
                {"error": "SMS Verification: Parameter error"},
                {
                    "failure_classification": {
                        "status": "completed",
                        "failure_type": "success",
                    }
                },
            ]
        }
    )

    result = failure_classifier.classify_attempt(
        exit_code=0,
        stdout=stdout,
        meta={"platform": "xhs"},
    )

    assert result["failure_type"] == "success"


def test_xhs_300011_beats_policy_launch_timeout_and_zero_exit() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=0,
        stderr="BrowserType.launch Timeout: XHS error code 300011",
        meta={"platform": "xhs", "blocked_by_policy": True},
    )

    assert result == {
        "status": "blocked",
        "failure_type": "platform_security_limit",
        "retryable": False,
        "wait_seconds": 0,
        "reason": "platform_security_limit_300011",
    }


def test_unrelated_300011_value_does_not_override_latest_success() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=0,
        stdout=json.dumps({"records": [{"post_id": 300011}]}),
        meta={"platform": "xhs"},
    )

    assert result["failure_type"] == "success"


@pytest.mark.parametrize(
    ("platform", "status", "retryable", "wait_seconds"),
    [
        ("xhs", "blocked", False, 0),
        ("weibo", "retry_wait", True, 3600),
    ],
)
def test_only_xhs_frequency_is_a_terminal_challenge(
    platform: str,
    status: str,
    retryable: bool,
    wait_seconds: int,
) -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr="请求过于频繁",
        meta={"platform": platform},
    )

    assert result["failure_type"] == "rate_limited"
    assert result["status"] == status
    assert result["retryable"] is retryable
    assert result["wait_seconds"] == wait_seconds


def test_xhs_structured_frequency_beats_policy_timeout_and_zero_exit() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=0,
        stderr="Navigation timeout",
        meta={
            "platform": "xhs",
            "blocked_by_policy": True,
            "structured_markers": {"rate_limited": True},
        },
    )

    assert result == {
        "status": "blocked",
        "failure_type": "rate_limited",
        "retryable": False,
        "wait_seconds": 0,
        "reason": "xhs_rate_limited_terminal",
    }


def test_strong_security_challenge_is_captcha() -> None:
    result = failure_classifier.classify_attempt(exit_code=1, stderr="请完成安全验证并拖动滑块")

    assert result["status"] == "captcha_detected"


def test_false_captcha_marker_key_does_not_self_match() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        meta={"structured_markers": {"captcha_or_verify": False}},
    )

    assert result["status"] == "retry_wait"
    assert result["failure_type"] == "tool_error"


def test_false_platform_security_limit_marker_key_does_not_self_match() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        meta={"structured_markers": {"platform_security_limit": False}},
    )

    assert result["status"] == "retry_wait"
    assert result["failure_type"] == "tool_error"


def test_unspecified_platform_security_limit_is_not_claimed_as_300011() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr="RuntimeError: xhs_creator_profile_visible_block:platform_security_limit",
    )

    assert result == {
        "status": "blocked",
        "failure_type": "platform_security_limit",
        "retryable": False,
        "wait_seconds": 0,
        "reason": "xhs_platform_security_limit_unspecified",
    }


def test_structured_unspecified_security_limit_beats_zero_exit_code() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=0,
        meta={"structured_markers": {"platform_security_limit": True}},
    )

    assert result == {
        "status": "blocked",
        "failure_type": "platform_security_limit",
        "retryable": False,
        "wait_seconds": 0,
        "reason": "xhs_platform_security_limit_unspecified",
    }


def test_generic_retry_later_text_is_not_xhs_platform_security_limit() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr="upstream account exception, please retry later",
    )

    assert result["status"] == "retry_wait"
    assert result["failure_type"] == "tool_error"


def test_false_captcha_marker_in_stdout_json_does_not_self_match() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stdout=json.dumps(
            {
                "records": [
                    {
                        "behavior_evidence": {
                            "visible_markers": {"captcha_or_verify": False}
                        }
                    }
                ]
            }
        ),
    )

    assert result["status"] == "retry_wait"
    assert result["failure_type"] == "tool_error"


def test_scalar_record_count_does_not_crash_child_classification() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stdout=json.dumps({"keyword": "青岛旅游", "records": 136}),
        stderr="BilibiliArticleDetailError: no parseable body",
    )

    assert result["status"] == "retry_wait"
    assert result["failure_type"] == "tool_error"
    assert result["retryable"] is True


def test_true_captcha_marker_in_stdout_json_is_detected() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stdout=json.dumps(
            {
                "records": [
                    {
                        "behavior_evidence": {
                            "visible_markers": {"captcha_or_verify": True}
                        }
                    }
                ]
            }
        ),
    )

    assert result["status"] == "captcha_detected"


def test_strong_platform_classification_beats_formal_count_failure() -> None:
    summary = (
        {
            "records": [
                {
                    "failure_classification": {
                        "status": "login_required",
                        "failure_type": "login_required",
                        "retryable": False,
                        "wait_seconds": 0,
                        "reason": "login_or_profile_refresh_required",
                    }
                }
            ],
            "import_completion_met": False,
            "formal_validation": {"stop_reason": "behavior_evidence_failed"},
        }
    )

    result = failure_classifier.classify_attempt(exit_code=2, summary=summary)

    assert result["status"] == "login_required"
    assert result["failure_type"] == "login_required"


def test_latest_policy_block_preserves_child_cooldown() -> None:
    summary = (
        {
            "records": [
                {
                    "failure_classification": {
                        "status": "completed",
                        "failure_type": "success",
                        "retryable": False,
                        "wait_seconds": 0,
                        "reason": "completed",
                    }
                },
                {
                    "failure_classification": {
                        "status": "retry_wait",
                        "failure_type": "policy_blocked",
                        "retryable": True,
                        "wait_seconds": 7200,
                        "reason": "max_requests_per_session",
                    }
                },
            ],
            "import_completion_met": False,
            "formal_validation": {
                "stop_reason": "behavior_evidence_failed",
            },
        }
    )

    result = failure_classifier.classify_attempt(exit_code=2, summary=summary)

    assert result["status"] == "retry_wait"
    assert result["failure_type"] == "policy_blocked"
    assert result["wait_seconds"] == 7200
    assert result["reason"] == "max_requests_per_session"


def test_target_closed_after_page_launch_is_not_browser_launch_failure() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr="TargetClosedError: Page.wait_for_timeout: Target page, context or browser has been closed",
    )

    assert result["status"] == "failed_final"
    assert result["failure_type"] == "browser_target_closed"
    assert result["retryable"] is False


@pytest.mark.parametrize(
    "code",
    [
        "xhs_browser_process_exited",
        "xhs_cdp_disconnected_unexpected",
        "xhs_browser_context_closed_unexpected",
        "xhs_main_page_closed_unexpected",
    ],
)
def test_xhs_stable_cdp_lifecycle_code_is_terminal(code: str) -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr=f"RuntimeError: {code}:search_after_network_pause\n扫码登录",
        meta={"platform": "xhs"},
    )

    assert result == {
        "status": "failed_final",
        "failure_type": "browser_target_closed",
        "retryable": False,
        "wait_seconds": 0,
        "reason": code,
    }


@pytest.mark.parametrize(
    "message",
    [
        "not_xhs_browser_process_exited",
        "xhs_browser_process_exited_extra",
        "xhs_cdp_disconnected_unexpectedly",
        "prefix_xhs_browser_context_closed_unexpected",
        "xhs_main_page_closed_unexpectedly",
        "browser disconnected while the network was unavailable",
    ],
)
def test_similar_text_is_not_a_stable_xhs_cdp_lifecycle_code(message: str) -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr=message,
        meta={"platform": "xhs"},
    )

    assert result["failure_type"] != "browser_target_closed"


def test_rate_limit_beats_target_closed_after_operator_closes_page() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr="请求过于频繁\nTargetClosedError: Target page, context or browser has been closed",
    )

    assert result["failure_type"] == "rate_limited"
    assert result["wait_seconds"] == 3600


# ---------- runner 分类读取子摘要（#112 S1） ----------
# 结构化 child 的 stdout 只打印终态信封（reporting.terminal_summary_envelope）；记录级分类、运行级阻断与
# 正式门禁只在 summary.json。下列摘要用 collection 写摘要时的同一组函数构造。

WORKER_SIGINT_LINE = "\n[Main] Received interrupt signal 2, exiting (cleanup max 15.0s)...\n"
SUCCESS = {
    "status": "completed", "failure_type": "success", "retryable": False,
    "wait_seconds": 0, "reason": "completed",
}


def child_outputs(
    batch_dir: Path,
    *,
    platform: str,
    record_classification: dict,
    stop_reason: str,
    stop_event: dict | None = None,
    worker_stdout: str = "",
) -> tuple[str, dict]:
    """返回 (child stdout 终态信封, summary.json 内容)，形状与 collection._run_main 写出的一致。"""

    from trippostcollect.application.reporting import terminal_summary_envelope

    record = {
        "platform": platform,
        "status": "completed" if record_classification["status"] == "completed" else "failed",
        "run": {"returncode": 0, "stdout_tail": worker_stdout, "stderr_tail": ""},
        "failure_classification": record_classification,
    }
    pagination_evidence = {
        "available": True, "stopped": stop_event is not None, "stop_event": stop_event or {},
    }
    blocker = (
        failure_classifier.runtime_blocker_from_pagination_evidence(pagination_evidence, [platform])
        or failure_classifier.latest_runtime_blocker([record], [platform])
    )
    validation = failure_classifier.apply_runtime_blocker(
        {"stop_reason": stop_reason, "stop_detail": "", "source_exhausted_met": False}, blocker,
    )
    summary = {
        "records": [record],
        "pagination_evidence": pagination_evidence,
        "formal_validation": validation,
        "import_completion_met": False,
        "failure_reason": (
            "import_completion_not_met: completion_mode=source-exhausted valid_new=3 "
            f"stop_reason={validation['stop_reason']} behavior_ok=True policy_ok=True"
        ),
    }
    if blocker:
        summary["runtime_blocker"] = blocker
    batch_dir.mkdir(parents=True, exist_ok=True)
    summary_path = batch_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    envelope = terminal_summary_envelope(
        summary_path=summary_path,
        report_path=batch_dir / "summary.md",
        batch_dir=batch_dir,
        summary=summary,
    )
    return json.dumps(envelope, ensure_ascii=False) + "\n", summary


def test_envelope_alone_never_claims_source_exhaustion_not_persisted(tmp_path: Path) -> None:
    """信封里只有 import_completion_met=false，没有 formal_validation：不得据此进入未耗尽分支。"""

    stdout, _summary = child_outputs(
        tmp_path, platform="douyin", record_classification=SUCCESS, stop_reason="runtime_failed",
    )

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout)

    assert result["failure_type"] != "source_exhaustion_not_persisted"


@pytest.mark.parametrize(
    ("platform", "record_kwargs", "expected"),
    [
        (
            "weibo",
            {"exit_code": 1, "stderr": "Login state result: False"},
            {"status": "login_required", "failure_type": "login_required", "retryable": False},
        ),
        (
            "zhihu",
            {"exit_code": 1, "meta": {"structured_markers": {"captcha_or_verify": True}}},
            {"status": "captcha_detected", "failure_type": "captcha_detected", "retryable": False},
        ),
    ],
    ids=["login_required", "captcha_detected"],
)
def test_record_level_runtime_blocker_reaches_runner_from_summary(
    tmp_path: Path, platform: str, record_kwargs: dict, expected: dict,
) -> None:
    kwargs = dict(record_kwargs)
    kwargs["meta"] = {"platform": platform, **kwargs.get("meta", {})}
    record_classification = failure_classifier.classify_attempt(**kwargs)
    stdout, summary = child_outputs(
        tmp_path, platform=platform, record_classification=record_classification,
        stop_reason="behavior_evidence_failed",
    )

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout, summary=summary)

    assert {key: result[key] for key in expected} == expected
    assert summary["formal_validation"]["stop_reason"] == expected["failure_type"]


def test_policy_block_from_summary_keeps_child_cooldown(tmp_path: Path) -> None:
    record_classification = {
        "status": "retry_wait", "failure_type": "policy_blocked", "retryable": True,
        "wait_seconds": 7200, "reason": "max_requests_per_session",
    }
    stdout, summary = child_outputs(
        tmp_path, platform="bilibili", record_classification=record_classification,
        stop_reason="crawl_policy_evidence_failed",
    )

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout, summary=summary)

    assert result == record_classification


def test_pagination_runtime_stop_is_runtime_failed_with_its_detail(tmp_path: Path) -> None:
    stop_event = {"platform": "weibo", "stop_reason": "runtime_failed", "stop_detail": "search_request_failed"}
    stdout, summary = child_outputs(
        tmp_path, platform="weibo", record_classification=SUCCESS, stop_reason="runtime_failed",
        stop_event=stop_event,
    )

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout, summary=summary)

    assert result == {
        "status": "retry_wait", "failure_type": "runtime_failed", "retryable": True,
        "wait_seconds": 600, "reason": "search_request_failed",
    }


def test_pagination_login_stop_is_login_required(tmp_path: Path) -> None:
    stop_event = {"platform": "zhihu", "stop_reason": "login_required", "stop_detail": "login_required"}
    stdout, summary = child_outputs(
        tmp_path, platform="zhihu", record_classification=SUCCESS, stop_reason="login_required",
        stop_event=stop_event,
    )

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout, summary=summary)

    assert (result["status"], result["failure_type"], result["retryable"]) == (
        "login_required", "login_required", False,
    )


def test_worker_sigint_seen_in_summary_is_operator_interrupt(tmp_path: Path) -> None:
    """2026-10-08 抖音形状：worker 收到 Ctrl+C 后以 0 退出，中间层写出未耗尽摘要并以 2 退出。"""

    stdout, summary = child_outputs(
        tmp_path, platform="douyin", record_classification=SUCCESS, stop_reason="runtime_failed",
        worker_stdout=WORKER_SIGINT_LINE,
    )

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout, summary=summary)

    assert result["failure_type"] == "runtime_failed"
    assert result["reason"] == "operator_interrupt"
    assert result["interrupt"]["signal"] == "SIGINT"
    assert result["interrupt"]["error"] == "runtime_failed:operator_interrupt:SIGINT"


def test_worker_sigterm_from_watchdog_is_not_operator_interrupt(tmp_path: Path) -> None:
    """父链路与无进展看门狗只发 SIGTERM；worker 打印的 SIGTERM 行不能当作操作人中断。"""

    stdout, summary = child_outputs(
        tmp_path, platform="douyin", record_classification=SUCCESS,
        stop_reason="image_materialization_incomplete",
        worker_stdout="[Main] Received interrupt signal 15, exiting (cleanup max 15.0s)...\n",
    )

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout, summary=summary)

    assert "interrupt" not in result
    assert result["failure_type"] == "source_exhaustion_not_persisted"


def test_middle_layer_operator_sigterm_line_is_interrupt() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=143,
        stderr="Traceback ...\nruntime_failed:operator_interrupt:SIGTERM\n",
    )

    assert result["reason"] == "operator_interrupt"
    assert result["interrupt"]["signal"] == "SIGTERM"
    assert result["retryable"] is True


def test_genuinely_unfinished_source_keeps_formal_stop_reason(tmp_path: Path) -> None:
    stdout, summary = child_outputs(
        tmp_path, platform="weibo", record_classification=SUCCESS,
        stop_reason="image_materialization_incomplete",
    )

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout, summary=summary)

    assert result == {
        "status": "retry_wait", "failure_type": "source_exhaustion_not_persisted", "retryable": True,
        "wait_seconds": 600,
        "reason": "formal_source_exhaustion_not_persisted:image_materialization_incomplete",
    }


def test_summary_body_text_is_not_pattern_matched(tmp_path: Path) -> None:
    """摘要只按结构化字段读取：样本正文里的“请登录”不能把未耗尽改判为登录失效。"""

    stdout, summary = child_outputs(
        tmp_path, platform="weibo", record_classification=SUCCESS,
        stop_reason="image_materialization_incomplete",
    )
    summary["formal_validation"]["valid_new_samples"] = [{"title": "青岛 请登录后查看 验证码 429"}]

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout, summary=summary)

    assert result["failure_type"] == "source_exhaustion_not_persisted"


def test_runner_classifies_from_child_summary_not_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import sqlite3
    import subprocess
    from types import SimpleNamespace

    from trippostcollect.db.bootstrap import bootstrap_connection
    from trippostcollect.db.connection import connect_db

    crawl_runner = import_module("crawl_runner")
    source = json.loads((ROOT / "config" / "crawl_targets.json").read_text(encoding="utf-8"))
    job_config = next(job for job in source["jobs"] if job["site_key"] == "weibo")
    db_path = tmp_path / "runner.sqlite"
    with connect_db(db_path) as conn:
        bootstrap_connection(conn, config={**source, "jobs": [job_config]}, sync_jobs=True)
        row = dict(conn.execute("SELECT * FROM crawl_jobs WHERE job_key=?", (job_config["job_key"],)).fetchone())
    contract = tmp_path / "contract.md"
    contract.write_text("contract", encoding="utf-8")
    state_path = tmp_path / "states" / f"{row['job_key']}.json"
    crawl_runner.FrozenExecutionState.create(
        state_path, run_id="run-classify", job_key=row["job_key"], site_key="weibo",
        job_kind="mediacrawler_search", plan={"scheduling": {"lane_key": "weibo"}}, frozen_inputs=[contract],
    )
    job = crawl_runner.PreparedJob(
        selection_index=0, row=row, command=["child"], discovery_plan=None,
        state_path=state_path, lane_key="weibo",
    )
    record_classification = failure_classifier.classify_attempt(
        exit_code=1, stderr="Login state result: False", meta={"platform": "weibo"},
    )
    stdout, _summary = child_outputs(
        tmp_path / "child", platform="weibo", record_classification=record_classification,
        stop_reason="behavior_evidence_failed",
    )

    def finished_child(*_args: object, **_kwargs: object) -> object:
        return crawl_runner.ChildRun(
            completed=subprocess.CompletedProcess(["child"], 2, stdout, ""),
            raw_stdout=stdout, raw_stderr="", logs={}, interrupt_signum=None, launched=True,
            forced_termination=False, killed_child_process_groups=[], residual_process_groups=[],
        )

    monkeypatch.setattr(crawl_runner, "run_child_command", finished_child)
    record = crawl_runner.execute_prepared_job(
        job, args=SimpleNamespace(no_import=False), db_path=db_path,
        config={"defaults": {"schedule_jitter_ratio": 0}}, run_id="run-classify",
        run_dir=tmp_path / "run", interrupt_signal=lambda: None,
    )

    assert (record["status"], record["failure_type"]) == ("login_required", "login_required")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["steps"]["command_executed"]["error"] == "login_or_profile_refresh_required"
    with sqlite3.connect(db_path) as conn:
        status, consecutive_failures = conn.execute(
            "SELECT status, consecutive_failures FROM crawl_jobs WHERE job_key=?", (row["job_key"],),
        ).fetchone()
    assert (status, consecutive_failures) == ("login_required", 1)


@pytest.mark.parametrize(
    "reason",
    [
        "runtime_failed:rate_limited",
        "[WeiboClient.request] GET /api/container/getIndex HTTP 429",
        "bilibili search rate limit",
    ],
)
def test_platform_rate_limited_code_is_rate_limited_not_tool_error(reason: str) -> None:
    """#112：平台层结构化码 rate_limited（下划线）须与自然语言 rate limit 同为频控冷却。"""

    result = failure_classifier.classify_attempt(exit_code=1, stderr=reason, meta={"platform": "weibo"})

    assert result["failure_type"] == "rate_limited"
    assert result["wait_seconds"] == 3600
