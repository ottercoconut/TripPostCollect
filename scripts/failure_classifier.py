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
IMPORT_TARGET_PATTERNS = re.compile(r"import_new_target_not_met", re.I)
RATE_PATTERNS = re.compile(r"429|too many requests|rate limit|访问过于频繁|请求过于频繁|操作频繁", re.I)
TIMEOUT_PATTERNS = re.compile(r"Timeout|timeout|ETIMEDOUT|Navigation timeout|net::ERR_TIMED_OUT", re.I)
NO_IMAGE_PATTERNS = re.compile(r"No image-bearing|no_content_images|skipped_no_image", re.I)
PARSE_PATTERNS = re.compile(r"JSONDecodeError|parse_failed|Selector|KeyError|ValueError", re.I)
BLOCK_PATTERNS = re.compile(r"forbidden|access denied|拒绝访问|blocked_detected|blocked_by_policy", re.I)
PLATFORM_SECURITY_LIMIT_PATTERNS = re.compile(
    r"\bplatform_security_limit(?:_300011)?\b",
    re.I,
)


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


def _strong_child_classification(stdout_json: dict[str, Any]) -> dict[str, Any] | None:
    strong_statuses = {"captcha_detected", "login_required", "blocked", "failed_final"}
    records = stdout_json.get("records")
    if not isinstance(records, list):
        return None
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        classification = record.get("failure_classification") or {}
        if not isinstance(classification, dict):
            continue
        if (
            classification.get("failure_type") == "policy_blocked"
            or classification.get("status") in strong_statuses
        ):
            return dict(classification)
    return None


def classify_attempt(
    *,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = meta or {}
    stdout_json = extract_stdout_json(stdout)
    markers = _meta_markers(meta)
    text_meta = dict(meta)
    text_meta.pop("structured_markers", None)
    preflight = text_meta.get("scrapling_preflight")
    if isinstance(preflight, dict):
        text_meta["scrapling_preflight"] = {
            key: value for key, value in preflight.items() if key != "structured_markers"
        }
    text = _text_blob(
        _stdout_without_json_payload(stdout, stdout_json),
        stderr,
        json.dumps(text_meta, ensure_ascii=False),
        json.dumps(_without_false_security_markers(stdout_json), ensure_ascii=False),
    )

    if meta.get("skipped") and str(meta.get("skip_reason") or "").startswith("video_"):
        return {
            "status": "completed",
            "failure_type": "skipped_video",
            "retryable": False,
            "wait_seconds": 0,
            "reason": meta.get("skip_reason") or "video_target_ignored",
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

    if bool(markers.get("platform_security_limit")):
        return {
            "status": "blocked",
            "failure_type": "platform_security_limit",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "platform_security_limit_300011",
        }

    if exit_code == 0 and not meta.get("blocked_detected"):
        return {
            "status": "completed",
            "failure_type": "success",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "completed",
        }

    child_classification = _strong_child_classification(stdout_json)
    if child_classification:
        return child_classification

    formal_validation = stdout_json.get("formal_validation") or {}
    if stdout_json.get("import_new_target_met") is False and isinstance(formal_validation, dict):
        stop_reason = str(formal_validation.get("stop_reason") or "new_target_not_met")
        return {
            "status": "retry_wait",
            "failure_type": "import_new_target_not_met",
            "retryable": True,
            "wait_seconds": 600,
            "reason": f"formal_import_new_target_not_reached:{stop_reason}",
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

    if PLATFORM_SECURITY_LIMIT_PATTERNS.search(text):
        return {
            "status": "blocked",
            "failure_type": "platform_security_limit",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "platform_security_limit_300011",
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
