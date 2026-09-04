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

failure_classifier = import_module("failure_classifier")


def test_source_exhaustion_failure_beats_incidental_rate_text() -> None:
    stdout = json.dumps(
        {
            "import_completion_met": False,
            "failure_reason": "source_exhaustion_not_persisted",
            "formal_validation": {
                "stop_reason": "source_not_exhausted",
                "invalid_reason_counts": {"raw_text_containing_429": 1},
            },
        }
    )

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout)

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
    stdout = json.dumps(
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
        stdout=stdout,
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
    stdout = json.dumps(
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

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout)

    assert result["status"] == "login_required"
    assert result["failure_type"] == "login_required"


def test_latest_policy_block_preserves_child_cooldown() -> None:
    stdout = json.dumps(
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

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout)

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
