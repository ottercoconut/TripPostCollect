"""只依赖标准库的终止短信挑战文本判别。"""

from __future__ import annotations

import re


XHS_LEGACY_LOGIN_TERMINAL_PATTERNS = re.compile(
    r"(?<![A-Za-z0-9_])xhs_login_verification_terminal:([^\r\n]+)",
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
