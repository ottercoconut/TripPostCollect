"""小红书页面取数规则与规则端点响应分类；只读截获的状态码、业务码与消息，不发请求。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trippostcollect.platforms.xhs.errors import IPBlockError, PlatformRuntimeError, XHSLoginExpired
from trippostcollect.runtime.page_capture import CaptureRule, CapturedResponse, SentinelRule


API_RESOURCE_TYPES = frozenset({"xhr", "fetch"})
XHS_SEARCH_RULE = CaptureRule(
    name="xhs.search_notes",
    stage="search_results",
    method="POST",
    host="so.xiaohongshu.com",
    path="/api/sns/web/v2/search/notes",
    resource_types=API_RESOURCE_TYPES,
    body="json",
)
XHS_FEED_RULE = CaptureRule(
    name="xhs.feed",
    stage="note_detail",
    method="POST",
    host="edith.xiaohongshu.com",
    path="/api/sns/web/v1/feed",
    resource_types=API_RESOURCE_TYPES,
    body="json",
)
XHS_USER_ME_RULE = CaptureRule(
    name="xhs.user_me",
    stage="login_state",
    method="GET",
    host="edith.xiaohongshu.com",
    path="/api/sns/web/v2/user/me",
    resource_types=API_RESOURCE_TYPES,
    body="json",
)
# 头像主机不在图片规则内，永不读取。
XHS_IMAGE_RULE = CaptureRule(
    name="xhs.note_image",
    stage="note_image",
    method="GET",
    host="*.xhscdn.com",
    path=None,
    resource_types=frozenset({"image"}),
    body="bytes",
    host_exclude_prefixes=("sns-avatar",),
)
XHS_RULES = (XHS_SEARCH_RULE, XHS_FEED_RULE, XHS_USER_ME_RULE, XHS_IMAGE_RULE)
# 非规则端点（埋点、安全类、评论、分享码等）的异常状态只写诊断；461/471 交平台检查验证界面。
XHS_SENTINEL = SentinelRule(
    host="*.xiaohongshu.com",
    resource_types=API_RESOURCE_TYPES,
    statuses=frozenset({401, 403, 429, 461, 471}),
    surface_check_statuses=frozenset({461, 471}),
)
XHS_VERIFY_HEADERS = ("verifytype", "verifyuuid")

XHS_IP_BLOCK_CODE = "300012"
XHS_SECURITY_LIMIT_CODE = "300011"
XHS_NOTE_NOT_FOUND_CODES = frozenset({"-510000", "-510001"})
XHS_NOT_LOGGED_IN_CODE = "-101"
LOGIN_EXPIRED_MARKERS = (
    "登录已过期",
    "登录状态已失效",
    "登录失效",
    "login expired",
    "login has expired",
    "session expired",
)


def login_expired_markers() -> tuple[str, ...]:
    return LOGIN_EXPIRED_MARKERS


@dataclass(frozen=True)
class XhsApiVerdict:
    """规则端点的分类结果：kind 取 ok、login_expired、verification、rate_limited、auth、
    security_limit_300011、ip_blocked_300012、note_not_found 或 business_error。"""

    kind: str
    status: int
    code: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    @property
    def detail(self) -> str:
        return f"business_error:{self.code or 'unknown'}" if self.kind == "business_error" else self.kind


def _payload(captured: CapturedResponse) -> dict[str, Any] | None:
    return captured.json if isinstance(captured.json, dict) else None


def classify_xhs_api_response(captured: CapturedResponse) -> XhsApiVerdict:
    """只用于三个规则端点；码表沿用原 HTTP 客户端，按状态码、业务码、消息的先后判定。"""
    status = int(captured.status)
    if status in {461, 471}:
        return XhsApiVerdict("verification", status)
    if status == 429:
        return XhsApiVerdict("rate_limited", status)
    if status in {401, 403}:
        return XhsApiVerdict("auth", status)
    payload = _payload(captured)
    code = str(payload.get("code")).strip() if payload and payload.get("code") is not None else ""
    if code == XHS_IP_BLOCK_CODE:
        return XhsApiVerdict("ip_blocked_300012", status, code)
    if code == XHS_SECURITY_LIMIT_CODE:
        return XhsApiVerdict("security_limit_300011", status, code)
    if payload is None:
        return XhsApiVerdict("business_error", status, "unreadable" if status == 200 else f"http_{status}")
    if payload.get("success"):
        return XhsApiVerdict("ok", status, code)
    if code in XHS_NOTE_NOT_FOUND_CODES:
        return XhsApiVerdict("note_not_found", status, code)
    message = str(payload.get("msg") or "").casefold()
    if code == XHS_NOT_LOGGED_IN_CODE or any(marker.casefold() in message for marker in LOGIN_EXPIRED_MARKERS):
        return XhsApiVerdict("login_expired", status, code)
    return XhsApiVerdict("business_error", status, code)


def raise_for_runtime_verdict(verdict: XhsApiVerdict, *, stage: str) -> None:
    """运行级判定只来自规则端点；验证与业务失败交调用方处理，这里不抛。"""
    if verdict.kind == "rate_limited":
        raise PlatformRuntimeError(f"XHS {stage} HTTP 429", code="rate_limited")
    if verdict.kind == "auth":
        raise PlatformRuntimeError(f"XHS {stage} HTTP {verdict.status}", code="login_required")
    if verdict.kind == "security_limit_300011":
        raise PlatformRuntimeError(
            f"XHS platform security limit, code {XHS_SECURITY_LIMIT_CODE}",
            code="platform_security_limit_300011",
        )
    if verdict.kind == "ip_blocked_300012":
        raise IPBlockError("Network connection error, please check network settings or restart")
    if verdict.kind == "login_expired":
        raise XHSLoginExpired(stage)


def user_me_state(captured: CapturedResponse) -> str:
    """user/me 的登录态：logged_in（guest=false 且有 user_id）、guest，或 unknown。"""
    verdict = classify_xhs_api_response(captured)
    payload = _payload(captured) or {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if verdict.ok and data.get("guest") is False and str(data.get("user_id") or "").strip():
        return "logged_in"
    if verdict.kind == "login_expired" or (verdict.ok and data.get("guest") is True):
        return "guest"
    return "unknown"


def user_me_logged_in(item: Any) -> bool:
    return isinstance(item, CapturedResponse) and user_me_state(item) == "logged_in"
