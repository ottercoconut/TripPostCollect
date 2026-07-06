"""Shared website definitions for browser login and Scrapling probes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from project_paths import BROWSER_PROFILE_ROOT, BROWSER_STATE_ROOT, PROJECT_ROOT, STEALTH_BROWSER_PROFILE_ROOT


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
    "ctrip": WebSite(
        key="ctrip",
        name="Ctrip",
        default_url="https://you.ctrip.com/travels/jinan128.html",
        login_url="https://passport.ctrip.com/user/login?BackUrl=https%3A%2F%2Fyou.ctrip.com%2F",
        cookie_domains=("ctrip.com",),
        login_hosts=("passport.ctrip.com",),
        recommended_mode="dynamic",
        default_wait_ms=1500,
        min_delay_seconds=75,
        max_requests_per_session=20,
        daily_request_budget=50,
        cooldown_minutes=45,
        account_risk_level="high",
        content_focus="travel guides, sights, old travel notes, POI reviews",
        notes="Useful for Ctrip guides, sights, and old travel notes.",
    ),
    "qunar": WebSite(
        key="qunar",
        name="Qunar",
        default_url="https://travel.qunar.com/p-cs300150-jinan",
        login_url="https://user.qunar.com/passport/login.jsp?ret=https%3A%2F%2Ftravel.qunar.com%2Fp-cs300150-jinan",
        cookie_domains=("qunar.com",),
        login_hosts=("user.qunar.com",),
        recommended_mode="static",
        min_delay_seconds=45,
        max_requests_per_session=30,
        daily_request_budget=80,
        cooldown_minutes=30,
        account_risk_level="medium",
        content_focus="destination guides, travel notes, route lists",
        notes="Guide pages are often static-friendly; login can help with detail pages.",
    ),
    "weibo": WebSite(
        key="weibo",
        name="Weibo mobile",
        default_url="https://m.weibo.cn/search?containerid=100103type%3D1%26q%3D%E6%B5%8E%E5%8D%97%E6%97%85%E6%B8%B8",
        login_url="https://passport.weibo.cn/signin/login?entry=mweibo&r=https%3A%2F%2Fm.weibo.cn%2Fsearch%3Fcontainerid%3D100103type%253D1%2526q%253D%25E6%25B5%258E%25E5%258D%2597%25E6%2597%2585%25E6%25B8%25B8",
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
        default_url="https://www.xiaohongshu.com/search_result?keyword=%E6%B5%8E%E5%8D%97%E6%97%85%E6%B8%B8",
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
    "qyer": WebSite(
        key="qyer",
        name="Qyer",
        default_url="https://place.qyer.com/jinan/",
        login_url="https://passport.qyer.com/login?ref=https%3A%2F%2Fplace.qyer.com%2Fjinan%2F",
        cookie_domains=("qyer.com",),
        login_hosts=("passport.qyer.com",),
        recommended_mode="static",
        min_delay_seconds=45,
        max_requests_per_session=30,
        daily_request_budget=80,
        cooldown_minutes=30,
        account_risk_level="medium",
        content_focus="destination guides, POIs, city impressions, community links",
        notes="Destination pages are static-friendly; login may help community surfaces.",
    ),
    "douban_group": WebSite(
        key="douban_group",
        name="Douban Groups",
        default_url="https://www.douban.com/group/topic/53104421/",
        login_url="https://accounts.douban.com/passport/login?source=group",
        cookie_domains=("douban.com",),
        login_hosts=("accounts.douban.com",),
        login_required=False,
        active=True,
        recommended_mode="static",
        default_wait_ms=1200,
        min_delay_seconds=120,
        max_requests_per_session=12,
        daily_request_budget=30,
        cooldown_minutes=60,
        account_risk_level="medium",
        content_focus="public group topics, travel discussions, long-tail user experiences",
        static_referer="",
        notes="Only collect public topic URLs at low frequency. Do not use group search paths; many topics require login.",
    ),
    "bilibili": WebSite(
        key="bilibili",
        name="Bilibili",
        default_url="https://www.bilibili.com/opus/917192602541883449",
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
        default_url="https://www.douyin.com/note/7628472839242454346",
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
        default_url="https://www.zhihu.com/question/538549565",
        login_url="https://www.zhihu.com/signin?next=%2Fquestion%2F538549565",
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
    "mafengwo": WebSite(
        key="mafengwo",
        name="Mafengwo",
        default_url="https://www.mafengwo.cn/search/q.php?q=%E6%B5%8E%E5%8D%97&t=notes",
        login_url="https://passport.mafengwo.cn/?return_url=https%3A%2F%2Fwww.mafengwo.cn%2Fsearch%2Fq.php%3Fq%3D%25E6%25B5%258E%25E5%258D%2597%26t%3Dnotes",
        cookie_domains=("mafengwo.cn",),
        login_hosts=("passport.mafengwo.cn",),
        login_required=False,
        active=False,
        recommended_mode="dynamic",
        min_delay_seconds=120,
        max_requests_per_session=0,
        daily_request_budget=0,
        cooldown_minutes=60,
        account_risk_level="high",
        content_focus="travel note search, login-gated note details",
        notes="Currently inactive. Search was visible; note details were login-gated in earlier probes.",
    ),
    "tuniu": WebSite(
        key="tuniu",
        name="Tuniu",
        default_url="https://m.tuniu.com/g2402/jianjie/",
        login_url="https://passport.tuniu.com/login",
        cookie_domains=("tuniu.com",),
        login_hosts=("passport.tuniu.com",),
        login_required=False,
        active=False,
        recommended_mode="static",
        min_delay_seconds=60,
        max_requests_per_session=0,
        daily_request_budget=0,
        cooldown_minutes=30,
        account_risk_level="low",
        content_focus="guide/product pages",
        notes="Mostly guide/product content; login is optional for our current use.",
    ),
    "bendibao": WebSite(
        key="bendibao",
        name="Bendibao",
        default_url="https://jn.bendibao.com/tour/",
        login_url=None,
        cookie_domains=("bendibao.com",),
        login_hosts=(),
        login_required=False,
        active=False,
        recommended_mode="static",
        min_delay_seconds=60,
        max_requests_per_session=0,
        daily_request_budget=0,
        cooldown_minutes=30,
        account_risk_level="low",
        content_focus="local guide/news reference pages",
        notes="Local guide/news content. No login workflow is needed.",
    ),
    "wechat_mp": WebSite(
        key="wechat_mp",
        name="WeChat Official Account Articles",
        default_url="https://mp.weixin.qq.com/",
        login_url=None,
        cookie_domains=("mp.weixin.qq.com",),
        login_hosts=(),
        login_required=False,
        active=False,
        recommended_mode="static",
        min_delay_seconds=300,
        max_requests_per_session=0,
        daily_request_budget=0,
        cooldown_minutes=240,
        account_risk_level="high",
        content_focus="official account article links, manual/single-link review only",
        notes="Inactive for automation: mp.weixin.qq.com robots disallows ordinary crawling and test links returned captcha.",
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
