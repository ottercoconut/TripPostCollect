"""Shared website definitions for browser login and Scrapling probes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trippostcollect.core.paths import (
    BROWSER_PROFILE_ROOT,
    BROWSER_STATE_ROOT,
    PROJECT_ROOT,
    STEALTH_BROWSER_PROFILE_ROOT,
)


ROOT = PROJECT_ROOT
DEFAULT_PROFILE_ROOT = BROWSER_PROFILE_ROOT
DEFAULT_STATE_DIR = BROWSER_STATE_ROOT


@dataclass(frozen=True)
class WebSite:
    key: str
    name: str
    default_url: str
    login_url: str | None
    cookie_domains: tuple[str, ...]
    login_hosts: tuple[str, ...]
    login_required: bool = True
    active: bool = True
    recommended_mode: str = "static"
    preferred_engine: str = "playwright"
    mobile_context: bool = False
    default_wait_ms: int = 800
    min_delay_seconds: int = 60
    jitter_ratio: float = 0.25
    min_jitter_seconds: float = 2.0
    max_jitter_seconds: float = 45.0
    max_requests_per_session: int = 20
    daily_request_budget: int = 50
    cooldown_minutes: int = 30
    account_risk_level: str = "medium"
    content_focus: str = ""
    profile_dir_override: Path | None = None
    static_referer: str | None = None
    notes: str = ""
    # #217：目前只有微博接受 job 参数里的显式时间窗。通用代码只看这个能力。
    supports_time_window: bool = False


SITES: dict[str, WebSite] = {
    "weibo": WebSite(
        key="weibo",
        name="Weibo",
        default_url="https://s.weibo.com/weibo?q=%E9%9D%92%E5%B2%9B%E6%97%85%E6%B8%B8&nodup=1",
        login_url="https://passport.weibo.com/sso/signin",
        cookie_domains=("weibo.com",),
        login_hosts=("passport.weibo.com",),
        recommended_mode="dynamic",
        mobile_context=False,
        default_wait_ms=6000,
        min_delay_seconds=120,
        max_requests_per_session=15,
        daily_request_budget=40,
        cooldown_minutes=60,
        account_risk_level="high",
        content_focus="desktop search posts, post details, author metrics",
        notes="Desktop search at s.weibo.com is the page-state source. Persistent profile holds the login.",
        supports_time_window=True,
    ),
    "xhs": WebSite(
        key="xhs",
        name="Xiaohongshu",
        default_url="https://www.xiaohongshu.com/search_result?keyword=%E9%9D%92%E5%B2%9B%E6%97%85%E6%B8%B8",
        login_url="https://www.xiaohongshu.com",
        cookie_domains=("xiaohongshu.com",),
        login_hosts=("www.xiaohongshu.com",),
        login_required=True,
        active=True,
        recommended_mode="dynamic",
        default_wait_ms=4500,
        min_delay_seconds=240,
        max_requests_per_session=6,
        daily_request_budget=16,
        cooldown_minutes=180,
        account_risk_level="high",
        content_focus="search notes, image-text details, visible engagement metrics, creator profile metrics",
        notes="Use headed/CDP mode and a warmed browser profile; do not process video media.",
    ),
    "bilibili": WebSite(
        key="bilibili",
        name="Bilibili",
        default_url="https://search.bilibili.com/article?keyword=%E9%9D%92%E5%B2%9B%E6%97%85%E6%B8%B8",
        login_url="https://passport.bilibili.com/login",
        cookie_domains=("bilibili.com",),
        login_hosts=("passport.bilibili.com",),
        login_required=True,
        active=True,
        recommended_mode="static",
        default_wait_ms=1200,
        min_delay_seconds=120,
        max_requests_per_session=12,
        daily_request_budget=30,
        cooldown_minutes=60,
        account_risk_level="high",
        content_focus="opus/image-text posts, articles, creator/video-adjacent metrics",
        notes="Use low-frequency static Opus/article detail capture with saved login cookies.",
    ),
    "douyin": WebSite(
        key="douyin",
        name="Douyin",
        default_url="https://www.douyin.com/search/%E9%9D%92%E5%B2%9B%E6%97%85%E6%B8%B8",
        login_url="https://www.douyin.com/",
        cookie_domains=("douyin.com", "iesdouyin.com", "amemv.com"),
        login_hosts=("sso.douyin.com", "login.douyin.com"),
        login_required=True,
        active=True,
        recommended_mode="dynamic",
        mobile_context=True,
        default_wait_ms=4500,
        min_delay_seconds=180,
        max_requests_per_session=8,
        daily_request_budget=20,
        cooldown_minutes=120,
        account_risk_level="high",
        content_focus="note/image-text posts, mobile share detail pages, visible engagement metrics",
        notes="Use mobile dynamic detail capture only; avoid search/profile crawling unless explicitly needed.",
    ),
    "zhihu": WebSite(
        key="zhihu",
        name="Zhihu",
        default_url="https://www.zhihu.com/search?q=%E9%9D%92%E5%B2%9B%E6%97%85%E6%B8%B8&type=content",
        login_url="https://www.zhihu.com/signin?next=%2Fsearch%3Fq%3D%25E9%259D%2592%25E5%25B2%259B%25E6%2597%2585%25E6%25B8%25B8%26type%3Dcontent",
        cookie_domains=("zhihu.com",),
        login_hosts=(),
        recommended_mode="stealth",
        preferred_engine="patchright",
        default_wait_ms=3000,
        min_delay_seconds=180,
        max_requests_per_session=10,
        daily_request_budget=25,
        cooldown_minutes=120,
        account_risk_level="high",
        content_focus="long-form answers, articles, author profile metadata",
        profile_dir_override=STEALTH_BROWSER_PROFILE_ROOT / "zhihu",
        notes="Useful for long-form answers/articles, but anti-bot pressure is moderate.",
    ),
}


def site_keys(include_no_login: bool = True) -> list[str]:
    return [
        key
        for key, site in SITES.items()
        if include_no_login or site.login_required
    ]


async def read_douyin_homepage_login(context, page) -> bool:
    """首页 ``HasUserLogin`` 是否为 ``\"1\"``。实现留在抖音平台，避免应用层直接导入平台包。"""

    from trippostcollect.platforms.douyin.search_page import read_homepage_login

    return await read_homepage_login(context, page)


async def read_weibo_config_uid(context, page) -> dict[str, bool]:
    """首页 ``$CONFIG.uid`` 的存在与真值。实现留在微博平台，避免应用层直接导入平台包。"""

    from trippostcollect.platforms.weibo.search_page import read_homepage_uid

    return await read_homepage_uid(context, page)


def get_site(key: str) -> WebSite:
    try:
        return SITES[key]
    except KeyError as exc:
        choices = ", ".join(site_keys())
        raise ValueError(f"Unknown site {key!r}. Choices: {choices}") from exc


def login_sites(include_no_login: bool = False) -> Iterable[WebSite]:
    for site in SITES.values():
        if site.active and (site.login_required or include_no_login):
            yield site


def profile_dir_for(site_key: str, profile_root: str | Path = DEFAULT_PROFILE_ROOT) -> Path:
    site = get_site(site_key)
    if site.profile_dir_override is not None:
        return site.profile_dir_override
    return Path(profile_root).expanduser() / site_key


def state_path_for(site_key: str, state_dir: str | Path = DEFAULT_STATE_DIR) -> Path:
    return Path(state_dir).expanduser() / f"{site_key}_storage_state.json"


def supports_time_window(platform_key: str) -> bool:
    """平台是否接受 ``params.time_window``。未知平台视为不支持。"""

    try:
        site = get_site(platform_key)
    except ValueError:
        return False
    return bool(site.supports_time_window)


def require_job_time_window(
    platform_key: str,
    params: Mapping[str, Any],
    *,
    job_key: str,
) -> dict[str, str] | None:
    """同步与派发前检查 job 参数。没有 ``time_window`` 时返回 None。"""

    if "time_window" not in params:
        return None
    from trippostcollect.platforms.weibo.time_window import (
        TIME_WINDOW_UNSUPPORTED,
        TimeWindowError,
        parse_time_window,
    )

    if not supports_time_window(platform_key):
        raise TimeWindowError(
            TIME_WINDOW_UNSUPPORTED,
            job_key,
            f"platform={platform_key} does not accept time_window",
        )
    return parse_time_window(params.get("time_window"), job_key=job_key).audit()


def _blank(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_child_time_window(
    platforms: Sequence[str],
    *,
    start: Any,
    end: Any,
    source: Any = None,
    job_key: str,
) -> dict[str, str] | None:
    """child 收到的起止必须成对，并经与 job 参数相同的解析函数校验。"""

    import json

    from trippostcollect.platforms.weibo.time_window import (
        TIME_WINDOW_ARGS_INCOMPLETE,
        TIME_WINDOW_INVALID,
        TIME_WINDOW_UNSUPPORTED,
        TimeWindowError,
        parse_time_window,
    )

    start_text = _blank(start)
    end_text = _blank(end)
    source_text = _blank(source)
    if start_text is None and end_text is None and source_text is None:
        return None
    bounds_incomplete = (start_text is None) != (end_text is None)
    source_without_bounds = source_text is not None and start_text is None
    if bounds_incomplete or source_without_bounds:
        raise TimeWindowError(
            TIME_WINDOW_ARGS_INCOMPLETE,
            job_key,
            "time window start and end must both be set",
        )
    for platform_key in platforms:
        if not supports_time_window(platform_key):
            raise TimeWindowError(
                TIME_WINDOW_UNSUPPORTED,
                job_key,
                f"platform={platform_key} does not accept time_window",
            )
    flagged = parse_time_window({"start": start_text, "end": end_text}, job_key=job_key)
    if source_text is None:
        return flagged.audit()
    try:
        payload = json.loads(source_text)
    except json.JSONDecodeError as exc:
        raise TimeWindowError(TIME_WINDOW_INVALID, job_key, "time window source is not json") from exc
    raw = parse_time_window(payload, job_key=job_key)
    if (raw.start, raw.end) != (flagged.start, flagged.end):
        raise TimeWindowError(
            TIME_WINDOW_INVALID,
            job_key,
            "normalized bounds do not match the time window source",
        )
    return raw.audit()


def time_window_worker_env(audit: Mapping[str, str] | None, *, job_key: str) -> dict[str, str]:
    """微博 worker 读取的原始起止。应用层不直接导入平台实现。"""

    from trippostcollect.platforms.weibo.time_window import worker_environment

    return worker_environment(audit, job_key=job_key)
