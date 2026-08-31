"""TripPostCollect tests for failure classifier."""

from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path


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


def test_platform_security_limit_runtime_error_is_blocked() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr="RuntimeError: xhs_creator_profile_visible_block:platform_security_limit",
    )

    assert result == {
        "status": "blocked",
        "failure_type": "platform_security_limit",
        "retryable": False,
        "wait_seconds": 0,
        "reason": "platform_security_limit_300011",
    }


def test_structured_platform_security_limit_beats_zero_exit_code() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=0,
        meta={"structured_markers": {"platform_security_limit": True}},
    )

    assert result == {
        "status": "blocked",
        "failure_type": "platform_security_limit",
        "retryable": False,
        "wait_seconds": 0,
        "reason": "platform_security_limit_300011",
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


def test_rate_limit_beats_target_closed_after_operator_closes_page() -> None:
    result = failure_classifier.classify_attempt(
        exit_code=1,
        stderr="请求过于频繁\nTargetClosedError: Target page, context or browser has been closed",
    )

    assert result["failure_type"] == "rate_limited"
    assert result["wait_seconds"] == 3600
