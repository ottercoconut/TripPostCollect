#!/usr/bin/env python3
"""Deterministic crawl attempt failure classification."""

from __future__ import annotations



import json
import re
import signal
from typing import Any

from trippostcollect.records.text_signals import (
    XHS_LEGACY_LOGIN_TERMINAL_PATTERNS as XHS_LEGACY_LOGIN_TERMINAL_PATTERNS,
    XHS_SMS_DAILY_LIMIT_PATTERNS as XHS_SMS_DAILY_LIMIT_PATTERNS,
    XHS_SMS_FREQUENCY_PATTERNS as XHS_SMS_FREQUENCY_PATTERNS,
    XHS_SMS_PARAMETER_TERMINAL_PATTERNS as XHS_SMS_PARAMETER_TERMINAL_PATTERNS,
    _XHS_DAILY as _XHS_DAILY,
    _XHS_LIMIT as _XHS_LIMIT,
    _XHS_SMS_CONTEXT as _XHS_SMS_CONTEXT,
    _XHS_SMS_PARAMETER_ERROR as _XHS_SMS_PARAMETER_ERROR,
    _XHS_SMS_VERIFICATION as _XHS_SMS_VERIFICATION,
    _xhs_sms_terminal_reason as _xhs_sms_terminal_reason,
    _xhs_stable_sms_terminal_reason as _xhs_stable_sms_terminal_reason,
    is_xhs_sms_terminal_text as is_xhs_sms_terminal_text,
)


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
# 中间层收到操作人信号后在 stderr 写出的稳定行（runtime.process.operator_interrupt_error）。
MIDDLE_OPERATOR_INTERRUPT_PATTERNS = re.compile(
    r"(?<![A-Za-z0-9_])runtime_failed:operator_interrupt:(SIG[A-Z0-9]+)(?![A-Za-z0-9_])",
)
# worker 生命周期（runtime.worker.run）收到首个 SIGINT 时打印的固定行；worker 随后仍可能以 0 退出。
# 只认 SIGINT：runner、中间层与无进展看门狗沿链路只发 SIGTERM，worker 收到的 SIGINT 只能来自链路外的操作人。
WORKER_OPERATOR_INTERRUPT_PATTERNS = re.compile(
    rf"\[Main\] Received interrupt signal {int(signal.SIGINT)}, exiting",
)
IMPORT_TARGET_PATTERNS = re.compile(r"import_new_target_not_met", re.I)
# 平台层结构化错误码 rate_limited（下划线）与自然语言 "rate limit" 同为频控（#112）。
RATE_PATTERNS = re.compile(r"429|too many requests|rate[ _]limit(?:ed)?|访问过于频繁|请求过于频繁|操作频繁", re.I)
XHS_FREQUENT_CHALLENGE_PATTERNS = re.compile(
    r"访问(?:过于)?频繁|请求(?:过于)?频繁|操作(?:过于)?频繁|"
    r"requests?\s+(?:are\s+)?too\s+frequent|too many requests|rate limit",
    re.I,
)
TIMEOUT_PATTERNS = re.compile(r"Timeout|timeout|ETIMEDOUT|Navigation timeout|net::ERR_TIMED_OUT", re.I)
# #129：通用四平台浏览器外请求重试用尽后仍是传输层失败时写出的稳定停止码（网络整体故障）。
NETWORK_UNREACHABLE_PATTERNS = re.compile(r"(?<![A-Za-z0-9_])network_unreachable(?![A-Za-z0-9_])")
NETWORK_UNREACHABLE_CLASSIFICATION = {
    "status": "retry_wait",
    "failure_type": "network_timeout",
    "retryable": True,
    "wait_seconds": 900,
    "reason": "network_unreachable",
}
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


def _latest_child_classification(summary: dict[str, Any]) -> dict[str, Any]:
    classification = _latest_child_record(summary).get("failure_classification") or {}
    return dict(classification) if isinstance(classification, dict) else {}


def _latest_child_record(summary: dict[str, Any]) -> dict[str, Any]:
    records = summary.get("records")
    if not isinstance(records, list):
        return {}
    return next(
        (record for record in reversed(records) if isinstance(record, dict)),
        {},
    )


def _latest_worker_output(summary: dict[str, Any]) -> str:
    """最近一条平台记录里中间层保存的 worker 输出尾部（已清洗）；只用于匹配固定的中断行。"""

    run = _latest_child_record(summary).get("run") or {}
    if not isinstance(run, dict):
        return ""
    return _text_blob(run.get("stdout_tail"), run.get("stderr_tail"))


def _strong_child_classification(summary: dict[str, Any]) -> dict[str, Any] | None:
    strong_statuses = {"captcha_detected", "login_required", "blocked", "failed_final"}
    classification = _latest_child_classification(summary)
    if (
        classification.get("failure_type") == "policy_blocked"
        or classification.get("status") in strong_statuses
    ):
        return classification
    return None


def _summary_runtime_blocker_classification(
    summary: dict[str, Any],
    *,
    platform: str,
) -> dict[str, Any] | None:
    """把子摘要里本轮的运行级阻断还原成调度分类。

    阻断来自最近一条平台记录时直接沿用该记录的分类（保留 wait_seconds）；来自分页停止事件时，
    按停止细节重新分类取得状态与等待时间，原因保留停止细节。
    """

    blocker = summary.get("runtime_blocker") or {}
    if not isinstance(blocker, dict):
        return None
    failure_type = str(blocker.get("failure_type") or "")
    reason = str(blocker.get("reason") or "")
    if failure_type not in RUNTIME_BLOCKING_FAILURE_TYPES or not reason:
        return None
    if failure_type == "runtime_failed" and NETWORK_UNREACHABLE_PATTERNS.search(reason):
        # #129：停止细节是网络整体故障时按网络超时等待重试，原因保留停止细节。
        return {**NETWORK_UNREACHABLE_CLASSIFICATION, "reason": reason}
    record_classification = _latest_child_classification(summary)
    if record_classification.get("failure_type") == failure_type:
        return record_classification
    if failure_type == "runtime_failed":
        return {
            "status": "retry_wait",
            "failure_type": "runtime_failed",
            "retryable": True,
            "wait_seconds": 600,
            "reason": reason,
        }
    detail_classification = classify_attempt(
        exit_code=1,
        stderr=reason,
        meta={"platform": str(blocker.get("platform") or platform)},
    )
    if detail_classification.get("failure_type") == failure_type:
        return {**detail_classification, "reason": reason}
    return {
        "status": (
            failure_type
            if failure_type in {"login_required", "captcha_detected"}
            else str(blocker.get("status") or "blocked")
        ),
        "failure_type": failure_type,
        "retryable": bool(blocker.get("retryable", False)),
        "wait_seconds": int(blocker.get("wait_seconds") or 0),
        "reason": reason,
    }


def _operator_interrupt_classification(signum: int) -> dict[str, Any]:
    """child 输出里可见、runner 自身未锁存的操作人中断。

    与 runner 的中断分类同形：按 ``runtime_failed`` 重试，不写来源耗尽；带 ``interrupt`` 使调度表
    不累计失败次数，也不因 ``max_attempts`` 升级为 ``failed_final``。
    """

    name = signal.Signals(signum).name
    return {
        "status": "retry_wait",
        "failure_type": "runtime_failed",
        "retryable": True,
        "wait_seconds": 0,
        "reason": "operator_interrupt",
        "stop_reason": "runtime_failed",
        "stop_detail": "operator_interrupt",
        "interrupt": {
            "reason": "operator_interrupt",
            "signum": signum,
            "signal": name,
            "error": f"runtime_failed:operator_interrupt:{name}",
            "source": "child_output",
        },
    }


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
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按退出码、输出文本与子摘要给出一次尝试的调度分类。

    ``summary`` 是子进程的 summary.json。子进程 stdout 只打印终态信封（路径、状态与
    ``failure_reason``），记录级分类、运行级阻断与正式门禁只能从摘要读取，不从 stdout 推断。
    摘要只按结构化字段读取，不并入文本匹配，避免帖子正文里的“请登录”等字样被误判。
    """

    meta = meta or {}
    child_summary = summary if isinstance(summary, dict) else {}
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

    structured_runtime_blocker = _structured_runtime_blocker(child_summary)
    if structured_runtime_blocker:
        return structured_runtime_blocker

    middle_interrupt = MIDDLE_OPERATOR_INTERRUPT_PATTERNS.search(stderr)
    if middle_interrupt and middle_interrupt.group(1) in signal.Signals.__members__:
        return _operator_interrupt_classification(int(signal.Signals[middle_interrupt.group(1)]))

    checkpoint_failure = re.search(
        r"^RuntimeError: (xhs_batch_checkpoint_[a-z0-9_]+)\s*$", stderr, re.M
    )
    if platform == "xhs" and exit_code and checkpoint_failure:
        return {
            "status": "failed_final",
            "failure_type": "runtime_failed",
            "retryable": False,
            "wait_seconds": 0,
            "reason": checkpoint_failure.group(1),
        }

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

    child_classification = _strong_child_classification(child_summary)
    if child_classification:
        return child_classification

    summary_blocker = _summary_runtime_blocker_classification(child_summary, platform=platform)
    if summary_blocker:
        return summary_blocker

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

    # worker 被操作人 SIGINT 收束后可能以 0 退出，中间层随后写出“未耗尽”摘要；该行只在中间层保存的
    # worker 输出尾部可见。它先于来源耗尽未持久化，避免把中断记成需要 600 秒重试的普通未耗尽。
    if WORKER_OPERATOR_INTERRUPT_PATTERNS.search(
        _text_blob(terminal_text, _latest_worker_output(child_summary))
    ):
        return _operator_interrupt_classification(int(signal.SIGINT))

    formal_validation = child_summary.get("formal_validation")
    if child_summary.get("import_completion_met") is False and isinstance(formal_validation, dict):
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

    if NETWORK_UNREACHABLE_PATTERNS.search(text):
        return dict(NETWORK_UNREACHABLE_CLASSIFICATION)

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
RUNTIME_BLOCKING_FAILURE_TYPES = frozenset(
    {
        "runtime_failed",
        "policy_blocked",
        "platform_security_limit",
        "sms_verification_terminal",
        "manual_checkpoint_timeout",
        "captcha_detected",
        "login_required",
        "rate_limited",
        "blocked_or_forbidden",
        "runtime_permission_error",
        "browser_launch_failed",
        "browser_target_closed",
        "browser_runtime_failed",
        "login_runtime_error",
        "verification_timeout",
        "ip_blocked",
    }
)


def runtime_blocker_stop_reason(failure_type: str) -> str:
    """Map a diagnostic failure family onto the formal stop-state contract."""

    if failure_type in {"login_required", "captcha_detected"}:
        return failure_type
    return "runtime_failed"


def runtime_blocker_from_terminal_event(event: Any) -> dict[str, Any]:
    """Normalize the safe child event written before login exceptions escape."""

    if not isinstance(event, dict):
        return {}
    failure_type = str(event.get("failure_type") or "")
    stop_detail = str(event.get("stop_detail") or "")
    if failure_type not in RUNTIME_BLOCKING_FAILURE_TYPES or not stop_detail:
        return {}
    stop_reason = str(event.get("stop_reason") or "")
    if stop_reason not in {"runtime_failed", "login_required", "captcha_detected"}:
        stop_reason = runtime_blocker_stop_reason(failure_type)
    return {
        "platform": "xhs",
        "status": "blocked",
        "failure_type": failure_type,
        "stop_reason": stop_reason,
        "reason": stop_detail,
        "retryable": bool(event.get("retryable", False)),
        "source": "xhs_runtime_terminal",
    }


def runtime_blocker_from_pagination_evidence(
    pagination_evidence: Any,
    platforms: list[str],
) -> dict[str, Any]:
    """Recover a run blocker from the current adaptive stop event."""

    if not isinstance(pagination_evidence, dict):
        return {}
    stop_event = pagination_evidence.get("stop_event") or {}
    if not isinstance(stop_event, dict):
        return {}
    stop_reason = str(stop_event.get("stop_reason") or "")
    if stop_reason not in {"runtime_failed", "login_required", "captcha_detected"}:
        return {}
    stop_detail = str(stop_event.get("stop_detail") or stop_reason)
    platform = str(stop_event.get("platform") or "")
    if platform not in platforms:
        platform = platforms[0] if len(platforms) == 1 else ""
    if not platform:
        return {}
    classification = classify_attempt(
        exit_code=1,
        stderr=stop_detail,
        meta={"platform": platform},
    )
    failure_type = str(classification.get("failure_type") or "")
    if failure_type not in RUNTIME_BLOCKING_FAILURE_TYPES:
        failure_type = (
            stop_reason
            if stop_reason in {"login_required", "captcha_detected"}
            else "runtime_failed"
        )
    return {
        "platform": platform,
        "status": "blocked",
        "failure_type": failure_type,
        "stop_reason": stop_reason,
        "reason": stop_detail,
        "retryable": False,
        "source": "adaptive_search_stopped",
    }


def latest_runtime_blocker(
    records: list[dict[str, Any]],
    platforms: list[str],
) -> dict[str, Any]:
    """Return only the current attempt's structured blocker per platform.

    ``records`` can start with resumed campaign records.  Reading each
    platform from the end prevents a historical blocker from contaminating a
    newer attempt.
    """

    latest_by_platform: dict[str, dict[str, Any]] = {}
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        platform = str(record.get("platform") or "")
        if platform in platforms and platform not in latest_by_platform:
            latest_by_platform[platform] = record

    for platform in platforms:
        record = latest_by_platform.get(platform) or {}
        repair_report = record.get("repair_report") or {}
        repair_runtime_blocker = repair_report.get("runtime_blocker") or {}
        blocker_code = str(repair_runtime_blocker.get("error_code") or "")
        if blocker_code in RUNTIME_BLOCKING_FAILURE_TYPES:
            return {
                "platform": platform,
                "status": "blocked",
                "failure_type": blocker_code,
                "stop_reason": runtime_blocker_stop_reason(blocker_code),
                "reason": str(
                    repair_runtime_blocker.get("reason") or blocker_code
                ),
                "retryable": bool(
                    repair_runtime_blocker.get("retryable", False)
                ),
            }
        classification = record.get("failure_classification") or {}
        failure_type = str(
            classification.get("failure_type") or ""
        )
        if failure_type in RUNTIME_BLOCKING_FAILURE_TYPES:
            return {
                "platform": platform,
                "status": str(classification.get("status") or "blocked"),
                "failure_type": failure_type,
                "stop_reason": runtime_blocker_stop_reason(failure_type),
                "reason": str(classification.get("reason") or failure_type),
                "retryable": bool(classification.get("retryable", False)),
            }
    return {}


def apply_runtime_blocker(
    validation: dict[str, Any],
    runtime_blocker: dict[str, Any],
) -> dict[str, Any]:
    """Project the current attempt's blocker without losing its subtype."""

    if not runtime_blocker:
        return validation
    return {
        **validation,
        "pagination_runtime_blocked": True,
        "stop_reason": str(runtime_blocker.get("stop_reason") or "")
        or runtime_blocker_stop_reason(str(runtime_blocker["failure_type"])),
        "stop_detail": str(runtime_blocker["reason"]),
        "runtime_blocker": runtime_blocker,
    }
