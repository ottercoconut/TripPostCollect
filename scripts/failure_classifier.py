#!/usr/bin/env python3
"""Deterministic crawl attempt failure classification."""

from __future__ import annotations

import json
import re
from typing import Any


CAPTCHA_PATTERNS = re.compile(
    r"人机验证|安全验证|请完成验证|请通过验证|图形验证码|滑块验证码|拖动滑块|"
    r"captcha|turnstile|geetest|wappoc_appmsgcaptcha",
    re.I,
)
LOGIN_PATTERNS = re.compile(
    r"登录后|请登录|需要登录|手机号登录|扫码登录|未登录|login state (?:result: false|not confirmed)|"
    r"(?<![A-Za-z])sign[ _-]?in(?![A-Za-z])|login_required|passport",
    re.I,
)
RUNTIME_PERMISSION_PATTERNS = re.compile(
    r"Failed to initialize cache|Operation not permitted|Permission denied|browser_runtime_permission",
    re.I,
)
BROWSER_LAUNCH_PATTERNS = re.compile(
    r"BrowserType\.launch|launch_persistent_context|SIGABRT|signal 6|crashpad|kill EPERM",
    re.I,
)
TARGET_CLOSED_PATTERNS = re.compile(
    r"TargetClosedError|Target page, context or browser has been closed",
    re.I,
)
XHS_CDP_LIFECYCLE_CODE_PATTERNS = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"xhs_browser_process_exited|"
    r"xhs_cdp_disconnected_unexpected|"
    r"xhs_browser_context_closed_unexpected|"
    r"xhs_main_page_closed_unexpected"
    r")(?![A-Za-z0-9_])",
    re.I,
)
IMPORT_TARGET_PATTERNS = re.compile(r"import_new_target_not_met", re.I)
RATE_PATTERNS = re.compile(r"429|too many requests|rate limit|访问过于频繁|请求过于频繁|操作频繁", re.I)
XHS_FREQUENT_CHALLENGE_PATTERNS = re.compile(
    r"访问(?:过于)?频繁|请求(?:过于)?频繁|操作(?:过于)?频繁|"
    r"requests?\s+(?:are\s+)?too\s+frequent|too many requests|rate limit",
    re.I,
)
TIMEOUT_PATTERNS = re.compile(r"Timeout|timeout|ETIMEDOUT|Navigation timeout|net::ERR_TIMED_OUT", re.I)
NO_IMAGE_PATTERNS = re.compile(r"No image-bearing|no_content_images|skipped_no_image", re.I)
PARSE_PATTERNS = re.compile(r"JSONDecodeError|parse_failed|Selector|KeyError|ValueError", re.I)
BLOCK_PATTERNS = re.compile(r"forbidden|access denied|拒绝访问|blocked_detected|blocked_by_policy", re.I)
PLATFORM_SECURITY_LIMIT_PATTERNS = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:xhs_)?platform_security_limit(?:_300011)?\b",
    re.I,
)
XHS_SECURITY_300011_PATTERNS = re.compile(
    r"(?:error(?:[_\s-]?code)?|错误码|异常码)\s*[:=]?\s*[\"']?300011\b|"
    r"(?:安全限制|账号异常|account exception).{0,80}\b300011\b|"
    r"\b300011\b.{0,80}(?:安全限制|账号异常|account exception)",
    re.I,
)
XHS_SECURITY_300012_PATTERNS = re.compile(
    r"(?:error(?:[_\s-]?code)?|错误码|异常码)\s*[:=]?\s*[\"']?300012\b|"
    r"(?:安全限制|账号异常|account exception).{0,80}\b300012\b|"
    r"\b300012\b.{0,80}(?:安全限制|账号异常|account exception)",
    re.I,
)
XHS_LEGACY_LOGIN_TERMINAL_PATTERNS = re.compile(
    r"(?<![A-Za-z0-9_])xhs_login_verification_terminal:([^\r\n]+)",
    re.I,
)
XHS_MANUAL_CHECKPOINT_TIMEOUT_PATTERNS = re.compile(
    r"(?<![A-Za-z0-9_])xhs_manual_checkpoint_budget_exhausted(?![A-Za-z0-9_])",
    re.I,
)
XHS_RATE_LIMIT_TERMINAL_PATTERNS = re.compile(
    r"(?<![A-Za-z0-9_])xhs_rate_limited_terminal(?![A-Za-z0-9_])",
    re.I,
)
XHS_BLOCKED_TERMINAL_PATTERNS = re.compile(
    r"(?<![A-Za-z0-9_])xhs_blocked_terminal(?![A-Za-z0-9_])",
    re.I,
)
XHS_ACCOUNT_EXCEPTION_PATTERNS = re.compile(
    r"(?<![A-Za-z0-9_])xhs_account_exception(?![A-Za-z0-9_])|"
    r"账号异常|account exception",
    re.I,
)
XHS_LOGIN_ERROR_PAGE_PATTERNS = re.compile(
    r"(?<![A-Za-z0-9_])xhs_login_error_page(?![A-Za-z0-9_])|"
    r"/website-login/error",
    re.I,
)
XHS_UNSPECIFIED_SECURITY_LIMIT_PATTERNS = re.compile(
    r"(?<![A-Za-z0-9_])xhs_platform_security_limit_unspecified(?![A-Za-z0-9_])",
    re.I,
)
_XHS_SMS_CONTEXT = r"\bsms\b|短信|验证码|verification\s+code"
_XHS_SMS_VERIFICATION = r"sms\s+verification|短信验证|短信验证码"
_XHS_SMS_PARAMETER_ERROR = r"parameter\s+error|参数错误"
_XHS_DAILY = r"今日|今天|当日|today(?:'s)?|daily"
_XHS_LIMIT = r"上限|限制|已用完|用完|耗尽|limit|quota|maximum|used\s+up|exhausted"
XHS_SMS_PARAMETER_TERMINAL_PATTERNS = re.compile(
    rf"(?:{_XHS_SMS_VERIFICATION}).{{0,64}}(?:{_XHS_SMS_PARAMETER_ERROR})|"
    rf"(?:{_XHS_SMS_PARAMETER_ERROR}).{{0,64}}(?:{_XHS_SMS_VERIFICATION})",
    re.I,
)
XHS_SMS_DAILY_LIMIT_PATTERNS = re.compile(
    rf"(?:{_XHS_DAILY}).{{0,48}}(?:{_XHS_SMS_CONTEXT}).{{0,48}}(?:{_XHS_LIMIT})|"
    rf"(?:{_XHS_SMS_CONTEXT}).{{0,48}}(?:{_XHS_DAILY}).{{0,48}}(?:{_XHS_LIMIT})|"
    rf"(?:{_XHS_SMS_CONTEXT}).{{0,48}}(?:{_XHS_LIMIT}).{{0,48}}(?:{_XHS_DAILY})|"
    rf"(?:{_XHS_LIMIT}).{{0,48}}(?:{_XHS_DAILY}).{{0,48}}(?:{_XHS_SMS_CONTEXT})",
    re.I,
)
XHS_SMS_FREQUENCY_PATTERNS = re.compile(
    rf"(?:{_XHS_SMS_CONTEXT}).{{0,48}}"
    r"(?:过于频繁|太频繁|操作(?:过于)?频繁|请求(?:过于)?频繁|too\s+frequent|"
    r"too\s+many\s+(?:requests|attempts)|rate\s+limit)|"
    r"(?:过于频繁|太频繁|操作(?:过于)?频繁|请求(?:过于)?频繁|too\s+frequent|"
    rf"too\s+many\s+(?:requests|attempts)|rate\s+limit).{{0,48}}"
    rf"(?:{_XHS_SMS_CONTEXT})",
    re.I,
)


def _xhs_stable_sms_terminal_reason(text: str) -> str:
    """Read stable codes, including one-line output from the retired emitter."""

    raw = str(text or "")
    legacy = XHS_LEGACY_LOGIN_TERMINAL_PATTERNS.search(raw)
    legacy_marker = legacy.group(1).strip() if legacy else ""
    if re.search(
        r"(?<![A-Za-z0-9_])xhs_sms_verification_parameter_error(?![A-Za-z0-9_])",
        raw,
        re.I,
    ) or (legacy and re.search(_XHS_SMS_PARAMETER_ERROR, legacy_marker, re.I)):
        return "xhs_sms_verification_parameter_error"
    if re.search(
        r"(?<![A-Za-z0-9_])xhs_sms_verification_daily_limit(?![A-Za-z0-9_])",
        raw,
        re.I,
    ) or (legacy and XHS_SMS_DAILY_LIMIT_PATTERNS.search(legacy_marker)):
        return "xhs_sms_verification_daily_limit"
    if re.search(
        r"(?<![A-Za-z0-9_])xhs_sms_verification_rate_limited(?![A-Za-z0-9_])",
        raw,
        re.I,
    ) or (legacy and XHS_SMS_FREQUENCY_PATTERNS.search(legacy_marker)):
        return "xhs_sms_verification_rate_limited"
    if legacy or re.search(
        r"(?<![A-Za-z0-9_])xhs_sms_verification_terminal(?![A-Za-z0-9_])",
        raw,
        re.I,
    ):
        return "xhs_sms_verification_terminal"
    return ""


def _xhs_sms_terminal_reason(text: str) -> str:
    """Return the precise stable subtype for a terminal XHS SMS checkpoint."""

    stable_reason = _xhs_stable_sms_terminal_reason(text)
    if stable_reason:
        return stable_reason
    normalized = " ".join(str(text or "").split())
    if XHS_SMS_PARAMETER_TERMINAL_PATTERNS.search(normalized):
        return "xhs_sms_verification_parameter_error"
    if XHS_SMS_DAILY_LIMIT_PATTERNS.search(normalized):
        return "xhs_sms_verification_daily_limit"
    if XHS_SMS_FREQUENCY_PATTERNS.search(normalized):
        return "xhs_sms_verification_rate_limited"
    return ""


def is_xhs_sms_terminal_text(text: str) -> bool:
    """Return whether visible/error text proves a terminal XHS SMS challenge."""

    return bool(_xhs_sms_terminal_reason(text))


def _platform_security_limit_reason(
    *,
    text: str,
    markers: dict[str, Any],
    platform: str,
) -> str:
    if re.search(
        r"(?<![A-Za-z0-9_])(?:xhs_)?platform_security_limit_300011(?![A-Za-z0-9_])",
        text,
        re.I,
    ) or (platform == "xhs" and XHS_SECURITY_300011_PATTERNS.search(text)):
        return "platform_security_limit_300011"
    if XHS_UNSPECIFIED_SECURITY_LIMIT_PATTERNS.search(text):
        return "xhs_platform_security_limit_unspecified"
    if platform == "xhs" and XHS_ACCOUNT_EXCEPTION_PATTERNS.search(text):
        return "xhs_account_exception"
    if platform == "xhs" and XHS_LOGIN_ERROR_PAGE_PATTERNS.search(text):
        return "xhs_login_error_page"
    if bool(markers.get("platform_security_limit")):
        return "xhs_platform_security_limit_unspecified"
    if PLATFORM_SECURITY_LIMIT_PATTERNS.search(text):
        return (
            "xhs_platform_security_limit_unspecified"
            if platform == "xhs" or "xhs_" in text.lower()
            else "platform_security_limit"
        )
    return ""


def extract_stdout_json(stdout: str) -> dict[str, Any]:
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        value = json.loads(stdout[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _meta_markers(meta: dict[str, Any]) -> dict[str, Any]:
    markers = dict(meta.get("structured_markers") or {})
    preflight = meta.get("scrapling_preflight") or {}
    markers.update(preflight.get("structured_markers") or {})
    return markers


def _text_blob(*parts: Any) -> str:
    return "\n".join(str(part or "") for part in parts)


def _without_false_security_markers(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_false_security_markers(item)
            for key, item in value.items()
            if not (
                key in {"captcha", "captcha_or_verify", "platform_security_limit"}
                and item is False
            )
        }
    if isinstance(value, list):
        return [_without_false_security_markers(item) for item in value]
    return value


def _stdout_without_json_payload(stdout: str, stdout_json: dict[str, Any]) -> str:
    if not stdout_json:
        return stdout
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end < start:
        return stdout
    return f"{stdout[:start]}\n{stdout[end + 1:]}"


def _terminal_stdout_payload(stdout_json: dict[str, Any]) -> dict[str, Any]:
    records = stdout_json.get("records")
    if not isinstance(records, list):
        return stdout_json
    latest_record = next(
        (record for record in reversed(records) if isinstance(record, dict)),
        {},
    )
    return {
        "record": latest_record,
        "failure_reason": stdout_json.get("failure_reason"),
        "pagination_evidence": stdout_json.get("pagination_evidence"),
        "runtime_blocker": stdout_json.get("runtime_blocker"),
    }


def _strong_child_classification(stdout_json: dict[str, Any]) -> dict[str, Any] | None:
    strong_statuses = {"captcha_detected", "login_required", "blocked", "failed_final"}
    records = stdout_json.get("records")
    if not isinstance(records, list):
        return None
    latest_record = next(
        (record for record in reversed(records) if isinstance(record, dict)),
        {},
    )
    classification = latest_record.get("failure_classification") or {}
    if not isinstance(classification, dict):
        return None
    if (
        classification.get("failure_type") == "policy_blocked"
        or classification.get("status") in strong_statuses
    ):
        return dict(classification)
    return None


def _structured_runtime_blocker(
    stdout_json: dict[str, Any],
) -> dict[str, Any] | None:
    """Normalize the authoritative terminal event projected into child summary."""

    blocker = stdout_json.get("runtime_blocker") or {}
    if not isinstance(blocker, dict) or blocker.get("source") != "xhs_runtime_terminal":
        return None
    failure_type = str(blocker.get("failure_type") or "")
    reason = str(blocker.get("reason") or "")
    if not failure_type or not reason:
        return None
    return {
        "status": str(blocker.get("status") or "blocked"),
        "failure_type": failure_type,
        "retryable": bool(blocker.get("retryable", False)),
        "wait_seconds": int(blocker.get("wait_seconds") or 0),
        "reason": reason,
    }


def classify_attempt(
    *,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = meta or {}
    platform = str(meta.get("platform") or "").strip().lower()
    stdout_json = extract_stdout_json(stdout)
    markers = _meta_markers(meta)
    text_meta = dict(meta)
    text_meta.pop("structured_markers", None)
    preflight = text_meta.get("scrapling_preflight")
    if isinstance(preflight, dict):
        text_meta["scrapling_preflight"] = {
            key: value for key, value in preflight.items() if key != "structured_markers"
        }
    stdout_without_payload = _stdout_without_json_payload(stdout, stdout_json)
    text = _text_blob(
        stdout_without_payload,
        stderr,
        json.dumps(text_meta, ensure_ascii=False),
        json.dumps(_without_false_security_markers(stdout_json), ensure_ascii=False),
    )
    terminal_text = _text_blob(
        stdout_without_payload,
        stderr,
        json.dumps(text_meta, ensure_ascii=False),
        json.dumps(
            _without_false_security_markers(_terminal_stdout_payload(stdout_json)),
            ensure_ascii=False,
        ),
    )

    if meta.get("skipped") and str(meta.get("skip_reason") or "").startswith("video_"):
        return {
            "status": "completed",
            "failure_type": "skipped_video",
            "retryable": False,
            "wait_seconds": 0,
            "reason": meta.get("skip_reason") or "video_target_ignored",
        }

    structured_runtime_blocker = _structured_runtime_blocker(stdout_json)
    if structured_runtime_blocker:
        return structured_runtime_blocker

    if re.search(
        r"(?<![A-Za-z0-9_])(?:xhs_platform_security_limit_300012|ip_blocked_300012)(?![A-Za-z0-9_])",
        terminal_text,
        re.I,
    ) or (platform == "xhs" and XHS_SECURITY_300012_PATTERNS.search(terminal_text)):
        return {
            "status": "blocked",
            "failure_type": "ip_blocked",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "ip_blocked_300012",
        }

    if re.search(
        r"(?<![A-Za-z0-9_])(?:xhs_)?platform_security_limit_300011(?![A-Za-z0-9_])",
        terminal_text,
        re.I,
    ) or (platform == "xhs" and XHS_SECURITY_300011_PATTERNS.search(terminal_text)):
        return {
            "status": "blocked",
            "failure_type": "platform_security_limit",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "platform_security_limit_300011",
        }

    stable_sms_terminal_reason = (
        _xhs_stable_sms_terminal_reason(terminal_text)
        if platform == "xhs" or "xhs_" in terminal_text.lower()
        else ""
    )
    if stable_sms_terminal_reason:
        return {
            "status": "blocked",
            "failure_type": "sms_verification_terminal",
            "retryable": False,
            "wait_seconds": 0,
            "reason": stable_sms_terminal_reason,
        }

    if XHS_MANUAL_CHECKPOINT_TIMEOUT_PATTERNS.search(terminal_text):
        return {
            "status": "blocked",
            "failure_type": "manual_checkpoint_timeout",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "xhs_manual_checkpoint_budget_exhausted",
        }

    stable_security_reason = ""
    for candidate in (
        "xhs_platform_security_limit_unspecified",
        "xhs_account_exception",
        "xhs_login_error_page",
    ):
        if re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(candidate)}(?![A-Za-z0-9_])",
            terminal_text,
            re.I,
        ):
            stable_security_reason = candidate
            break
    if stable_security_reason:
        return {
            "status": "blocked",
            "failure_type": "platform_security_limit",
            "retryable": False,
            "wait_seconds": 0,
            "reason": stable_security_reason,
        }

    if XHS_RATE_LIMIT_TERMINAL_PATTERNS.search(terminal_text):
        return {
            "status": "blocked",
            "failure_type": "rate_limited",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "xhs_rate_limited_terminal",
        }

    if XHS_BLOCKED_TERMINAL_PATTERNS.search(terminal_text):
        return {
            "status": "blocked",
            "failure_type": "blocked_or_forbidden",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "xhs_blocked_terminal",
        }

    xhs_cdp_lifecycle = XHS_CDP_LIFECYCLE_CODE_PATTERNS.search(terminal_text)
    if xhs_cdp_lifecycle:
        return {
            "status": "failed_final",
            "failure_type": "browser_target_closed",
            "retryable": False,
            "wait_seconds": 0,
            "reason": xhs_cdp_lifecycle.group(0).lower(),
        }

    child_classification = _strong_child_classification(stdout_json)
    if child_classification:
        return child_classification

    sms_terminal_reason = (
        _xhs_sms_terminal_reason(terminal_text)
        if platform == "xhs" or "xhs_" in terminal_text.lower()
        else ""
    )
    if bool(markers.get("sms_verification_terminal")) or sms_terminal_reason:
        return {
            "status": "blocked",
            "failure_type": "sms_verification_terminal",
            "retryable": False,
            "wait_seconds": 0,
            "reason": sms_terminal_reason or "xhs_sms_verification_terminal",
        }

    platform_security_reason = _platform_security_limit_reason(
        text=terminal_text,
        markers=markers,
        platform=platform,
    )
    if platform_security_reason:
        return {
            "status": "blocked",
            "failure_type": "platform_security_limit",
            "retryable": False,
            "wait_seconds": 0,
            "reason": platform_security_reason,
        }

    if platform == "xhs" and (
        bool(markers.get("rate_limited"))
        or XHS_FREQUENT_CHALLENGE_PATTERNS.search(terminal_text)
    ):
        return {
            "status": "blocked",
            "failure_type": "rate_limited",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "xhs_rate_limited_terminal",
        }

    if stdout_json.get("blocked_by_policy") or meta.get("blocked_by_policy"):
        wait_seconds = int(stdout_json.get("wait_seconds") or 0)
        return {
            "status": "retry_wait",
            "failure_type": "policy_blocked",
            "retryable": True,
            "wait_seconds": max(60, wait_seconds),
            "reason": stdout_json.get("reason") or "policy_blocked",
        }

    if exit_code == 0 and not meta.get("blocked_detected"):
        return {
            "status": "completed",
            "failure_type": "success",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "completed",
        }

    formal_validation = stdout_json.get("formal_validation") or {}
    if stdout_json.get("import_completion_met") is False and isinstance(formal_validation, dict):
        stop_reason = str(formal_validation.get("stop_reason") or "source_not_exhausted")
        return {
            "status": "retry_wait",
            "failure_type": "source_exhaustion_not_persisted",
            "retryable": True,
            "wait_seconds": 600,
            "reason": f"formal_source_exhaustion_not_persisted:{stop_reason}",
        }

    runtime_permission = RUNTIME_PERMISSION_PATTERNS.search(text)
    if runtime_permission and (
        "uv" in text.lower()
        or "browser_runtime_permission" in text.lower()
        or BROWSER_LAUNCH_PATTERNS.search(text)
    ):
        return {
            "status": "failed_final",
            "failure_type": "runtime_permission_error",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "local_runtime_path_is_not_writable",
        }

    if BROWSER_LAUNCH_PATTERNS.search(text):
        return {
            "status": "retry_wait",
            "failure_type": "browser_launch_failed",
            "retryable": True,
            "wait_seconds": 60,
            "reason": "chromium_or_playwright_launch_failed",
        }

    if bool(markers.get("captcha_or_verify")) or CAPTCHA_PATTERNS.search(text):
        return {
            "status": "captcha_detected",
            "failure_type": "captcha_detected",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "captcha_or_security_challenge_detected",
        }

    if LOGIN_PATTERNS.search(text):
        return {
            "status": "login_required",
            "failure_type": "login_required",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "login_or_profile_refresh_required",
        }

    if RATE_PATTERNS.search(text):
        return {
            "status": "retry_wait",
            "failure_type": "rate_limited",
            "retryable": True,
            "wait_seconds": 3600,
            "reason": "rate_limited",
        }

    if TARGET_CLOSED_PATTERNS.search(text):
        return {
            "status": "failed_final",
            "failure_type": "browser_target_closed",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "page_context_or_browser_closed_after_launch",
        }

    if BLOCK_PATTERNS.search(text) or meta.get("blocked_detected"):
        return {
            "status": "blocked",
            "failure_type": "blocked_or_forbidden",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "blocked_or_forbidden",
        }

    if NO_IMAGE_PATTERNS.search(text):
        return {
            "status": "failed_final",
            "failure_type": "no_image",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "no_content_images",
        }

    if TIMEOUT_PATTERNS.search(text):
        return {
            "status": "retry_wait",
            "failure_type": "network_timeout",
            "retryable": True,
            "wait_seconds": 900,
            "reason": "timeout_or_navigation_timeout",
        }

    if PARSE_PATTERNS.search(text):
        return {
            "status": "failed_final",
            "failure_type": "parse_failed",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "parser_or_extractor_failed",
        }

    if IMPORT_TARGET_PATTERNS.search(text):
        return {
            "status": "retry_wait",
            "failure_type": "import_new_target_not_met",
            "retryable": True,
            "wait_seconds": 600,
            "reason": "formal_import_new_target_not_reached",
        }

    return {
        "status": "retry_wait",
        "failure_type": "tool_error",
        "retryable": True,
        "wait_seconds": 600,
        "reason": "nonzero_exit_without_specific_classification",
    }
