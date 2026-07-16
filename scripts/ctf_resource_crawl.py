#!/usr/bin/env python3
"""Batch browser crawl for CTF targets with image-resource accounting."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import BrowserContext, Page, Request, Response, TimeoutError as PlaywrightTimeoutError

from browser_runtime import browser_launch_environment, browser_runtime_args
from ctf_browser_resilience import (
    clean_douyin_profile_cookies,
    clear_douyin_context_cookies,
    is_douyin_target,
    navigate_with_commit_and_readiness,
)
from ctf_scrapling_preflight import run_scrapling_static_preflight, should_preflight
from crawl_policy import CrawlPolicyBlocked, site_request_guard, varied_wait_seconds
from human_flow import dwell_on_detail, install_runtime_hints, load_behavior_profile
from trippostcollect.core.paths import CTF_BROWSER_PROFILE_ROOT, CTF_RESOURCE_OUTPUT, PROJECT_ROOT, ensure_dir
from trippostcollect.platforms.registry import SITES, get_site, site_keys


ROOT = PROJECT_ROOT
DEFAULT_OUTPUT = CTF_RESOURCE_OUTPUT
DEFAULT_PROFILE_ROOT = CTF_BROWSER_PROFILE_ROOT
RANDOM = random.SystemRandom()
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
}
TEXT_CONTENT_PREFIXES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/x-javascript",
    "application/xml",
    "application/xhtml+xml",
)
FLAG_PATTERNS = (
    re.compile(r"flag\{[^}\r\n]{1,200}\}", re.I),
    re.compile(r"(?:CTF|DASCTF|[A-Z0-9_]{2,20}CTF)\{[^}\r\n]{1,200}\}", re.I),
    re.compile(r"FLAG[-_:][A-Za-z0-9_./+=-]{8,160}", re.I),
)
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36"
)
VIDEO_URL_RE = re.compile(
    r"(?i)(?:/(?:video|share/video)/\d+|[?&](?:video_id|bvid|aid)=|"
    r"\.(?:mp4|m4v|mov|webm|flv|avi|mkv|m3u8|mpd|ts)(?:[?#]|$))"
)
FORBIDDEN_MEDIA_CONTENT_TYPES = (
    "video/",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
)
RUNTIME_OVERRIDE_SCRIPT = """
(() => {
  const ua = navigator.userAgent || '';
  const isAndroid = /Android/i.test(ua);
  const isMobile = /Mobile|Android/i.test(ua);
  const pluginList = [
    { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
  ];
  const define = (target, prop, getter) => {
    try {
      Object.defineProperty(target, prop, { get: getter, configurable: true });
    } catch (_) {}
  };
  define(navigator, 'webdriver', () => undefined);
  define(navigator, 'languages', () => ['zh-CN', 'zh', 'en-US', 'en']);
  define(navigator, 'plugins', () => pluginList);
  define(navigator, 'platform', () => isAndroid ? 'Linux armv8l' : 'MacIntel');
  define(navigator, 'hardwareConcurrency', () => isMobile ? 6 : 8);
  define(navigator, 'deviceMemory', () => isMobile ? 4 : 8);
  window.chrome = window.chrome || { runtime: {}, app: { isInstalled: false } };
})();
"""


def extract_flags_from_text(text: str) -> list[str]:
    flags: set[str] = set()
    for pattern in FLAG_PATTERNS:
        flags.update(match.group(0) for match in pattern.finditer(text or ""))
    return sorted(flags)


async def extract_published_at(page: Page) -> dict[str, Any]:
    """Extract post publication time from common page metadata without scraping video media."""
    return await page.evaluate(
        """() => {
            const candidates = [];
            const push = (value, source) => {
                const text = String(value || '').trim();
                if (!text || text.length > 80) return;
                candidates.push({ value: text, source });
            };
            const metaSelectors = [
                'meta[property="article:published_time"]',
                'meta[property="og:article:published_time"]',
                'meta[name="article:published_time"]',
                'meta[name="publishdate"]',
                'meta[name="pubdate"]',
                'meta[name="datePublished"]',
                'meta[name="date"]',
                'meta[itemprop="datePublished"]',
                'meta[itemprop="uploadDate"]',
                'meta[itemprop="dateCreated"]',
            ];
            for (const selector of metaSelectors) {
                for (const node of document.querySelectorAll(selector)) {
                    push(node.getAttribute('content') || node.getAttribute('value'), selector);
                }
            }
            for (const node of document.querySelectorAll('time[datetime], [itemprop="datePublished"][datetime], [itemprop="uploadDate"][datetime]')) {
                push(node.getAttribute('datetime'), node.tagName.toLowerCase() + '[datetime]');
            }
            for (const node of document.querySelectorAll('time')) {
                push(node.getAttribute('datetime') || node.textContent, 'time');
            }
            const dateKeys = new Set([
                'datePublished',
                'uploadDate',
                'dateCreated',
                'publishedAt',
                'published_at',
                'publishTime',
                'publish_time',
                'createdAt',
                'created_at',
            ]);
            const walk = (value, path, depth) => {
                if (depth > 5 || value == null) return;
                if (Array.isArray(value)) {
                    value.slice(0, 50).forEach((item, index) => walk(item, `${path}[${index}]`, depth + 1));
                    return;
                }
                if (typeof value === 'object') {
                    for (const [key, child] of Object.entries(value)) {
                        if (dateKeys.has(key)) push(child, `jsonld:${path}.${key}`);
                        walk(child, `${path}.${key}`, depth + 1);
                    }
                }
            };
            for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
                try {
                    walk(JSON.parse(script.textContent || 'null'), '$', 0);
                } catch (_) {}
            }
            const normalized = [];
            const seen = new Set();
            for (const item of candidates) {
                const key = `${item.value}\\u0000${item.source}`;
                if (seen.has(key)) continue;
                seen.add(key);
                normalized.push(item);
            }
            const shanghaiFormatter = new Intl.DateTimeFormat('en-CA', {
                timeZone: 'Asia/Shanghai',
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hourCycle: 'h23',
            });
            const toShanghaiIso = (timestamp) => {
                const parts = Object.fromEntries(
                    shanghaiFormatter
                        .formatToParts(new Date(timestamp))
                        .filter((part) => part.type !== 'literal')
                        .map((part) => [part.type, part.value])
                );
                return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}+08:00`;
            };
            for (const item of normalized) {
                const timestamp = Date.parse(item.value);
                if (Number.isFinite(timestamp)) {
                    return {
                        published_at: toShanghaiIso(timestamp),
                        timezone: 'Asia/Shanghai',
                        source: item.source,
                        raw_value: item.value,
                        candidates: normalized.slice(0, 20),
                    };
                }
            }
            return {
                published_at: null,
                source: null,
                raw_value: null,
                candidates: normalized.slice(0, 20),
            };
        }"""
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl configured CTF targets and summarize image resources.")
    parser.add_argument("--sites", nargs="+", choices=site_keys(include_no_login=True), help="Configured site keys.")
    parser.add_argument("--urls", nargs="+", help="Explicit URLs. Use with optional --site-label.")
    parser.add_argument("--site-label", default="custom", help="Label for explicit URLs.")
    parser.add_argument(
        "--configured-site-urls",
        action="store_true",
        help=(
            "Treat explicit --urls as targets for the configured --site-label, "
            "reusing that site's policy, browser profile, mobile context, and engine."
        ),
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT), help="Output root.")
    parser.add_argument("--headless", action="store_true", help="Run browser headless.")
    parser.add_argument("--max-image-save", type=int, default=30, help="Maximum image bodies saved per target.")
    parser.add_argument("--max-scrolls", type=int, default=6, help="Maximum scroll passes per target.")
    parser.add_argument("--behavior-profile", default="", help="Human-flow behavior profile name. Defaults to each site profile.")
    parser.add_argument("--timeout", type=int, default=60_000, help="Navigation timeout in milliseconds.")
    parser.add_argument("--commit-timeout", type=int, default=12_000, help="Short navigation commit timeout in milliseconds.")
    parser.add_argument("--readiness-timeout", type=int, default=15_000, help="Content readiness timeout in milliseconds.")
    parser.add_argument("--settle-min-ms", type=int, default=2_000, help="Minimum post-load settle wait.")
    parser.add_argument("--settle-max-ms", type=int, default=6_000, help="Maximum post-load settle wait.")
    parser.add_argument(
        "--douyin-cookie-cleanup",
        choices=("auto", "off"),
        default="auto",
        help="Rotate Douyin persistent cookies before and after capture.",
    )
    parser.add_argument(
        "--scrapling-preflight",
        choices=("auto", "off"),
        default="auto",
        help="Run a Scrapling static SSR preflight before browser resource accounting when useful.",
    )
    parser.add_argument(
        "--scrapling-preflight-timeout",
        type=float,
        default=30.0,
        help="Scrapling static preflight timeout in seconds.",
    )
    parser.add_argument("--no-throttle", action="store_true", help="Skip shared crawl policy checks.")
    parser.add_argument("--keyword", default="", help="Optional search keyword to record in capture_meta for downstream import (e.g. 烟台旅游).")
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f%z")


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()[:80] or "target"


def active_targets(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.urls:
        configured = bool(args.configured_site_urls)
        if configured and args.site_label not in SITES:
            raise SystemExit("--configured-site-urls requires --site-label to be a configured site key.")
        site_config = get_site(args.site_label) if configured else None
        return [
            {
                "site": args.site_label,
                "url": url,
                "configured": configured,
                "mobile": bool(site_config.mobile_context) if site_config else False,
                "explicit_url": True,
            }
            for url in args.urls
        ]
    keys = args.sites or [key for key, site in SITES.items() if site.active and site.daily_request_budget > 0]
    return [
        {"site": key, "url": get_site(key).default_url, "configured": True, "mobile": get_site(key).mobile_context}
        for key in keys
    ]


def target_engine(target: dict[str, Any]) -> str:
    if not target.get("configured"):
        return "playwright"
    return get_site(str(target["site"])).preferred_engine


def browser_cache_revision(path: Path) -> int:
    for part in path.parts:
        match = re.fullmatch(r"chromium-(\d+)", part)
        if match:
            return int(match.group(1))
    return -1


def discover_chrome_for_testing_path() -> str | None:
    for env_key in ("TRIPPOSTCOLLECT_CUSTOM_BROWSER_PATH", "CUSTOM_BROWSER_PATH"):
        value = os.environ.get(env_key)
        if value and Path(value).is_file():
            return value

    cache_roots: list[Path] = []
    playwright_browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if playwright_browsers_path and playwright_browsers_path != "0":
        cache_roots.append(Path(playwright_browsers_path).expanduser())
    cache_roots.extend(
        [
            Path.home() / "Library" / "Caches" / "ms-playwright",
            Path.home() / ".cache" / "ms-playwright",
        ]
    )
    patterns = (
        "chromium-*/chrome-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-win/chrome.exe",
    )
    candidates: list[Path] = []
    for root in dict.fromkeys(cache_roots):
        for pattern in patterns:
            candidates.extend(root.glob(pattern))
    for path in sorted(set(candidates), key=browser_cache_revision, reverse=True):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def profile_dir_for_target(target: dict[str, Any]) -> Path:
    site = str(target["site"])
    if target.get("configured"):
        site_config = get_site(site)
        if site_config.profile_dir_override is not None:
            return site_config.profile_dir_override
        return DEFAULT_PROFILE_ROOT / site
    digest = hashlib.sha1(str(target["url"]).encode("utf-8")).hexdigest()[:12]
    return DEFAULT_PROFILE_ROOT / f"{site}_{digest}"


def content_type_base(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def is_image_response(response: Response) -> bool:
    ctype = content_type_base(response.headers.get("content-type", ""))
    if ctype and not ctype.startswith("image/") and (ctype.startswith("application/") or ctype.startswith("text/")):
        return False
    return ctype.startswith("image/") or response.request.resource_type == "image"


PERIPHERAL_IMAGE_URL_RE = re.compile(r"doubanio\.com/f/", re.IGNORECASE)
DOUBAN_TOPIC_URL_RE = re.compile(r"^/group/topic/\d+/?$", re.IGNORECASE)
DOUBAN_PEOPLE_URL_RE = re.compile(r"^/people/[^/]+/?$", re.IGNORECASE)
DOUBAN_FOLLOWERS_LINK_RE = re.compile(
    r'<a\b[^>]*href=["\'][^"\']*/rev_contacts(?:[/?#][^"\']*)?["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
DOUBAN_PRIVACY_MARKER = "由于用户的设置，无法查看主页内容"


def is_peripheral_image_url(url: str) -> bool:
    """True for known non-content decorative images (e.g. douban CSS assets)."""
    return bool(PERIPHERAL_IMAGE_URL_RE.search(url or ""))


def is_douban_topic_url(url: str) -> bool:
    parsed = urlparse(url or "")
    return bool(parsed.hostname and parsed.hostname.endswith("douban.com") and DOUBAN_TOPIC_URL_RE.fullmatch(parsed.path))


def is_douban_people_url(url: str) -> bool:
    parsed = urlparse(url or "")
    return bool(parsed.hostname and parsed.hostname.endswith("douban.com") and DOUBAN_PEOPLE_URL_RE.fullmatch(parsed.path))


def extract_douban_topic_author_profile_url(rendered_html: str, topic_url: str) -> str | None:
    from_match = re.search(
        r'<span\b[^>]*class=["\'][^"\']*\bfrom\b[^"\']*["\'][^>]*>(.*?)</span>',
        rendered_html or "",
        re.IGNORECASE | re.DOTALL,
    )
    if not from_match:
        return None
    link_match = re.search(
        r'<a\b[^>]*href=["\']([^"\']*/people/[^"\']+)["\']',
        from_match.group(1),
        re.IGNORECASE | re.DOTALL,
    )
    if not link_match:
        return None
    profile_url = urljoin(topic_url, link_match.group(1).strip())
    parsed = urlparse(profile_url)
    if not is_douban_people_url(profile_url):
        return None
    return f"{parsed.scheme or 'https'}://{parsed.netloc}{parsed.path.rstrip('/')}/"


def parse_douban_follower_count(text: str) -> int | None:
    normalized = re.sub(r"\s+", "", text or "").replace(",", "")
    match = re.search(r"(?:被)?(?P<number>\d+(?:\.\d+)?)(?P<unit>万)?人关注", normalized)
    if not match:
        return None
    number = float(match.group("number"))
    value = number * 10_000 if match.group("unit") else number
    return int(value)


def extract_douban_people_followers(
    rendered_html: str,
    visible_text: str,
    *,
    profile_url: str,
) -> dict[str, Any]:
    link_match = DOUBAN_FOLLOWERS_LINK_RE.search(rendered_html or "")
    if link_match:
        raw_text = re.sub(r"<[^>]+>", " ", link_match.group(1))
        raw_text = re.sub(r"\s+", " ", raw_text).strip()
        followers_count = parse_douban_follower_count(raw_text)
        if followers_count is not None:
            return {
                "status": "observed",
                "followers_count": followers_count,
                "followers_observed": True,
                "followers_source": "people_page",
                "evidence": {
                    "profile_url": profile_url,
                    "selector": 'a[href*="/rev_contacts"]',
                    "raw_text": raw_text,
                },
            }

    if DOUBAN_PRIVACY_MARKER in (visible_text or ""):
        return {
            "status": "privacy_restricted",
            "followers_count": None,
            "followers_observed": False,
            "followers_source": "privacy_restricted",
            "evidence": {
                "profile_url": profile_url,
                "privacy_marker": DOUBAN_PRIVACY_MARKER,
            },
        }

    return {
        "status": "not_observed",
        "followers_count": None,
        "followers_observed": False,
        "followers_source": None,
        "evidence": {
            "profile_url": profile_url,
            "reason": "rev_contacts_followers_not_found",
        },
    }


def is_video_url(url: str) -> bool:
    return bool(VIDEO_URL_RE.search(url or ""))


def is_video_response(response: Response) -> bool:
    ctype = content_type_base(response.headers.get("content-type", ""))
    return response.request.resource_type == "media" or ctype.startswith(FORBIDDEN_MEDIA_CONTENT_TYPES) or is_video_url(response.url)


def is_text_response(response: Response) -> bool:
    ctype = content_type_base(response.headers.get("content-type", ""))
    return any(ctype.startswith(prefix) for prefix in TEXT_CONTENT_PREFIXES)


def image_extension(response: Response, url: str) -> str:
    ctype = content_type_base(response.headers.get("content-type", ""))
    if ctype in IMAGE_EXTENSIONS:
        return IMAGE_EXTENSIONS[ctype]
    path_suffix = Path(urlparse(url).path).suffix.lower()
    return path_suffix if path_suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif"} else ".img"


async def open_context(playwright, target: dict[str, Any], args: argparse.Namespace) -> BrowserContext:
    profile_dir = profile_dir_for_target(target)
    ensure_dir(profile_dir)
    engine = target_engine(target)
    executable_path = discover_chrome_for_testing_path()
    if engine == "patchright" and executable_path is None:
        raise RuntimeError(
            "Patchright targets require a shared Chrome for Testing executable. "
            "Install the Playwright Chromium browser or set TRIPPOSTCOLLECT_CUSTOM_BROWSER_PATH."
        )
    mobile = bool(target.get("mobile"))
    viewport = (
        {"width": RANDOM.randint(375, 430), "height": RANDOM.randint(780, 920)}
        if mobile
        else {"width": RANDOM.randint(1280, 1512), "height": RANDOM.randint(820, 980)}
    )
    return await playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=args.headless,
        executable_path=executable_path,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport=viewport,
        screen=viewport,
        user_agent=MOBILE_UA if mobile else DESKTOP_UA,
        device_scale_factor=3 if mobile else RANDOM.choice((1, 2)),
        is_mobile=mobile,
        has_touch=mobile,
        java_script_enabled=True,
        service_workers="allow",
        ignore_https_errors=True,
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            *browser_runtime_args(),
        ],
        env=browser_launch_environment(),
        ignore_default_args=["--enable-automation"],
    )


async def install_runtime_overrides(context: BrowserContext) -> None:
    await context.add_init_script(RUNTIME_OVERRIDE_SCRIPT)


async def install_page_runtime_overrides(page: Page) -> None:
    await page.add_init_script(RUNTIME_OVERRIDE_SCRIPT)


async def varied_settle(page: Page, args: argparse.Namespace) -> None:
    wait_ms = RANDOM.randint(min(args.settle_min_ms, args.settle_max_ms), max(args.settle_min_ms, args.settle_max_ms))
    await page.wait_for_timeout(wait_ms)
    width = await page.evaluate("() => window.innerWidth")
    height = await page.evaluate("() => window.innerHeight")
    for _ in range(RANDOM.randint(2, 5)):
        await page.mouse.move(RANDOM.randint(20, max(30, width - 20)), RANDOM.randint(20, max(30, height - 20)))
        await page.wait_for_timeout(int(varied_wait_seconds(0.35, ratio=0.7, floor_seconds=0.08) * 1000))


async def varied_scroll(page: Page, args: argparse.Namespace) -> None:
    for index in range(RANDOM.randint(2, max(2, args.max_scrolls))):
        direction = -1 if index > 0 and RANDOM.random() < 0.15 else 1
        await page.mouse.wheel(0, direction * RANDOM.randint(420, 1550))
        await page.wait_for_timeout(int(varied_wait_seconds(0.9, ratio=0.8, floor_seconds=0.18, ceiling_seconds=2.6) * 1000))


async def install_no_video_policy(context: BrowserContext, media_policy: dict[str, Any]) -> None:
    async def route_handler(route) -> None:
        request = route.request
        if request.resource_type == "media" or is_video_url(request.url):
            media_policy["blocked_media_requests"] += 1
            if len(media_policy["blocked_media_urls"]) < 40:
                media_policy["blocked_media_urls"].append({"url": request.url, "resource_type": request.resource_type})
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", route_handler)


def blank_artifacts(target_dir: Path) -> dict[str, str]:
    artifacts = {
        "rendered_html": str(target_dir / "rendered.html"),
        "visible_text": str(target_dir / "visible_text.txt"),
        "images_json": str(target_dir / "images.json"),
        "failed_images_json": str(target_dir / "failed_images.json"),
        "screenshot": str(target_dir / "screen.png"),
    }
    (target_dir / "rendered.html").write_text("", encoding="utf-8")
    (target_dir / "visible_text.txt").write_text("", encoding="utf-8")
    (target_dir / "images.json").write_text("[]", encoding="utf-8")
    (target_dir / "failed_images.json").write_text("[]", encoding="utf-8")
    return artifacts


async def crawl_one(playwright, target: dict[str, Any], batch_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    site_key = str(target["site"])
    url = str(target["url"])
    target_dir = batch_dir / f"{site_key}_{slug(url)}"
    image_dir = target_dir / "images"
    ensure_dir(image_dir)
    site_policy = get_site(site_key) if target.get("configured") else None

    image_records: list[dict[str, Any]] = []
    failed_image_records: list[dict[str, Any]] = []
    text_flags: list[dict[str, Any]] = []
    policy_events: list[dict[str, Any]] = []
    cookie_events: list[dict[str, Any]] = []
    media_policy: dict[str, Any] = {
        "video_enabled": False,
        "blocked_media_requests": 0,
        "blocked_media_urls": [],
        "video_responses": [],
        "skipped_video_targets": 0,
    }
    behavior_profile_key = args.behavior_profile or site_key
    behavior_profile = load_behavior_profile(
        behavior_profile_key,
        strict=bool(args.behavior_profile or target.get("configured")),
    )
    behavior_events: list[dict[str, Any]] = []
    pending_tasks: set[asyncio.Task] = set()
    image_save_lock = asyncio.Lock()
    saved_images = 0

    douyin_target = is_douyin_target(site_key, url)
    profile_dir = profile_dir_for_target(target)
    engine = target_engine(target)

    if is_video_url(url):
        media_policy["skipped_video_targets"] = 1
        artifacts = blank_artifacts(target_dir)
        skipped_summary = {
            "site": site_key,
            "url": url,
            "ok": True,
            "skipped": True,
            "skip_reason": "video_target_ignored",
            "blocked_by_policy": None,
            "nav_error": "",
            "navigation": {"strategy": "skipped_before_navigation", "video_target_ignored": True},
            "published_at": None,
            "published_at_evidence": {"skipped": "video_target_ignored", "candidates": []},
            "policy_events": policy_events,
            "cookie_events": cookie_events,
            "scrapling_preflight": {"enabled": False, "skipped": "video_target_ignored"},
            "behavior_profile": behavior_profile.name,
            "behavior_events": behavior_events,
            "browser_engine": engine,
            "media_policy": media_policy,
            "image_summary": {
                "total_requests": 0,
                "successful_responses": 0,
                "http_failed_responses": 0,
                "request_failed": 0,
                "saved_images": 0,
                "image_dir": str(image_dir),
            },
            "flags": [],
            "artifact_dir": str(target_dir),
            "artifacts": artifacts,
            "keyword": str(args.keyword or ""),
        }
        (target_dir / "capture_meta.json").write_text(json.dumps(skipped_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return skipped_summary

    scrapling_preflight: dict[str, Any] = {"enabled": False}
    if args.scrapling_preflight != "off" and should_preflight(site_key, url):
        try:
            with site_request_guard(site_policy, label="ctf-resource-crawl:scrapling-preflight", disabled=args.no_throttle) as event:
                policy_events.append(event)
                scrapling_preflight = await asyncio.to_thread(
                    run_scrapling_static_preflight,
                    url=url,
                    site_key=site_key,
                    output_dir=target_dir,
                    timeout_seconds=args.scrapling_preflight_timeout,
                    artifact_prefix="scrapling_preflight",
                )
                for flag in scrapling_preflight.get("flags", []):
                    text_flags.append({"url": scrapling_preflight.get("final_url") or url, "status": scrapling_preflight.get("status"), "flags": [flag]})
        except CrawlPolicyBlocked as exc:
            artifacts = blank_artifacts(target_dir)
            blocked_summary = {
                "site": site_key,
                "url": url,
                "ok": False,
                "blocked_by_policy": exc.event,
                "nav_error": "",
                "navigation": {"strategy": "scrapling_static_preflight", "blocked_by_policy": True},
                "published_at": None,
                "published_at_evidence": {"skipped": "blocked_by_policy", "candidates": []},
                "policy_events": policy_events,
                "cookie_events": cookie_events,
                "scrapling_preflight": {"enabled": True, "blocked_by_policy": True},
                "behavior_profile": behavior_profile.name,
                "behavior_events": behavior_events,
                "browser_engine": engine,
                "media_policy": media_policy,
                "image_summary": {
                    "total_requests": 0,
                    "successful_responses": 0,
                    "http_failed_responses": 0,
                    "request_failed": 0,
                    "saved_images": 0,
                    "image_dir": str(image_dir),
                },
                "flags": [],
                "artifact_dir": str(target_dir),
                "artifacts": artifacts,
                "keyword": str(args.keyword or ""),
            }
            (target_dir / "capture_meta.json").write_text(json.dumps(blocked_summary, ensure_ascii=False, indent=2), encoding="utf-8")
            return blocked_summary

    if douyin_target and args.douyin_cookie_cleanup != "off":
        cookie_events.append(clean_douyin_profile_cookies(profile_dir))

    context = await open_context(playwright, target, args)
    if douyin_target and args.douyin_cookie_cleanup != "off":
        cookie_events.append(await clear_douyin_context_cookies(context))
    await install_runtime_overrides(context)
    await install_runtime_hints(context)
    await install_no_video_policy(context, media_policy)

    def track_task(coro) -> None:
        task = asyncio.create_task(coro)
        pending_tasks.add(task)
        task.add_done_callback(pending_tasks.discard)

    async def record_request_failed(request: Request) -> None:
        if request.resource_type == "image":
            failure = request.failure or ""
            failed_image_records.append({"url": request.url, "resource_type": request.resource_type, "failure": failure})

    async def record_response(response: Response) -> None:
        nonlocal saved_images
        try:
            if is_video_response(response):
                if len(media_policy["video_responses"]) < 40:
                    media_policy["video_responses"].append(
                        {
                            "url": response.url,
                            "status": response.status,
                            "content_type": response.headers.get("content-type", ""),
                            "resource_type": response.request.resource_type,
                        }
                    )
                return
            if is_image_response(response):
                if is_peripheral_image_url(response.url):
                    return
                record: dict[str, Any] = {
                    "url": response.url,
                    "status": response.status,
                    "ok": response.ok,
                    "content_type": response.headers.get("content-type", ""),
                    "resource_type": response.request.resource_type,
                    "saved_path": "",
                    "bytes": None,
                }
                if response.ok:
                    async with image_save_lock:
                        if saved_images < args.max_image_save:
                            body = await response.body()
                            digest = hashlib.sha1(response.url.encode("utf-8")).hexdigest()[:12]
                            out_path = image_dir / f"image_{saved_images + 1:03d}_{digest}{image_extension(response, response.url)}"
                            out_path.write_bytes(body)
                            record["saved_path"] = str(out_path)
                            record["bytes"] = len(body)
                            saved_images += 1
                image_records.append(record)
                return
            if is_text_response(response):
                body = await response.body()
                text = body[:750_000].decode("utf-8", errors="replace")
                flags = extract_flags_from_text(text)
                if flags:
                    text_flags.append({"url": response.url, "status": response.status, "flags": flags})
        except Exception as exc:
            if is_image_response(response):
                image_records.append({"url": response.url, "status": response.status, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    context.on("requestfailed", lambda request: track_task(record_request_failed(request)))
    context.on("response", lambda response: track_task(record_response(response)))
    page = context.pages[0] if context.pages else await context.new_page()
    await install_page_runtime_overrides(page)

    blocked_by_policy = None
    nav_error = ""
    navigation: dict[str, Any] = {}
    try:
        with site_request_guard(site_policy, label="ctf-resource-crawl:page", disabled=args.no_throttle) as event:
            policy_events.append(event)
            if douyin_target:
                navigation = await navigate_with_commit_and_readiness(
                    page,
                    url,
                    commit_timeout_ms=min(args.timeout, args.commit_timeout),
                    readiness_timeout_ms=args.readiness_timeout,
                )
                if navigation.get("fatal"):
                    nav_error = "content_not_ready_after_commit_navigation"
            else:
                await page.goto(url, wait_until="domcontentloaded", timeout=args.timeout)
                navigation = {
                    "strategy": "domcontentloaded",
                    "commit_ok": True,
                    "content_ready": True,
                    "final_url": page.url,
                }
        if not nav_error:
            await dwell_on_detail(
                page,
                behavior_profile,
                content_hint=navigation,
                max_scroll_passes=max(1, args.max_scrolls),
                log=behavior_events,
            )
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        if pending_tasks:
            await asyncio.wait(pending_tasks, timeout=10)
    except CrawlPolicyBlocked as exc:
        blocked_by_policy = exc.event
    except Exception as exc:
        nav_error = f"{type(exc).__name__}: {exc}"

    rendered_html = ""
    visible_text = ""
    flags: list[str] = []
    artifact_errors: list[str] = []
    published_at_evidence: dict[str, Any] = {"published_at": None, "candidates": []}
    screenshot_path = target_dir / "screen.png"
    if not blocked_by_policy:
        try:
            rendered_html = await page.content()
            visible_text = await page.locator("body").inner_text(timeout=10_000)
            flags = extract_flags_from_text(rendered_html + "\n" + visible_text)
            published_at_evidence = await extract_published_at(page)
        except Exception as exc:
            nav_error = nav_error or f"{type(exc).__name__}: {exc}"
        try:
            await page.screenshot(path=str(screenshot_path), full_page=False, timeout=10_000)
        except Exception as exc:
            artifact_errors.append(f"screenshot: {type(exc).__name__}: {exc}")
    if pending_tasks:
        await asyncio.wait(pending_tasks, timeout=10)
    await context.close()
    if douyin_target and args.douyin_cookie_cleanup != "off":
        cookie_events.append(clean_douyin_profile_cookies(profile_dir))

    total_image_requests = len(image_records) + len(failed_image_records)
    image_success = sum(1 for item in image_records if item.get("ok"))
    image_http_fail = sum(1 for item in image_records if not item.get("ok"))
    summary = {
        "site": site_key,
        "url": url,
        "capture_role": str(target.get("capture_role") or "primary"),
        "parent_url": str(target.get("parent_url") or ""),
        "ok": not blocked_by_policy and not nav_error,
        "blocked_by_policy": blocked_by_policy,
        "nav_error": nav_error,
        "navigation": navigation,
        "published_at": published_at_evidence.get("published_at"),
        "published_at_evidence": published_at_evidence,
        "behavior_profile": behavior_profile.name,
        "behavior_events": behavior_events,
        "browser_engine": engine,
        "media_policy": media_policy,
        "policy_events": policy_events,
        "cookie_events": cookie_events,
        "scrapling_preflight": scrapling_preflight,
        "keyword": str(args.keyword or ""),
        "artifact_errors": artifact_errors,
        "image_summary": {
            "total_requests": total_image_requests,
            "successful_responses": image_success,
            "http_failed_responses": image_http_fail,
            "request_failed": len(failed_image_records),
            "saved_images": saved_images,
            "image_dir": str(image_dir),
        },
        "flags": sorted(set(flags + [flag for item in text_flags for flag in item.get("flags", [])])),
        "artifact_dir": str(target_dir),
        "artifacts": {
            "rendered_html": str(target_dir / "rendered.html"),
            "visible_text": str(target_dir / "visible_text.txt"),
            "images_json": str(target_dir / "images.json"),
            "failed_images_json": str(target_dir / "failed_images.json"),
            "screenshot": str(screenshot_path),
        },
    }
    if site_key == "douban_group" and is_douban_people_url(url):
        followers_evidence = extract_douban_people_followers(
            rendered_html,
            visible_text,
            profile_url=url,
        )
        followers_evidence["artifact_dir"] = str(target_dir)
        followers_evidence["capture_meta_path"] = str(target_dir / "capture_meta.json")
        followers_evidence["artifacts"] = {
            "rendered_html": str(target_dir / "rendered.html"),
            "visible_text": str(target_dir / "visible_text.txt"),
            "screenshot": str(screenshot_path),
        }
        summary["douban_people_followers"] = followers_evidence
    (target_dir / "rendered.html").write_text(rendered_html, encoding="utf-8")
    (target_dir / "visible_text.txt").write_text(visible_text, encoding="utf-8")
    (target_dir / "images.json").write_text(json.dumps(image_records, ensure_ascii=False, indent=2), encoding="utf-8")
    (target_dir / "failed_images.json").write_text(json.dumps(failed_image_records, ensure_ascii=False, indent=2), encoding="utf-8")
    (target_dir / "capture_meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def write_capture_meta(summary: dict[str, Any]) -> None:
    artifact_dir = Path(str(summary["artifact_dir"]))
    (artifact_dir / "capture_meta.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def run_douban_conditional_enrichment(
    playwright,
    target: dict[str, Any],
    primary: dict[str, Any],
    batch_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    if str(target.get("site") or "") != "douban_group" or not is_douban_topic_url(str(target.get("url") or "")):
        return None

    existing = primary.get("conditional_enrichment") or (primary.get("navigation") or {}).get("enrichment") or {}
    if existing.get("followers_observed") is True and existing.get("followers_count") is not None:
        return None

    rendered_html_path = Path(str((primary.get("artifacts") or {}).get("rendered_html") or ""))
    rendered_html = (
        rendered_html_path.read_text(encoding="utf-8", errors="replace")
        if rendered_html_path.is_file()
        else ""
    )
    profile_url = extract_douban_topic_author_profile_url(rendered_html, str(target["url"]))
    enrichment: dict[str, Any] = {
        "kind": "douban_author_followers",
        "triggered": False,
        "status": "not_applicable",
        "followers_count": None,
        "followers_observed": False,
        "followers_source": None,
        "author_profile_url": profile_url,
        "evidence": {},
    }
    if not primary.get("ok"):
        enrichment["status"] = "primary_capture_failed"
        enrichment["evidence"] = {"reason": "primary_capture_not_ok"}
    elif not profile_url:
        enrichment["status"] = "not_observed"
        enrichment["evidence"] = {"reason": "visible_author_people_url_not_found"}
    else:
        enrichment["triggered"] = True
        enrichment_target = {
            **target,
            "url": profile_url,
            "explicit_url": True,
            "capture_role": "conditional_enrichment",
            "parent_url": str(target["url"]),
        }
        profile_capture = await crawl_one(playwright, enrichment_target, batch_dir, args)
        followers = dict(profile_capture.get("douban_people_followers") or {})
        if profile_capture.get("ok"):
            enrichment.update(followers)
        else:
            enrichment.update(
                {
                    "status": "capture_failed",
                    "followers_count": None,
                    "followers_observed": False,
                    "followers_source": None,
                    "evidence": {
                        "reason": str(profile_capture.get("nav_error") or "people_page_capture_failed"),
                    },
                }
            )
        enrichment.update(
            {
                "kind": "douban_author_followers",
                "triggered": True,
                "author_profile_url": profile_url,
                "profile_capture_ok": bool(profile_capture.get("ok")),
                "capture_meta_path": str(Path(str(profile_capture["artifact_dir"])) / "capture_meta.json"),
                "artifact_dir": str(profile_capture["artifact_dir"]),
            }
        )
        profile_capture["conditional_enrichment_for"] = {
            "parent_url": str(target["url"]),
            "parent_artifact_dir": str(primary["artifact_dir"]),
        }
        write_capture_meta(profile_capture)

        primary["conditional_enrichment"] = enrichment
        primary.setdefault("navigation", {})["enrichment"] = enrichment
        write_capture_meta(primary)
        return profile_capture

    primary["conditional_enrichment"] = enrichment
    primary.setdefault("navigation", {})["enrichment"] = enrichment
    write_capture_meta(primary)
    return None


def aggregate(records: list[dict[str, Any]], batch_dir: Path) -> dict[str, Any]:
    image_totals = {
        "total_requests": sum(record["image_summary"]["total_requests"] for record in records),
        "successful_responses": sum(record["image_summary"]["successful_responses"] for record in records),
        "http_failed_responses": sum(record["image_summary"]["http_failed_responses"] for record in records),
        "request_failed": sum(record["image_summary"]["request_failed"] for record in records),
        "saved_images": sum(record["image_summary"]["saved_images"] for record in records),
    }
    media_policy = {
        "video_enabled": False,
        "blocked_media_requests": sum(int((record.get("media_policy") or {}).get("blocked_media_requests") or 0) for record in records),
        "video_responses": sum(len((record.get("media_policy") or {}).get("video_responses") or []) for record in records),
        "skipped_video_targets": sum(int((record.get("media_policy") or {}).get("skipped_video_targets") or 0) for record in records),
    }
    skipped_count = sum(1 for record in records if record.get("skipped"))
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_count": len(records),
        "ok_count": sum(1 for record in records if record["ok"] and not record.get("skipped")),
        "skipped_count": skipped_count,
        "failed_count": sum(1 for record in records if not record["ok"] and not record.get("skipped")),
        "image_totals": image_totals,
        "media_policy": media_policy,
        "flags": sorted(set(flag for record in records for flag in record.get("flags", []))),
        "batch_dir": str(batch_dir),
        "records": records,
    }


async def main_async() -> int:
    args = parse_args()
    targets = active_targets(args)
    batch_dir = ensure_dir(Path(args.output_dir).expanduser() / utc_stamp())

    from playwright.async_api import async_playwright

    records: list[dict[str, Any]] = []
    conditional_enrichments: list[dict[str, Any]] = []
    engines = {target_engine(target) for target in targets}
    async with AsyncExitStack() as stack:
        drivers = {"playwright": await stack.enter_async_context(async_playwright())}
        if "patchright" in engines:
            from patchright.async_api import async_playwright as async_patchright

            drivers["patchright"] = await stack.enter_async_context(async_patchright())
        for target in targets:
            print(f"[crawl] {target['site']} {target['url']}", flush=True)
            driver = drivers[target_engine(target)]
            primary = await crawl_one(driver, target, batch_dir, args)
            records.append(primary)
            enrichment_capture = await run_douban_conditional_enrichment(
                driver,
                target,
                primary,
                batch_dir,
                args,
            )
            if enrichment_capture is not None:
                conditional_enrichments.append(enrichment_capture)
            await asyncio.sleep(varied_wait_seconds(2.0, ratio=0.8, floor_seconds=0.5, ceiling_seconds=5.0))

    summary = aggregate(records, batch_dir)
    summary["conditional_enrichments"] = conditional_enrichments
    summary["conditional_enrichment_count"] = len(conditional_enrichments)
    summary["conditional_enrichment_failed_count"] = sum(
        1 for record in conditional_enrichments if not record.get("ok")
    )
    summary["failed_count"] += summary["conditional_enrichment_failed_count"]
    summary_path = batch_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Summary: {summary_path}")
    return 0 if summary["failed_count"] == 0 else 1


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
