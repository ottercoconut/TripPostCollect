#!/usr/bin/env python3
"""Deterministic crawl attempt failure classification."""

from __future__ import annotations

import json
import re
from typing import Any


CAPTCHA_PATTERNS = re.compile(r"验证码|人机验证|安全验证|captcha|turnstile|geetest|滑块|wappoc_appmsgcaptcha", re.I)
LOGIN_PATTERNS = re.compile(r"登录后|请登录|需要登录|(?<![A-Za-z])sign[ _-]?in(?![A-Za-z])|login_required|passport|未登录", re.I)
RUNTIME_PERMISSION_PATTERNS = re.compile(
    r"Failed to initialize cache|Operation not permitted|Permission denied|browser_runtime_permission",
    re.I,
)
BROWSER_LAUNCH_PATTERNS = re.compile(
    r"TargetClosedError|BrowserType\.launch|launch_persistent_context|SIGABRT|signal 6|crashpad|kill EPERM",
    re.I,
)
IMPORT_TARGET_PATTERNS = re.compile(r"import_target_not_met", re.I)
RATE_PATTERNS = re.compile(r"429|too many requests|rate limit|访问过于频繁|请求过于频繁|操作频繁", re.I)
TIMEOUT_PATTERNS = re.compile(r"Timeout|timeout|ETIMEDOUT|Navigation timeout|net::ERR_TIMED_OUT", re.I)
NO_IMAGE_PATTERNS = re.compile(r"No image-bearing|no_content_images|skipped_no_image", re.I)
PARSE_PATTERNS = re.compile(r"JSONDecodeError|parse_failed|Selector|KeyError|ValueError", re.I)
BLOCK_PATTERNS = re.compile(r"forbidden|access denied|拒绝访问|blocked_detected|blocked_by_policy", re.I)


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


def classify_attempt(
    *,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = meta or {}
    stdout_json = extract_stdout_json(stdout)
    text = _text_blob(stdout, stderr, json.dumps(meta, ensure_ascii=False), json.dumps(stdout_json, ensure_ascii=False))
    markers = _meta_markers(meta)

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

    if exit_code == 0 and not meta.get("blocked_detected"):
        return {
            "status": "completed",
            "failure_type": "success",
            "retryable": False,
            "wait_seconds": 0,
            "reason": "completed",
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
            "failure_type": "import_target_not_met",
            "retryable": True,
            "wait_seconds": 600,
            "reason": "formal_import_target_not_reached",
        }

    return {
        "status": "retry_wait",
        "failure_type": "tool_error",
        "retryable": True,
        "wait_seconds": 600,
        "reason": "nonzero_exit_without_specific_classification",
    }
