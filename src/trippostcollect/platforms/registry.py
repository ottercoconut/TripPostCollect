"""Shared website definitions for browser login and Scrapling probes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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


SITES: dict[str, WebSite] = {
    "weibo": WebSite(
        key="weibo",
        name="Weibo mobile",
        default_url="https://m.weibo.cn/search?containerid=100103type%3D1%26q%3D%E9%9D%92%E5%B2%9B%E6%97%85%E6%B8%B8",
        login_url="https://passport.weibo.cn/signin/login?entry=mweibo&r=https%3A%2F%2Fm.weibo.cn%2Fsearch%3Fcontainerid%3D100103type%253D1%2526q%253D%25E9%259D%2592%25E5%25B2%259B%25E6%2597%2585%25E6%25B8%25B8",
        cookie_domains=("weibo.cn", "weibo.com", "sina.com.cn"),
        login_hosts=("passport.weibo.cn", "passport.weibo.com"),
        recommended_mode="dynamic",
        mobile_context=True,
        default_wait_ms=6000,
        min_delay_seconds=120,
        max_requests_per_session=15,
        daily_request_budget=40,
        cooldown_minutes=60,
        account_risk_level="high",
        content_focus="public search posts, post details, author metrics",
        notes="Mobile web is the most useful surface for public search/post data.",
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
        notes="Use MediaCrawler with headed/CDP mode and a warmed browser profile; do not process video media.",
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
