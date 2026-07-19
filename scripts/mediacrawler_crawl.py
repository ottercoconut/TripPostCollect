#!/usr/bin/env python3
"""MediaCrawler-first structured crawl entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import signal
import shlex
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import md5
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from playwright.async_api import async_playwright

from crawl_policy import CrawlPolicyBlocked, record_site_cooldown, site_request_guard
from execution_state import FrozenExecutionState
from failure_classifier import classify_attempt
from human_flow import install_runtime_hints
from mediacrawler_behavior import (
    HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS,
    behavior_evidence_valid,
    run_page_behavior,
)
from trippostcollect.core.paths import (
    DEFAULT_DB,
    MEDIACRAWLER_DIR,
    MEDIACRAWLER_RUNS_OUTPUT,
    PROJECT_ROOT,
    UV_CACHE_ROOT,
    ensure_dir,
    ensure_parent,
)
from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.platforms.registry import get_site
from trippostcollect.scheduler.discovery import (
    load_seen_candidates,
    save_checkpoint,
    save_seen_candidates,
)
from browser_runtime import browser_launch_environment, browser_runtime_args


ROOT = PROJECT_ROOT
DEFAULT_OUTPUT = MEDIACRAWLER_RUNS_OUTPUT
COOKIE_SNAPSHOT_FILENAME = "trippostcollect_cookie_snapshot.json"
BILIBILI_ARTICLE_SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
BILIBILI_RELATION_STAT_URL = "https://api.bilibili.com/x/relation/stat"
BILIBILI_ARTICLE_PAGE_SIZE = 20
BILIBILI_WBI_MIXIN_TABLE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)

PLATFORMS: dict[str, dict[str, str]] = {
    "bilibili": {"mediacrawler": "bili", "label": "B站"},
    "xhs": {"mediacrawler": "xhs", "label": "小红书"},
    "weibo": {"mediacrawler": "wb", "label": "微博"},
    "douyin": {"mediacrawler": "dy", "label": "抖音"},
    "zhihu": {"mediacrawler": "zhihu", "label": "知乎"},
}
FOLLOWERS_REQUIRED_PLATFORMS = frozenset(PLATFORMS)
REQUIRED_FOLLOWER_SOURCES = {
    "bilibili": frozenset({"relation_stat"}),
    "weibo": frozenset({"search_author"}),
    "xhs": frozenset({"creator_profile"}),
    "douyin": frozenset({"creator_profile"}),
    "zhihu": frozenset({"search_author"}),
}
PLATFORM_REQUIRED_METRICS: dict[str, tuple[tuple[str, ...], ...]] = {
    "bilibili": (("liked_count",), ("comment_count", "comments_count"), ("view_count", "video_play_count")),
    "weibo": (("liked_count",), ("comments_count", "comment_count"), ("shared_count", "reposts_count")),
    "xhs": (("liked_count",), ("collected_count",), ("comment_count",), ("share_count",)),
    "douyin": (("liked_count",), ("collected_count",), ("comment_count",), ("share_count",)),
    "zhihu": (("voteup_count", "liked_count"), ("comment_count", "comments_count")),
}
SHANDONG_CITY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("济南市", ("济南市", "济南", "泉城")),
    ("青岛市", ("青岛市", "青岛")),
    ("淄博市", ("淄博市", "淄博")),
    ("枣庄市", ("枣庄市", "枣庄")),
    ("东营市", ("东营市", "东营")),
    ("烟台市", ("烟台市", "烟台")),
    ("潍坊市", ("潍坊市", "潍坊")),
    ("济宁市", ("济宁市", "济宁")),
    ("泰安市", ("泰安市", "泰安")),
    ("威海市", ("威海市", "威海")),
    ("日照市", ("日照市", "日照")),
    ("临沂市", ("临沂市", "临沂")),
    ("德州市", ("德州市", "德州")),
    ("聊城市", ("聊城市", "聊城")),
    ("滨州市", ("滨州市", "滨州")),
    ("菏泽市", ("菏泽市", "菏泽")),
)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg", ".img"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
AUTHOR_FIELD_MARKERS = ("author", "user", "nickname", "avatar", "fans", "follower", "follow", "up")
IMAGE_URL_KEYS = ("cover", "image", "img", "pic", "avatar", "note_download")
URL_RE = re.compile(r"https?://[^\s\"'<>,，]+", re.I)
VIDEO_URL_RE = re.compile(r"(?i)(?:/(?:video|share/video)/\d+|\.(?:mp4|m4v|mov|webm|flv|m3u8|mpd)(?:[?#]|$))")
CHINA_TZ = timezone(timedelta(hours=8))
TIMESTAMP_MIN = 946_684_800
TIMESTAMP_MAX = 4_102_444_800
DATETIME_TEXT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
)
PUBLISHED_AT_KEYS = (
    "published_at",
    "date_published",
    "datePublished",
    "publish_at",
    "publish_time",
    "publish_date",
    "pub_time",
    "pubdate",
    "created_at",
    "created_time",
    "create_date_time",
    "create_time",
    "ctime",
    "timestamp",
    "time",
)
NESTED_PUBLISHED_AT_KEYS = tuple(key for key in PUBLISHED_AT_KEYS if key not in {"time", "timestamp"})
SAMPLE_KEYS = (
    "title",
    "desc",
    "content",
    "published_at",
    "create_time",
    "publish_time",
    "time",
    "create_date_time",
    "note_id",
    "aweme_id",
    "content_id",
    "content_type",
    "video_id",
    "bvid",
    "aid",
    "note_url",
    "video_url",
    "content_url",
    "created_time",
    "updated_time",
    "user_id",
    "nickname",
    "user_nickname",
    "fans",
    "fans_count",
    "followers_count",
    "following_count",
    "aweme_count",
    "author_liked_count",
    "author_followers_source",
    "liked_count",
    "voteup_count",
    "collected_count",
    "comment_count",
    "comments_count",
    "share_count",
    "shared_count",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run MediaCrawler for supported structured social platforms.")
    parser.add_argument("--keyword", default="济南旅游", help="Search keyword.")
    parser.add_argument(
        "--platforms",
        nargs="+",
        default=["weibo", "douyin"],
        help="bilibili weibo douyin zhihu; XHS is a low-level target selected only by xhs_runner.py",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT), help="Output root.")
    parser.add_argument("--timeout-per-platform", type=int, default=180, help="Timeout per MediaCrawler platform.")
    parser.add_argument("--login-type", default="cookie", choices=("cookie", "qrcode", "phone"), help="MediaCrawler login type.")
    parser.add_argument("--get-media", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--download-images", action="store_true", help="Download image files for supported image-only platforms. Videos remain disabled.")
    parser.add_argument("--headed", action="store_true", help="Run browser with visible UI.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path for web_posts import.")
    parser.add_argument(
        "--target-new-posts",
        type=int,
        default=0,
        help="Required field-valid records not already present in SQLite.",
    )
    parser.add_argument("--candidate-hard-limit", type=int, required=True, help="Maximum actual content candidates processed for validation.")
    parser.add_argument("--required-fields-profile", default="image_post_with_followers_v1")
    parser.add_argument("--behavior-profile", default="social_high_risk")
    parser.add_argument("--xhs-account-id", help="Required isolated account id for the XHS low-level executor.")
    parser.add_argument("--xhs-profile-dir", help="Required isolated persistent profile for XHS.")
    parser.add_argument("--xhs-storage-state", help="Required per-run decrypted XHS storage state.")
    parser.add_argument("--xhs-discovery-target-key", help=argparse.SUPPRESS)
    parser.add_argument("--xhs-discovery-query-fingerprint", help=argparse.SUPPRESS)
    parser.add_argument(
        "--xhs-post-interaction",
        choices=("none", "comment-scroll", "like-one", "random"),
        default="none",
        help="Optional one-post visible XHS interaction selected by xhs_runner.py.",
    )
    parser.add_argument("--max-stagnant-batches", type=int, default=3)
    parser.add_argument("--start-page", type=int, default=1, help="Recovery-only first platform page.")
    parser.add_argument("--start-offset", type=int, default=0, help="Saved platform offset for the discovery frontier.")
    parser.add_argument("--start-cursor", default="", help="Saved opaque platform cursor for the discovery frontier.")
    parser.add_argument("--resume-summary", help="Recovery-only prior summary whose JSONL records join this run.")
    parser.add_argument("--discovery-job-id", type=int)
    parser.add_argument("--discovery-query-fingerprint")
    parser.add_argument("--discovery-run-id")
    parser.add_argument("--top-refresh-max-pages", type=int, default=0)
    parser.add_argument("--discovery-source-exhausted", action="store_true")
    parser.add_argument("--no-checkpoint-write", action="store_true")
    parser.add_argument("--no-import", action="store_true", help="Do not import MediaCrawler JSONL records into SQLite.")
    parser.add_argument(
        "--zhihu-detail-urls-file",
        help="Diagnostic-only JSON array of Zhihu answer/article URLs to inspect via detail mode.",
    )
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f%z")


def selected_platforms(values: list[str]) -> list[str]:
    unknown = sorted(set(values) - set(PLATFORMS))
    if unknown:
        raise SystemExit(f"Unsupported MediaCrawler platform in this project: {', '.join(unknown)}")
    return values


def load_zhihu_detail_urls(path_value: str | Path) -> list[str]:
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid Zhihu detail URL file: {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit("Zhihu detail URL file must contain a JSON array")

    urls: list[str] = []
    for value in payload:
        url = str(value or "").strip().split("#", 1)[0].split("?", 1)[0]
        parsed = urlparse(url)
        answer_url = bool(
            parsed.hostname in {"zhihu.com", "www.zhihu.com"}
            and re.fullmatch(r"/question/[^/]+/answer/[^/]+/?", parsed.path)
        )
        article_url = bool(
            parsed.hostname == "zhuanlan.zhihu.com"
            and re.fullmatch(r"/p/[^/]+/?", parsed.path)
        )
        if not (parsed.scheme == "https" and (answer_url or article_url)):
            raise SystemExit(f"unsupported Zhihu detail URL: {url or value!r}")
        if url not in urls:
            urls.append(url)
    if not urls:
        raise SystemExit("Zhihu detail URL file contains no answer/article URLs")
    return urls


def ensure_prerequisites() -> None:
    if not (MEDIACRAWLER_DIR / "pyproject.toml").exists():
        raise SystemExit(f"MediaCrawler is missing or incomplete: {MEDIACRAWLER_DIR}")


def discover_cdp_browser_path() -> str | None:
    for env_key in ("TRIPPOSTCOLLECT_CUSTOM_BROWSER_PATH", "CUSTOM_BROWSER_PATH"):
        value = os.environ.get(env_key)
        if value and Path(value).is_file():
            return value

    playwright_cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    cache_candidates = sorted(
        playwright_cache.glob(
            "chromium-*/chrome-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        ),
        key=lambda path: int(match.group(1)) if (match := re.search(r"chromium-(\d+)", str(path))) else -1,
        reverse=True,
    )
    for path in cache_candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)

    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"),
        Path("/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev"),
        Path("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/Applications/Microsoft Edge Beta.app/Contents/MacOS/Microsoft Edge Beta"),
        Path("/Applications/Microsoft Edge Dev.app/Contents/MacOS/Microsoft Edge Dev"),
        Path("/Applications/Microsoft Edge Canary.app/Contents/MacOS/Microsoft Edge Canary"),
    ]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def profile_dir_for(platform_key: str) -> Path:
    code = PLATFORMS[platform_key]["mediacrawler"]
    return MEDIACRAWLER_DIR / "browser_data" / f"{code}_user_data_dir"


def cookie_snapshot_path(platform_key: str) -> Path:
    return profile_dir_for(platform_key) / COOKIE_SNAPSHOT_FILENAME


def platform_cookie_url(platform_key: str) -> str:
    return {
        "bilibili": "https://www.bilibili.com/",
        "weibo": "https://m.weibo.cn/",
        "xhs": "https://www.xiaohongshu.com/",
        "douyin": "https://www.douyin.com/",
        "zhihu": "https://www.zhihu.com/",
    }[platform_key]


def required_cookie_names(platform_key: str) -> tuple[str, ...]:
    if platform_key == "zhihu":
        return ("d_c0", "z_c0")
    return ()


def xhs_storage_snapshot_info(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "ok": False,
        "cookie_names": [],
        "origin_count": 0,
        "runtime_storage_count": 0,
        "saved_at": None,
        "reason": "",
    }
    if not path.is_file():
        info["reason"] = "missing_snapshot"
        return info
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        info["reason"] = f"read_failed:{type(exc).__name__}"
        info["error"] = str(exc)
        return info
    cookies = state.get("cookies") if isinstance(state, dict) else []
    origins = state.get("origins") if isinstance(state, dict) else []
    marker = state.get("trippostcollect") if isinstance(state, dict) else {}
    cookie_names = sorted({item.get("name", "") for item in cookies if isinstance(item, dict) and item.get("name")})
    origin_count = len(origins) if isinstance(origins, list) else 0
    runtime_storage = marker.get("runtime_storage") if isinstance(marker, dict) else []
    info.update(
        {
            "ok": bool(cookie_names or origin_count),
            "cookie_names": cookie_names,
            "origin_count": origin_count,
            "runtime_storage_count": len(runtime_storage) if isinstance(runtime_storage, list) else 0,
            "saved_at": marker.get("saved_at") or marker.get("captured_at") if isinstance(marker, dict) else None,
            "reason": "ready" if cookie_names or origin_count else "empty_snapshot",
        }
    )
    return info


def cookie_names_from_header(cookie_header: str) -> list[str]:
    names = []
    for item in cookie_header.split(";"):
        if "=" not in item:
            continue
        name = item.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return sorted(set(names))


def cookies_to_header(cookies: list[dict[str, Any]]) -> str:
    pairs = []
    now = time.time()
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if not name or value in (None, ""):
            continue
        expires = item.get("expires")
        if isinstance(expires, (int, float)) and expires > 0 and expires < now:
            continue
        pairs.append(f"{name}={value}")
    return ";".join(pairs)


def load_cookie_snapshot(platform_key: str) -> dict[str, Any] | None:
    path = cookie_snapshot_path(platform_key)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        return None
    cookie_header = cookies_to_header(cookies)
    names = cookie_names_from_header(cookie_header)
    missing = [name for name in required_cookie_names(platform_key) if name not in names]
    if missing:
        return None
    return {
        "cookie_header": cookie_header,
        "source": "snapshot",
        "snapshot_path": str(path),
        "saved_at": payload.get("saved_at"),
        "cookie_names": names,
        "required_cookie_names": list(required_cookie_names(platform_key)),
    }


def public_cookie_export(cookie_export: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in cookie_export.items() if key != "cookie_header"}


def export_profile_cookies(platform_key: str, browser_path: str | None) -> dict[str, Any] | None:
    snapshot = load_cookie_snapshot(platform_key)
    if snapshot:
        return snapshot

    profile_dir = profile_dir_for(platform_key)
    if not profile_dir.exists():
        return None
    script = r"""
import asyncio
import json
import sys
from playwright.async_api import async_playwright

async def main() -> int:
    profile_dir = sys.argv[1]
    executable_path = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    target_url = sys.argv[3]
    runtime_args = json.loads(sys.argv[4])
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            executable_path=executable_path,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-dev-shm-usage", "--no-sandbox", *runtime_args],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        cookies = await context.cookies([target_url])
        await context.close()
        sys.stdout.write(";".join(f"{item['name']}={item.get('value', '')}" for item in cookies))
    return 0

raise SystemExit(asyncio.run(main()))
"""
    cmd = [sys.executable, "-c", script, str(profile_dir)]
    if browser_path:
        cmd.append(browser_path)
    else:
        cmd.append("")
    cmd.append(platform_cookie_url(platform_key))
    cmd.append(json.dumps(browser_runtime_args()))
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
            env=browser_launch_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    cookie_str = result.stdout.strip()
    names = cookie_names_from_header(cookie_str)
    missing = [name for name in required_cookie_names(platform_key) if name not in names]
    if result.returncode != 0 or missing:
        return None
    return {
        "cookie_header": cookie_str,
        "source": "live_profile",
        "snapshot_path": str(cookie_snapshot_path(platform_key)),
        "saved_at": None,
        "cookie_names": names,
        "required_cookie_names": list(required_cookie_names(platform_key)),
    }


def load_behavior_evidence(path: str | Path) -> dict[str, Any]:
    candidate = Path(path).expanduser()
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "missing",
            "evidence_path": str(candidate),
            "error": "behavior_evidence_missing_or_invalid",
        }
    return value if isinstance(value, dict) else {
        "status": "invalid",
        "evidence_path": str(candidate),
        "error": "behavior_evidence_not_an_object",
    }


def behavior_environment(evidence_path: Path, profile_name: str) -> dict[str, str]:
    return {
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_ENABLED": "1",
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE": profile_name,
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE": str(evidence_path),
        "TRIPPOSTCOLLECT_PROJECT_SCRIPTS": str(ROOT / "scripts"),
        "TRIPPOSTCOLLECT_BROWSER_ARGS_JSON": json.dumps(browser_runtime_args()),
    }


async def run_bilibili_behavior_session(
    args: argparse.Namespace,
    evidence_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    browser_path = discover_cdp_browser_path()
    profile_dir = ensure_dir(profile_dir_for("bilibili"))
    stealth_script = MEDIACRAWLER_DIR / "libs" / "stealth.min.js"
    target_url = "https://search.bilibili.com/article?keyword=" + quote(args.keyword)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=not args.headed,
            executable_path=browser_path,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
            screen={"width": 1440, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                *browser_runtime_args(),
            ],
            env=browser_launch_environment(),
            ignore_default_args=["--enable-automation"],
        )
        try:
            if stealth_script.is_file():
                await context.add_init_script(path=str(stealth_script))
            await install_runtime_hints(context)

            async def block_video_media(route) -> None:
                request = route.request
                if request.resource_type == "media" or VIDEO_URL_RE.search(request.url):
                    await route.abort()
                    return
                await route.continue_()

            await context.route("**/*", block_video_media)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            evidence = await run_page_behavior(
                page,
                platform_key="bilibili",
                evidence_path=evidence_path,
                profile_name="social_high_risk",
            )
            cookies = await context.cookies([platform_cookie_url("bilibili")])
        finally:
            await context.close()

    cookie_header = cookies_to_header(cookies)
    cookie_export = {
        "cookie_header": cookie_header,
        "source": "human_behavior_session",
        "snapshot_path": str(cookie_snapshot_path("bilibili")),
        "saved_at": evidence.get("finished_at"),
        "cookie_names": cookie_names_from_header(cookie_header),
        "required_cookie_names": list(required_cookie_names("bilibili")),
    }
    return cookie_export, evidence


def decode_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def tail(value: str, limit: int = 4000) -> str:
    return value[-limit:] if len(value) > limit else value


def json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "无"}:
        return None
    unit = 1
    if text.endswith("万"):
        unit = 10_000
        text = text[:-1]
    elif text.endswith("亿"):
        unit = 100_000_000
        text = text[:-1]
    try:
        return int(float(text) * unit)
    except ValueError:
        return None


def first_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def timestamp_to_iso(value: Any) -> str | None:
    parsed = parse_int(value)
    if parsed is None:
        return None
    if parsed > 10_000_000_000:
        parsed = parsed // 1000
    if parsed < TIMESTAMP_MIN or parsed > TIMESTAMP_MAX:
        return None
    try:
        return datetime.fromtimestamp(parsed, CHINA_TZ).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def parse_datetime_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text or text in {"-", "无"}:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in DATETIME_TEXT_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            parsed = None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed.astimezone(CHINA_TZ).isoformat(timespec="seconds")


def datetime_value_to_iso(value: Any) -> str | None:
    return timestamp_to_iso(value) or parse_datetime_text(value)


def iter_nested_values_for_keys(value: Any, keys: tuple[str, ...], *, depth: int = 0) -> list[Any]:
    if depth > 4:
        return []
    found: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and child not in (None, ""):
                found.append(child)
            found.extend(iter_nested_values_for_keys(child, keys, depth=depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.extend(iter_nested_values_for_keys(child, keys, depth=depth + 1))
    return found


def published_at_for_record(record: dict[str, Any]) -> str | None:
    for key in PUBLISHED_AT_KEYS:
        value = record.get(key)
        parsed = datetime_value_to_iso(value)
        if parsed:
            return parsed
    for value in iter_nested_values_for_keys(record, NESTED_PUBLISHED_AT_KEYS):
        parsed = datetime_value_to_iso(value)
        if parsed:
            return parsed
    return None


def normalize_image_url(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip().rstrip(").];")
    if text.startswith("//"):
        text = "https:" + text
    return text if text.startswith(("http://", "https://")) else None


def extract_image_urls(value: Any, *, key_hint: str = "") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            items.extend(extract_image_urls(child, key_hint=str(key)))
        return items
    if isinstance(value, list):
        for child in value:
            items.extend(extract_image_urls(child, key_hint=key_hint))
        return items
    if not isinstance(value, str):
        return items

    key_lower = key_hint.lower()
    key_is_image_like = any(marker in key_lower for marker in IMAGE_URL_KEYS)
    if key_is_image_like:
        image_url = normalize_image_url(value)
        if image_url and "," not in value and "，" not in value:
            role = "author_avatar" if "avatar" in key_lower else "content"
            return [{"url": image_url, "role": role, "source_key": key_hint}]

    for match in URL_RE.finditer(value):
        url = normalize_image_url(match.group(0))
        if url and (key_is_image_like or any(marker in url.lower() for marker in IMAGE_URL_KEYS)):
            role = "author_avatar" if "avatar" in key_lower else "content"
            items.append({"url": url, "role": role, "source_key": key_hint})
    return items


def dedupe_image_urls(record: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in extract_image_urls(record):
        key = (item["role"], item["url"])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def is_video_record(platform_key: str, record: dict[str, Any]) -> bool:
    if platform_key == "bilibili" and first_value(record, "video_id", "video_url", "bvid", "aid"):
        return True
    if platform_key == "zhihu":
        content_type = str(first_value(record, "content_type", "type") or "").strip().lower()
        content_url = str(first_value(record, "content_url", "url", "share_url") or "")
        if "zvideo" in content_type or content_type == "video" or "/zvideo/" in content_url:
            return True
    if platform_key == "douyin":
        note_images = first_value(record, "note_download_url", "image_list", "images")
        aweme_type = str(first_value(record, "aweme_type", "video_type", "media_type", "type") or "").strip().lower()
        if note_images or aweme_type in {"68", "note", "image", "images", "image_text", "图文"}:
            return False
        if first_value(record, "video_download_url"):
            return True
    video_type = str(first_value(record, "video_type", "media_type", "type") or "").strip().lower()
    if video_type and video_type not in {"note", "image", "images", "image_text", "图文"}:
        if "video" in video_type or "视频" in video_type:
            return True
    for key, value in record.items():
        key_lower = str(key).lower()
        if "video" not in key_lower and key_lower not in {"bvid", "aid"}:
            continue
        if value not in (None, "") and VIDEO_URL_RE.search(str(value)):
            return True
    for value in (first_value(record, "url", "share_url", "note_url", "aweme_url", "video_url"),):
        if value and VIDEO_URL_RE.search(str(value)):
            return True
    return False


def run_command(
    cmd: list[str],
    cwd: Path,
    timeout: int,
    log_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    log_dir = ensure_dir(log_dir)
    env = browser_launch_environment()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("UV_CACHE_DIR", str(ensure_dir(UV_CACHE_ROOT)))
    if extra_env:
        env.update(extra_env)
    started = time.monotonic()
    stdout = ""
    stderr = ""
    returncode = 0
    timed_out = False
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout_data, stderr_data = proc.communicate(timeout=timeout)
        stdout = decode_text(stdout_data)
        stderr = decode_text(stderr_data)
        returncode = int(proc.returncode or 0)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        stdout = decode_text(exc.stdout)
        stderr = decode_text(exc.stderr)
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                extra_stdout, extra_stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                extra_stdout, extra_stderr = proc.communicate()
            stdout += decode_text(extra_stdout)
            stderr += decode_text(extra_stderr)

    stdout_log = log_dir / "stdout.log"
    stderr_log = log_dir / "stderr.log"
    command_log = log_dir / "command.txt"
    stdout_log.write_text(stdout, encoding="utf-8")
    stderr_log.write_text(stderr, encoding="utf-8")
    command_log.write_text(shlex.join(cmd), encoding="utf-8")
    return {
        "command": cmd,
        "command_text": shlex.join(cmd),
        "returncode": returncode,
        "timed_out": timed_out,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "command_log": str(command_log),
        "stdout_tail": tail(stdout),
        "stderr_tail": tail(stderr),
    }


def skipped_command(cmd: list[str], log_dir: Path, reason: str) -> dict[str, Any]:
    log_dir = ensure_dir(log_dir)
    stdout_log = log_dir / "stdout.log"
    stderr_log = log_dir / "stderr.log"
    command_log = log_dir / "command.txt"
    stdout_log.write_text("", encoding="utf-8")
    stderr_log.write_text(reason, encoding="utf-8")
    command_log.write_text(shlex.join(cmd), encoding="utf-8")
    return {
        "command": cmd,
        "command_text": shlex.join(cmd),
        "returncode": 1,
        "timed_out": False,
        "elapsed_seconds": 0,
        "skipped": True,
        "reason": reason,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "command_log": str(command_log),
        "stdout_tail": "",
        "stderr_tail": reason,
    }


def item_type_from_path(path: Path) -> str:
    name = path.name
    if "_contents_" in name:
        return "contents"
    if "_comments_" in name:
        return "comments"
    if "_creators_" in name:
        return "creators"
    return "unknown"


def truncate(value: Any, limit: int = 240) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def clean_html_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_sample(record: dict[str, Any]) -> dict[str, str]:
    sample = {key: truncate(record[key]) for key in SAMPLE_KEYS if record.get(key) not in (None, "")}
    published_at = published_at_for_record(record)
    if published_at and "published_at" not in sample:
        sample["published_at"] = published_at
    return sample or {key: truncate(value) for key, value in list(record.items())[:8]}


def summarize_jsonl(path: Path, keyword: str) -> dict[str, Any]:
    item_type = item_type_from_path(path)
    platform_key = platform_from_path(path)
    fields: set[str] = set()
    author_like_fields: set[str] = set()
    samples: list[dict[str, str]] = []
    line_count = 0
    parse_errors = 0
    keyword_hits = 0
    video_like_records = 0
    published_at_records = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            line_count += 1
            if keyword in text:
                keyword_hits += 1
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if isinstance(record, dict):
                fields.update(record)
                author_like_fields.update(
                    key for key in record if any(marker in key.lower() for marker in AUTHOR_FIELD_MARKERS)
                )
                is_video = item_type == "contents" and is_video_record(platform_key, record)
                if is_video:
                    video_like_records += 1
                if item_type == "contents" and not is_video and published_at_for_record(record):
                    published_at_records += 1
                if item_type == "contents" and len(samples) < 3:
                    samples.append(extract_sample(record))
    return {
        "path": str(path),
        "item_type": item_type,
        "line_count": line_count,
        "keyword_hit_records": keyword_hits,
        "parse_errors": parse_errors,
        "video_like_records": video_like_records,
        "published_at_records": published_at_records,
        "top_level_fields": sorted(fields),
        "author_like_fields": sorted(author_like_fields),
        "samples": samples,
    }


def summarize_output(save_path: Path, keyword: str) -> dict[str, Any]:
    jsonl_files = sorted(save_path.rglob("*.jsonl")) if save_path.exists() else []
    jsonl = [summarize_jsonl(path, keyword) for path in jsonl_files]
    counts = {"contents": 0, "comments": 0, "creators": 0, "unknown": 0}
    fields: set[str] = set()
    author_like_fields: set[str] = set()
    samples: list[dict[str, str]] = []
    keyword_hits = 0
    parse_errors = 0
    video_like_records = 0
    published_at_records = 0
    for item in jsonl:
        counts[item["item_type"]] = counts.get(item["item_type"], 0) + item["line_count"]
        fields.update(item["top_level_fields"])
        author_like_fields.update(item["author_like_fields"])
        keyword_hits += item["keyword_hit_records"]
        parse_errors += item["parse_errors"]
        video_like_records += int(item.get("video_like_records") or 0)
        published_at_records += int(item.get("published_at_records") or 0)
        samples.extend(item["samples"])

    files = [path for path in save_path.rglob("*") if path.is_file()] if save_path.exists() else []
    image_files = [path for path in files if path.suffix.lower() in IMAGE_SUFFIXES]
    video_files = [path for path in files if path.suffix.lower() in VIDEO_SUFFIXES]
    return {
        "save_path": str(save_path),
        "jsonl_files": [str(path) for path in jsonl_files],
        "jsonl_file_count": len(jsonl_files),
        "content_records": counts.get("contents", 0),
        "non_video_content_records": max(0, counts.get("contents", 0) - video_like_records),
        "video_like_records": video_like_records,
        "comment_records": counts.get("comments", 0),
        "creator_records": counts.get("creators", 0),
        "unknown_records": counts.get("unknown", 0),
        "total_jsonl_records": sum(counts.values()),
        "keyword_hit_records": keyword_hits,
        "parse_errors": parse_errors,
        "image_file_count": len(image_files),
        "video_file_count": len(video_files),
        "published_at_records": published_at_records,
        "top_level_fields": sorted(fields)[:120],
        "author_like_fields": sorted(author_like_fields),
        "samples": samples[:5],
        "files": jsonl,
    }


def ensure_web_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    return bootstrap_connection(conn)


def platform_from_path(path: Path) -> str:
    parts = [part.lower() for part in path.parts]
    if "bili" in parts:
        return "bilibili"
    if "zhihu" in parts:
        return "zhihu"
    if "xhs" in parts:
        return "xhs"
    if "weibo" in parts:
        return "weibo"
    if "douyin" in parts:
        return "douyin"
    for key in PLATFORMS:
        if key in parts:
            return key
    return "unknown"


def post_id_for_record(platform_key: str, record: dict[str, Any]) -> str | None:
    value = first_value(record, "note_id", "aweme_id", "content_id", "video_id", "bvid", "aid", "id")
    return str(value) if value not in (None, "") else None


def canonical_url_for_record(platform_key: str, record: dict[str, Any]) -> str | None:
    value = first_value(record, "note_url", "aweme_url", "content_url", "video_url", "url", "share_url")
    if value:
        return str(value)
    post_id = post_id_for_record(platform_key, record)
    if not post_id:
        return None
    if platform_key == "weibo":
        return f"https://m.weibo.cn/detail/{post_id}"
    if platform_key == "douyin":
        return f"https://www.douyin.com/video/{post_id}"
    if platform_key == "bilibili":
        content_type = str(first_value(record, "content_type", "type") or "").strip().lower()
        if content_type in {"article", "read", "opus", "dynamic"}:
            return f"https://www.bilibili.com/read/cv{post_id}/"
        return f"https://www.bilibili.com/video/av{post_id}"
    if platform_key == "xhs":
        return f"https://www.xiaohongshu.com/explore/{post_id}"
    if platform_key == "zhihu":
        content_type = str(first_value(record, "content_type", "type") or "").strip().lower()
        question_id = first_value(record, "question_id")
        if content_type == "article":
            return f"https://zhuanlan.zhihu.com/p/{post_id}"
        if content_type == "answer" and question_id:
            return f"https://www.zhihu.com/question/{question_id}/answer/{post_id}"
    return None


def content_text_for_record(platform_key: str, record: dict[str, Any]) -> str:
    title = str(first_value(record, "title") or "").strip()
    body = str(first_value(record, "content_text", "content", "desc") or "").strip()
    if platform_key in {"xhs", "zhihu"}:
        parts = [part for part in (title, body) if part]
        return "\n".join(dict.fromkeys(parts))
    return str(first_value(record, "content_text", "content", "desc", "title") or "")


def city_name_from_keyword(keyword: str) -> str | None:
    text = str(keyword or "").strip()
    if not text:
        return None
    best_match: tuple[int, int, int, str] | None = None
    for order, (city_name, aliases) in enumerate(SHANDONG_CITY_ALIASES):
        for alias in aliases:
            index = text.find(alias)
            if index < 0:
                continue
            candidate = (index, -len(alias), order, city_name)
            if best_match is None or candidate < best_match:
                best_match = candidate
    return best_match[3] if best_match else None


def row_for_record(
    platform_key: str,
    record: dict[str, Any],
    *,
    artifact_dir: str,
    captured_at: str,
    keyword: str,
) -> dict[str, Any]:
    content_text = content_text_for_record(platform_key, record)
    canonical_url = canonical_url_for_record(platform_key, record)
    image_items = dedupe_image_urls(record)
    keyword_value = str(record.get("source_keyword") or keyword or "")
    metrics = {
        "liked_count": parse_int(first_value(record, "liked_count", "voteup_count")),
        "favorites_count": parse_int(first_value(record, "collected_count", "video_favorite_count")),
        "comments_count": parse_int(first_value(record, "comment_count", "comments_count", "video_comment")),
        "shares_count": parse_int(first_value(record, "share_count", "shared_count", "video_share_count")),
        "reposts_count": parse_int(first_value(record, "reposts_count", "repost_count")),
        "views_count": parse_int(first_value(record, "video_play_count", "view_count", "play_count")),
    }
    author = {
        "nickname": first_value(record, "nickname", "user_nickname", "user_name", "author_name"),
        "creator_hash": first_value(record, "user_id", "creator_id", "creator_hash", "author_id"),
        "avatar_url": first_value(record, "avatar_url", "avatar", "user_avatar"),
        "followers_count": parse_int(
            first_value(
                record,
                "author_followers_count",
                "followers_count",
                "follower_count",
                "fans_count",
                "fans",
                "followers",
            )
        ),
        "following_count": parse_int(first_value(record, "author_following_count", "following_count", "follow_count", "follows")),
        "posts_count": parse_int(first_value(record, "author_posts_count", "aweme_count", "posts_count", "note_count", "notes_count", "video_count")),
        "liked_count": parse_int(first_value(record, "author_liked_count", "total_favorited", "favorited_count")),
        "followers_observed": record.get("followers_observed") is True,
        "followers_source": first_value(record, "author_followers_source", "followers_source"),
    }
    return {
        "platform_key": platform_key,
        "platform_post_id": post_id_for_record(platform_key, record),
        "source_type": "mediacrawler_search",
        "source_url": canonical_url or "",
        "canonical_url": canonical_url,
        "title": first_value(record, "title"),
        "author_display_name": author["nickname"],
        "author_platform_id": author["creator_hash"],
        "author_profile_url": first_value(record, "author_profile_url", "user_link", "profile_url", "user_url"),
        "author_avatar_url": author["avatar_url"],
        "author_description": first_value(record, "author_desc", "user_desc"),
        "author_followers_count": author["followers_count"],
        "author_following_count": author["following_count"],
        "author_posts_count": author["posts_count"],
        "author_platform_level": first_value(record, "level", "user_level"),
        "author_verified": None,
        "author_verified_text": first_value(record, "verified_text", "verify_info"),
        "published_at": published_at_for_record(record),
        "captured_at": captured_at,
        "city_name": city_name_from_keyword(keyword_value),
        "keyword": keyword_value,
        "content_text": content_text,
        "content_length": len(content_text),
        "post_likes_count": metrics["liked_count"],
        "post_favorites_count": metrics["favorites_count"],
        "post_comments_count": metrics["comments_count"],
        "post_shares_count": metrics["shares_count"],
        "post_reposts_count": metrics["reposts_count"],
        "post_views_count": metrics["views_count"],
        "post_images_count": len([item for item in image_items if item["role"] == "content"]),
        "metrics_json": json_dump(metrics),
        "author_json": json_dump(author),
        "raw_sample_json": json_dump(record),
        "artifact_dir": artifact_dir,
        "capture_method": "import",
        "status": "captured" if content_text else "partial",
        "_image_items": image_items,
    }


def formal_record_identity(platform_key: str, record: dict[str, Any]) -> str:
    post_id = post_id_for_record(platform_key, record)
    if post_id:
        return f"{platform_key}:id:{post_id}"
    canonical_url = canonical_url_for_record(platform_key, record)
    return f"{platform_key}:url:{canonical_url}" if canonical_url else ""


def formal_database_identities(platform_key: str, record: dict[str, Any]) -> set[str]:
    identities: set[str] = set()
    post_id = post_id_for_record(platform_key, record)
    canonical_url = canonical_url_for_record(platform_key, record)
    if post_id:
        identities.add(f"{platform_key}:id:{post_id}")
    if canonical_url:
        identities.add(f"{platform_key}:url:{canonical_url}")
    return identities


def validate_formal_record(platform_key: str, record: dict[str, Any], seen: set[str]) -> dict[str, Any]:
    identity = formal_record_identity(platform_key, record)
    reasons: list[str] = []
    if not identity:
        reasons.append("missing_identity")
    elif identity in seen:
        reasons.append("duplicate_identity")
    if is_video_record(platform_key, record):
        reasons.append("video_record")
    if not content_text_for_record(platform_key, record).strip():
        reasons.append("missing_content")
    if not published_at_for_record(record):
        reasons.append("missing_published_at")
    if not first_value(record, "user_id", "creator_id", "creator_hash", "author_id", "mid"):
        reasons.append("missing_author_id")
    if not first_value(record, "nickname", "user_nickname", "user_name", "author_name", "author"):
        reasons.append("missing_author_name")
    content_images = [item for item in dedupe_image_urls(record) if item.get("role") == "content"]
    if not content_images:
        detail_status = str(record.get("content_detail_status") or "")
        if (
            platform_key == "zhihu"
            and str(record.get("content_type") or "") in {"answer", "article"}
            and detail_status != "detail_observed"
        ):
            reasons.append("content_detail_unobserved")
        else:
            reasons.append("missing_content_image")

    followers_count = parse_int(
        first_value(
            record,
            "author_followers_count",
            "followers_count",
            "follower_count",
            "fans_count",
            "fans",
            "followers",
        )
    )
    followers_observed = record.get("followers_observed") is True
    followers_source = str(first_value(record, "author_followers_source", "followers_source") or "")
    if platform_key in FOLLOWERS_REQUIRED_PLATFORMS:
        if followers_count is None:
            reasons.append("missing_followers_count")
        if not followers_observed:
            reasons.append("followers_not_observed")
        if not followers_source or followers_source == "missing":
            reasons.append("missing_followers_source")
        elif followers_source not in REQUIRED_FOLLOWER_SOURCES.get(platform_key, frozenset()):
            reasons.append("untrusted_followers_source")

    missing_metric_groups = [
        "/".join(keys)
        for keys in PLATFORM_REQUIRED_METRICS.get(platform_key, ())
        if first_value(record, *keys) is None
    ]
    if missing_metric_groups:
        reasons.append("missing_metrics:" + ",".join(missing_metric_groups))
    return {
        "valid": not reasons,
        "identity": identity,
        "reasons": reasons,
        "followers_count": followers_count,
        "followers_observed": followers_observed,
        "followers_source": followers_source,
        "content_image_count": len(content_images),
    }


PAGINATION_EVENT_FIELDS = (
    "platform",
    "batch_no",
    "candidate_count",
    "valid_new_count",
    "valid_existing_count",
    "batch_new_count",
    "batch_candidate_identity_count",
    "source_page",
    "source_offset",
    "source_cursor",
    "next_cursor",
    "resume_page",
    "resume_offset",
    "resume_cursor",
    "batch_complete",
    "discovery_phase",
    "source_has_more",
    "raw_batch_count",
    "raw_response_count",
    "stagnant_batches",
    "stagnation_basis",
    "stop_reason",
    "stop_detail",
    "candidate_identities",
)

DISCOVERY_RESEED_EVENT_FIELDS = (
    "platform",
    "reason",
    "saved_resume_page",
    "saved_resume_offset",
    "saved_resume_cursor",
    "resume_page",
    "resume_offset",
    "resume_cursor",
    "refresh_new_candidate_count",
)


def persist_discovery_checkpoint(
    args: argparse.Namespace,
    platform_key: str,
    pagination_evidence: dict[str, Any],
) -> dict[str, Any]:
    if args.discovery_job_id is None:
        return {"skipped": True, "reason": "not_scheduler_managed"}
    if args.no_checkpoint_write:
        return {"skipped": True, "reason": "checkpoint_write_disabled"}
    event = pagination_evidence.get("stop_event") or (
        (pagination_evidence.get("batches") or [None])[-1]
    )
    if not isinstance(event, dict):
        return {"skipped": True, "reason": "no_frontier_batch_evidence"}
    refresh_only = event.get("discovery_phase") == "refresh"
    if refresh_only:
        resume_page = int(args.start_page)
        resume_offset = (
            int(args.start_offset)
            if platform_key == "douyin" and args.start_offset is not None
            else None
        )
        resume_cursor = (
            (str(args.start_cursor or "") or None)
            if platform_key == "douyin"
            else None
        )
        source_has_more_value = False if args.discovery_source_exhausted else None
    else:
        resume_page = int(
            event.get("resume_page") or event.get("source_page") or args.start_page
        )
        resume_offset_value = event.get("resume_offset")
        resume_offset = (
            int(resume_offset_value)
            if resume_offset_value not in (None, "")
            else None
        )
        resume_cursor = str(event.get("resume_cursor") or "") or None
        source_has_more_value = event.get("source_has_more")
    source_has_more = (
        None if source_has_more_value is None else bool(source_has_more_value)
    )
    with sqlite3.connect(Path(args.db).expanduser()) as conn:
        bootstrap_connection(conn, sync_content=False, sync_jobs=False)
        save_checkpoint(
            conn,
            job_id=int(args.discovery_job_id),
            platform_key=platform_key,
            keyword=args.keyword,
            query_fingerprint_value=str(args.discovery_query_fingerprint),
            resume_page=resume_page,
            resume_offset=resume_offset,
            resume_cursor=resume_cursor,
            source_has_more=source_has_more,
            last_batch_complete=bool(event.get("batch_complete")),
            last_stop_reason=str(event.get("stop_reason") or "continue"),
            last_run_id=str(args.discovery_run_id),
        )
        seen_candidate_count = save_seen_candidates(
            conn,
            job_id=int(args.discovery_job_id),
            platform_key=platform_key,
            query_fingerprint_value=str(args.discovery_query_fingerprint),
            platform_post_ids=list(event.get("candidate_identities") or []),
            run_id=str(args.discovery_run_id),
        )
        conn.commit()
    return {
        "skipped": False,
        "resume_page": resume_page,
        "resume_offset": resume_offset,
        "resume_cursor": resume_cursor,
        "source_has_more": source_has_more,
        "last_stop_reason": str(event.get("stop_reason") or "continue"),
        "refresh_only": refresh_only,
        "seen_candidate_count": seen_candidate_count,
    }


def load_existing_formal_identities(db_path: str | Path | None) -> set[str]:
    if not db_path:
        return set()
    try:
        with sqlite3.connect(Path(db_path).expanduser()) as conn:
            rows = conn.execute(
                """
                SELECT platform_key, platform_post_id, canonical_url
                FROM web_posts
                """
            ).fetchall()
    except (OSError, sqlite3.Error):
        return set()
    identities: set[str] = set()
    for platform_key, platform_post_id, canonical_url in rows:
        if platform_post_id:
            identities.add(f"{platform_key}:id:{platform_post_id}")
        if canonical_url:
            identities.add(f"{platform_key}:url:{canonical_url}")
    return identities


def load_pagination_evidence(state_path: str | Path | None) -> dict[str, Any]:
    if not state_path:
        return {"available": False, "stopped": False, "batches": []}
    path = Path(state_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {
            "available": False,
            "stopped": False,
            "state_path": str(path),
            "batches": [],
        }

    batches = []
    stopped_details: dict[str, Any] | None = None
    frontier_reseeds: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        details = event.get("details") or {}
        if not isinstance(details, dict):
            continue
        selected = {key: details.get(key) for key in PAGINATION_EVENT_FIELDS if key in details}
        if event.get("type") == "adaptive_batch_completed":
            batches.append(selected)
        elif event.get("type") == "adaptive_search_stopped":
            stopped_details = selected
        elif event.get("type") == "discovery_frontier_reseeded":
            frontier_reseeds.append(
                {
                    key: details.get(key)
                    for key in DISCOVERY_RESEED_EVENT_FIELDS
                    if key in details
                }
            )

    latest = stopped_details or (batches[-1] if batches else {})
    return {
        "available": bool(batches or stopped_details),
        "state_path": str(path),
        "batch_count": len(batches),
        "candidate_count": int(latest.get("candidate_count") or 0),
        "stopped": stopped_details is not None,
        "stop_reason": str((stopped_details or {}).get("stop_reason") or ""),
        "stop_detail": str((stopped_details or {}).get("stop_detail") or ""),
        "batches": batches,
        "frontier_reseeds": frontier_reseeds,
        "stop_event": stopped_details,
    }


def collect_formal_records(
    summary: dict[str, Any],
    *,
    candidate_hard_limit: int,
    target_new_posts: int,
    db_path: str | Path | None,
    pagination_evidence: dict[str, Any] | None = None,
    enforce_candidate_limit: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    existing_identities = load_existing_formal_identities(db_path)
    valid_new_count = 0
    valid_existing_count = 0
    reason_counts: Counter[str] = Counter()
    candidate_count = 0
    parse_errors = 0
    stop = False
    for platform_record in summary.get("records") or []:
        output = platform_record.get("output") if isinstance(platform_record, dict) else {}
        for path_value in (output or {}).get("jsonl_files") or []:
            path = Path(path_value)
            if item_type_from_path(path) != "contents":
                continue
            platform_key = platform_from_path(path)
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if enforce_candidate_limit and candidate_count >= candidate_hard_limit:
                        stop = True
                        break
                    text = line.strip()
                    if not text:
                        continue
                    candidate_count += 1
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        parse_errors += 1
                        reason_counts["invalid_json"] += 1
                        continue
                    if not isinstance(record, dict):
                        reason_counts["invalid_record_type"] += 1
                        continue
                    validation = validate_formal_record(platform_key, record, seen)
                    if not validation["valid"]:
                        reason_counts.update(validation["reasons"])
                        continue
                    seen.add(str(validation["identity"]))
                    is_existing = bool(
                        formal_database_identities(platform_key, record) & existing_identities
                    )
                    valid_existing_count += int(is_existing)
                    valid_new_count += int(not is_existing)
                    selected.append(
                        {
                            "platform": platform_key,
                            "record": record,
                            "identity": validation["identity"],
                            "source_path": str(path),
                            "line_number": line_number,
                            "is_new": not is_existing,
                        }
                    )
                    if target_new_posts > 0 and valid_new_count >= target_new_posts:
                        stop = True
                        break
            if stop:
                break
        if stop:
            break

    output_record_count = candidate_count
    pagination_evidence = pagination_evidence or {}
    run_candidate_count = int(pagination_evidence.get("candidate_count") or 0)
    candidate_count = max(candidate_count, run_candidate_count)
    new_target_met = target_new_posts <= 0 or valid_new_count >= target_new_posts
    if new_target_met and target_new_posts > 0:
        stop_reason = "target_new_met"
    elif run_candidate_count >= candidate_hard_limit or (
        enforce_candidate_limit and candidate_count >= candidate_hard_limit
    ):
        stop_reason = "candidate_hard_limit_reached"
    elif pagination_evidence.get("stopped") and pagination_evidence.get("stop_reason") in {
        "source_exhausted",
        "stagnated",
        "runtime_failed",
        "login_required",
        "captcha_detected",
    }:
        stop_reason = str(pagination_evidence["stop_reason"])
    else:
        stop_reason = "runtime_failed"
    validation_summary = {
        "candidate_hard_limit": candidate_hard_limit,
        "candidate_count": candidate_count,
        "run_candidate_count": run_candidate_count,
        "output_record_count": output_record_count,
        "target_new_posts": target_new_posts,
        "valid_new_count": valid_new_count,
        "valid_existing_count": valid_existing_count,
        "valid_total_count": len(selected),
        "new_target_met": new_target_met,
        "stop_reason": stop_reason,
        "stop_detail": str(pagination_evidence.get("stop_detail") or ""),
        "pagination_evidence": pagination_evidence,
        "parse_errors": parse_errors,
        "invalid_reason_counts": dict(sorted(reason_counts.items())),
        "new_identities": [item["identity"] for item in selected if item["is_new"]],
        "existing_identities": [item["identity"] for item in selected if not item["is_new"]],
        "valid_new_samples": [
            {
                "identity": item["identity"],
                "source_path": item["source_path"],
                "line_number": item["line_number"],
            }
            for item in selected
            if item["is_new"]
        ][:5],
        "valid_existing_samples": [
            {
                "identity": item["identity"],
                "source_path": item["source_path"],
                "line_number": item["line_number"],
            }
            for item in selected
            if not item["is_new"]
        ][:5],
    }
    return validation_summary, selected


def find_existing_post(conn: sqlite3.Connection, row: dict[str, Any]) -> int | None:
    if row.get("platform_post_id"):
        found = conn.execute(
            "SELECT id FROM web_posts WHERE platform_key=? AND platform_post_id=?",
            (row["platform_key"], row["platform_post_id"]),
        ).fetchone()
        if found:
            return int(found[0])
    if row.get("canonical_url"):
        found = conn.execute(
            "SELECT id FROM web_posts WHERE platform_key=? AND canonical_url=?",
            (row["platform_key"], row["canonical_url"]),
        ).fetchone()
        if found:
            return int(found[0])
    return None


def upsert_web_post(conn: sqlite3.Connection, row: dict[str, Any]) -> tuple[int, bool]:
    image_items = row.pop("_image_items", [])
    existing_id = find_existing_post(conn, row)
    columns = list(row)
    if existing_id:
        updates = ", ".join(f"{column}=:{column}" for column in columns)
        conn.execute(f"UPDATE web_posts SET {updates}, updated_at=datetime('now') WHERE id=:id", {**row, "id": existing_id})
        post_id = existing_id
    else:
        placeholders = ", ".join(f":{column}" for column in columns)
        conn.execute(f"INSERT INTO web_posts ({', '.join(columns)}) VALUES ({placeholders})", row)
        post_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    conn.execute("DELETE FROM web_post_images WHERE web_post_id=?", (post_id,))
    for index, item in enumerate(image_items):
        conn.execute(
            """
            INSERT INTO web_post_images (
                web_post_id, image_index, image_url, image_role, local_path, raw_image_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (post_id, index, item["url"], item["role"], None, json_dump(item)),
        )
    return post_id, existing_id is None


def import_valid_records(
    summary: dict[str, Any],
    selected: list[dict[str, Any]],
    db_path: Path,
) -> dict[str, Any]:
    db_path = ensure_parent(db_path)
    captured_at = str(summary.get("captured_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    keyword = str(summary.get("keyword") or "")
    processed = inserted = updated = 0
    with sqlite3.connect(db_path) as conn:
        db_sync = ensure_web_schema(conn)
        for item in selected:
            record = item["record"]
            platform_key = str(item["platform"])
            row = row_for_record(
                platform_key,
                record,
                artifact_dir=str(Path(str(summary.get("batch_dir") or "")).resolve()),
                captured_at=captured_at,
                keyword=keyword,
            )
            _, was_inserted = upsert_web_post(conn, row)
            processed += 1
            inserted += int(was_inserted)
            updated += int(not was_inserted)
        conn.commit()
    return {
        "db": str(db_path),
        "db_sync": db_sync,
        "processed_rows": processed,
        "inserted_rows": inserted,
        "updated_rows": updated,
        "skipped": 0,
        "skipped_video": 0,
        "parse_errors": 0,
    }


def normalize_bilibili_article_record(item: dict[str, Any], keyword: str) -> dict[str, Any] | None:
    post_id = str(item.get("id") or "").strip()
    if not post_id:
        return None
    title = clean_html_text(item.get("title"))
    desc = clean_html_text(item.get("desc"))
    if not title and not desc:
        return None
    content_url = str(item.get("arcurl") or item.get("url") or f"https://www.bilibili.com/read/cv{post_id}/")
    image_urls = item.get("image_urls") if isinstance(item.get("image_urls"), list) else []
    content_parts = [part for part in (title, desc) if part]
    record = dict(item)
    record.update(
        {
            "id": post_id,
            "content_id": post_id,
            "content_type": "article",
            "title": title,
            "desc": desc,
            "content_text": "\n".join(dict.fromkeys(content_parts)),
            "content_url": content_url,
            "image_urls": image_urls,
            "source_keyword": keyword,
            "created_time": item.get("pubdate") or item.get("pub_time"),
            "published_at": item.get("pubdate") or item.get("pub_time"),
            "liked_count": item.get("like"),
            "comment_count": item.get("reply"),
            "view_count": item.get("view"),
            "nickname": item.get("author"),
            "user_id": item.get("mid"),
            "raw_bilibili_type": item.get("type"),
        }
    )
    return record


def fetch_bilibili_wbi_keys(cookie_header: str = "") -> tuple[str, str]:
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers=headers,
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    data = payload.get("data") or {}
    wbi_img = data.get("wbi_img") or {}
    img_url = str(wbi_img.get("img_url") or "")
    sub_url = str(wbi_img.get("sub_url") or "")
    if not img_url or not sub_url:
        raise RuntimeError("bilibili nav response missing WBI keys")
    return Path(img_url).stem, Path(sub_url).stem


def sign_bilibili_wbi_params(params: dict[str, Any], img_key: str, sub_key: str) -> dict[str, str]:
    mixin_key = img_key + sub_key
    salt = "".join(mixin_key[index] for index in BILIBILI_WBI_MIXIN_TABLE)[:32]
    signed = {**params, "wts": int(time.time())}
    filtered = {
        key: "".join(character for character in str(value) if character not in "!'()*")
        for key, value in sorted(signed.items())
    }
    query = urlencode(filtered)
    filtered["w_rid"] = md5((query + salt).encode("utf-8")).hexdigest()
    return filtered


def fetch_bilibili_article_page(
    keyword: str,
    page: int,
    *,
    wbi_keys: tuple[str, str] | None = None,
    cookie_header: str = "",
) -> list[dict[str, Any]]:
    params = {
        "keyword": keyword,
        "page": page,
        "page_size": BILIBILI_ARTICLE_PAGE_SIZE,
        "search_type": "article",
    }
    img_key, sub_key = wbi_keys or fetch_bilibili_wbi_keys(cookie_header)
    signed_params = sign_bilibili_wbi_params(params, img_key, sub_key)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://search.bilibili.com/article?keyword=" + quote(keyword),
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(BILIBILI_ARTICLE_SEARCH_URL + "?" + urlencode(signed_params), headers=headers)
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if payload.get("code") != 0:
        raise RuntimeError(f"bilibili article search failed: {payload.get('code')} {payload.get('message')}")
    result = (payload.get("data") or {}).get("result") or []
    return [item for item in result if isinstance(item, dict)]


def fetch_bilibili_follower_count(creator_id: str, cookie_header: str = "") -> int | None:
    if not creator_id:
        return None
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://space.bilibili.com/{creator_id}",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = Request(
        BILIBILI_RELATION_STAT_URL + "?" + urlencode({"vmid": creator_id}),
        headers=headers,
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if payload.get("code") != 0:
        return None
    value = (payload.get("data") or {}).get("follower")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def run_bilibili_article_search(args: argparse.Namespace, batch_dir: Path) -> dict[str, Any]:
    platform_key = "bilibili"
    platform = PLATFORMS[platform_key]
    save_path = ensure_dir(batch_dir / platform_key / "data" / platform["mediacrawler"] / "jsonl")
    log_dir = ensure_dir(batch_dir / "logs" / platform_key)
    jsonl_path = save_path / f"search_contents_{datetime.now(CHINA_TZ).date().isoformat()}.jsonl"
    stdout_log = log_dir / "stdout.log"
    stderr_log = log_dir / "stderr.log"
    command_log = log_dir / "command.txt"
    behavior_evidence_path = log_dir / "behavior_evidence.json"
    command = [
        "bilibili_article_search",
        "--keyword",
        args.keyword,
        "--candidate-hard-limit",
        str(args.candidate_hard_limit),
    ]

    started = time.monotonic()
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    follower_cache: dict[str, int | None] = {}
    valid_seen: set[str] = set()
    valid_new_count = 0
    valid_existing_count = 0
    candidate_count = 0
    stagnant_pages = 0
    state_path = os.environ.get("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", "").strip()
    stderr = ""
    returncode = 0
    behavior_evidence: dict[str, Any] = load_behavior_evidence(behavior_evidence_path)
    try:
        max_records = int(
            getattr(args, "source_candidate_hard_limit", args.candidate_hard_limit)
        )
        target_new = max(
            1,
            int(getattr(args, "source_target_new_posts", args.target_new_posts) or max_records),
        )
        existing_identities = load_existing_formal_identities(args.db)
        known_post_ids = {
            identity.split(":id:", 1)[1]
            for identity in existing_identities
            if identity.startswith("bilibili:id:")
        }
        discovery_job_id = getattr(args, "discovery_job_id", None)
        discovery_fingerprint = str(
            getattr(args, "discovery_query_fingerprint", "") or ""
        ).strip()
        if discovery_job_id is not None and discovery_fingerprint:
            with sqlite3.connect(Path(args.db).expanduser()) as conn:
                known_post_ids.update(
                    load_seen_candidates(
                        conn,
                        job_id=int(discovery_job_id),
                        platform_key=platform_key,
                        query_fingerprint_value=discovery_fingerprint,
                    )
                )
        if args.resume_identities_path:
            try:
                resume_values = json.loads(
                    Path(args.resume_identities_path).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError, TypeError):
                resume_values = []
            known_post_ids.update(str(value) for value in resume_values if value)
        cookie_export, behavior_evidence = asyncio.run(
            run_bilibili_behavior_session(args, behavior_evidence_path)
        )
        cookie_header = str((cookie_export or {}).get("cookie_header") or "")
        wbi_keys = fetch_bilibili_wbi_keys(cookie_header)
        frontier_start = max(1, int(args.start_page))
        phases: list[tuple[str, int, int | None]] = []
        if frontier_start > 1 and args.top_refresh_max_pages > 0:
            phases.append(
                ("refresh", 1, min(frontier_start - 1, args.top_refresh_max_pages))
            )
        if not args.discovery_source_exhausted:
            phases.append(("frontier", frontier_start, None))

        stop_all = False
        for discovery_phase, phase_start, phase_end in phases:
            page = phase_start
            while not stop_all and (phase_end is None or page <= phase_end):
                page_items = fetch_bilibili_article_page(
                    args.keyword,
                    page,
                    wbi_keys=wbi_keys,
                    cookie_header=cookie_header,
                )
                if not page_items:
                    if discovery_phase == "frontier" and state_path:
                        FrozenExecutionState(state_path).append_event(
                            "adaptive_search_stopped",
                            {
                                "platform": platform_key,
                                "candidate_count": candidate_count,
                                "valid_new_count": valid_new_count,
                                "valid_existing_count": valid_existing_count,
                                "target_new": target_new,
                                "hard_limit": max_records,
                                "stagnant_batches": stagnant_pages,
                                "stop_reason": "source_exhausted",
                                "stop_detail": "empty_page",
                                "source_page": page,
                                "resume_page": page,
                                "source_has_more": False,
                                "batch_complete": True,
                                "discovery_phase": discovery_phase,
                                "raw_batch_count": 0,
                                "raw_response_count": 0,
                                "stagnation_basis": "candidate_identity",
                                "candidate_identities": sorted(seen_ids),
                            },
                        )
                    break
                new_before = valid_new_count
                seen_before = len(seen_ids)
                processed_in_batch = 0
                batch_complete = True
                for item_index, item in enumerate(page_items):
                    post_id = str(item.get("id") or "").strip()
                    if post_id and (post_id in known_post_ids or post_id in seen_ids):
                        continue
                    if candidate_count >= max_records:
                        batch_complete = False
                        break
                    candidate_count += 1
                    processed_in_batch += 1
                    normalized = normalize_bilibili_article_record(item, args.keyword)
                    if not normalized:
                        continue
                    post_id = str(normalized.get("content_id") or "")
                    if post_id in seen_ids:
                        continue
                    creator_id = str(normalized.get("user_id") or "")
                    if creator_id:
                        if creator_id not in follower_cache:
                            try:
                                follower_cache[creator_id] = fetch_bilibili_follower_count(
                                    creator_id,
                                    cookie_header,
                                )
                            except Exception:
                                follower_cache[creator_id] = None
                            time.sleep(0.15)
                        follower_count = follower_cache[creator_id]
                        normalized["followers_observed"] = follower_count is not None
                        normalized["author_followers_source"] = (
                            "relation_stat" if follower_count is not None else "missing"
                        )
                        if follower_count is not None:
                            normalized["followers_count"] = follower_count
                            normalized["author_followers_count"] = follower_count
                    seen_ids.add(post_id)
                    known_post_ids.add(post_id)
                    records.append(normalized)
                    validation = validate_formal_record(platform_key, normalized, valid_seen)
                    if validation["valid"]:
                        identity = str(validation["identity"])
                        valid_seen.add(identity)
                        if formal_database_identities(platform_key, normalized) & existing_identities:
                            valid_existing_count += 1
                        else:
                            valid_new_count += 1
                    if candidate_count >= max_records or valid_new_count >= target_new:
                        batch_complete = item_index == len(page_items) - 1
                        break
                candidate_identities_added = len(seen_ids) - seen_before
                if discovery_phase == "frontier":
                    stagnant_pages = stagnant_pages + 1 if candidate_identities_added == 0 else 0
                if valid_new_count >= target_new:
                    batch_stop_reason = "target_new_met"
                elif candidate_count >= max_records:
                    batch_stop_reason = "candidate_hard_limit_reached"
                elif (
                    discovery_phase == "frontier"
                    and stagnant_pages >= max(1, args.max_stagnant_batches)
                ):
                    batch_stop_reason = "stagnated"
                else:
                    batch_stop_reason = "continue"
                resume_page = page + 1 if batch_complete else page
                event_details = {
                    "platform": platform_key,
                    "batch_no": page,
                    "candidate_count": candidate_count,
                    "valid_new_count": valid_new_count,
                    "valid_existing_count": valid_existing_count,
                    "batch_new_count": valid_new_count - new_before,
                    "batch_candidate_identity_count": candidate_identities_added,
                    "stagnant_batches": stagnant_pages,
                    "stagnation_basis": "candidate_identity",
                    "target_new": target_new,
                    "hard_limit": max_records,
                    "stop_reason": batch_stop_reason,
                    "source_page": page,
                    "resume_page": resume_page,
                    "source_has_more": None,
                    "batch_complete": batch_complete,
                    "discovery_phase": discovery_phase,
                    "raw_batch_count": processed_in_batch,
                    "raw_response_count": len(page_items),
                    "candidate_identities": sorted(seen_ids),
                }
                if state_path:
                    frozen_state = FrozenExecutionState(state_path)
                    frozen_state.append_event("adaptive_batch_completed", event_details)
                    if batch_stop_reason != "continue":
                        frozen_state.append_event("adaptive_search_stopped", event_details)
                if batch_stop_reason != "continue":
                    stop_all = True
                    break
                page += 1

        if (not phases or (args.discovery_source_exhausted and not stop_all)) and state_path:
            FrozenExecutionState(state_path).append_event(
                "adaptive_search_stopped",
                {
                    "platform": platform_key,
                    "candidate_count": candidate_count,
                    "valid_new_count": valid_new_count,
                    "valid_existing_count": valid_existing_count,
                    "target_new": target_new,
                    "hard_limit": max_records,
                    "stagnant_batches": stagnant_pages,
                    "stop_reason": "source_exhausted",
                    "stop_detail": "saved_source_exhausted",
                    "source_page": frontier_start,
                    "resume_page": frontier_start,
                    "source_has_more": False,
                    "batch_complete": True,
                    "discovery_phase": "frontier",
                    "raw_batch_count": 0,
                    "raw_response_count": 0,
                    "stagnation_basis": "candidate_identity",
                    "candidate_identities": sorted(seen_ids),
                },
            )
    except Exception as exc:
        returncode = 1
        stderr = repr(exc)
        behavior_evidence = load_behavior_evidence(behavior_evidence_path)
        if state_path:
            FrozenExecutionState(state_path).append_event(
                "adaptive_search_stopped",
                {
                    "platform": platform_key,
                    "candidate_count": candidate_count,
                    "valid_new_count": valid_new_count,
                    "valid_existing_count": valid_existing_count,
                    "target_new": locals().get(
                        "target_new",
                        max(1, int(args.target_new_posts or args.candidate_hard_limit)),
                    ),
                    "hard_limit": locals().get("max_records", args.candidate_hard_limit),
                    "stagnant_batches": stagnant_pages,
                    "stop_reason": "runtime_failed",
                    "stop_detail": type(exc).__name__,
                    "source_page": locals().get("page"),
                    "stagnation_basis": "candidate_identity",
                    "candidate_identities": sorted(seen_ids),
                },
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    stdout = json.dumps(
        {
            "keyword": args.keyword,
            "jsonl": str(jsonl_path),
            "records": len(records),
        },
        ensure_ascii=False,
        indent=2,
    )
    stdout_log.write_text(stdout, encoding="utf-8")
    stderr_log.write_text(stderr, encoding="utf-8")
    command_log.write_text(shlex.join(command), encoding="utf-8")
    output = summarize_output(batch_dir / platform_key / "data", args.keyword)
    status = "completed" if records and returncode == 0 and behavior_evidence_valid(behavior_evidence) else "failed"
    return {
        "platform": platform_key,
        "label": platform["label"],
        "status": status,
        "ok": status == "completed",
        "media_enabled": False,
        "video_enabled": False,
        "login_state": None,
        "behavior_evidence": behavior_evidence,
        "run": {
            "command": command,
            "command_text": shlex.join(command),
            "returncode": returncode,
            "timed_out": False,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
            "command_log": str(command_log),
            "stdout_tail": tail(stdout),
            "stderr_tail": tail(stderr),
        },
        "output": output,
    }


def _run_platform_without_policy(platform_key: str, args: argparse.Namespace, batch_dir: Path) -> dict[str, Any]:
    if platform_key == "bilibili":
        return run_bilibili_article_search(args, batch_dir)

    platform = PLATFORMS[platform_key]
    source_candidate_hard_limit = int(
        getattr(args, "source_candidate_hard_limit", args.candidate_hard_limit)
    )
    source_target_new_posts = int(getattr(args, "source_target_new_posts", args.target_new_posts))
    save_path = batch_dir / platform_key / "data"
    log_dir = batch_dir / "logs" / platform_key
    behavior_evidence_path = log_dir / "behavior_evidence.json"
    image_download_enabled = bool(args.download_images and platform_key == "xhs")
    cmd = [
        "uv",
        "run",
        "python",
        "main.py",
        "--platform",
        platform["mediacrawler"],
        "--lt",
        args.login_type,
        "--type",
        "detail" if getattr(args, "zhihu_detail_urls", []) else "search",
        "--keywords",
        args.keyword,
        "--get_comment",
        "false",
        "--get_sub_comment",
        "false",
        "--get_media",
        "true" if image_download_enabled else "false",
        "--headless",
        "false" if args.headed else "true",
        "--save_data_option",
        "jsonl",
        "--save_data_path",
        str(save_path),
        "--crawler_max_notes_count",
        str(source_candidate_hard_limit),
        "--start",
        str(args.start_page),
        "--max_concurrency_num",
        "1",
        "--enable_ip_proxy",
        "false",
    ]
    if getattr(args, "zhihu_detail_urls", []):
        cmd.extend(["--specified_id", ",".join(args.zhihu_detail_urls)])
    extra_env: dict[str, str] = {
        "TRIPPOSTCOLLECT_TARGET_NEW_POSTS": str(max(1, source_target_new_posts)),
        "TRIPPOSTCOLLECT_CANDIDATE_HARD_LIMIT": str(source_candidate_hard_limit),
        "TRIPPOSTCOLLECT_MAX_STAGNANT_BATCHES": str(max(1, args.max_stagnant_batches)),
        "TRIPPOSTCOLLECT_DB_PATH": str(Path(args.db).expanduser().resolve()),
        **behavior_environment(behavior_evidence_path, args.behavior_profile),
    }
    if args.discovery_job_id is not None:
        extra_env.update(
            {
                "TRIPPOSTCOLLECT_DISCOVERY_JOB_ID": str(args.discovery_job_id),
                "TRIPPOSTCOLLECT_DISCOVERY_QUERY_FINGERPRINT": str(
                    args.discovery_query_fingerprint or ""
                ),
                "TRIPPOSTCOLLECT_DISCOVERY_RUN_ID": str(args.discovery_run_id or ""),
                "TRIPPOSTCOLLECT_DISCOVERY_PLATFORM": platform_key,
                "TRIPPOSTCOLLECT_DISCOVERY_KEYWORD": args.keyword,
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_PAGE": str(args.start_page),
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET": str(args.start_offset),
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR": str(args.start_cursor or ""),
                "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES": str(
                    args.top_refresh_max_pages
                ),
                "TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED": (
                    "1" if args.discovery_source_exhausted else "0"
                ),
                "TRIPPOSTCOLLECT_DISCOVERY_CHECKPOINT_WRITE_DISABLED": (
                    "1" if args.no_checkpoint_write else "0"
                ),
            }
        )
    if platform_key == "xhs":
        extra_env.update(
            {
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_PAGE": str(args.start_page),
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR": str(args.start_cursor or ""),
                "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES": str(
                    args.top_refresh_max_pages
                ),
                "TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED": (
                    "1" if args.discovery_source_exhausted else "0"
                ),
            }
        )
    if args.resume_identities_path:
        extra_env["TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH"] = args.resume_identities_path
    login_state: dict[str, Any] | None = None
    if platform_key == "xhs":
        xhs_storage_path = Path(str(args.xhs_storage_state)).expanduser().resolve()
        xhs_storage_info = xhs_storage_snapshot_info(xhs_storage_path)
        login_state = {"ok": bool(xhs_storage_info.get("ok")), "storage_snapshot": xhs_storage_info}
        if args.login_type == "cookie" and not xhs_storage_info.get("ok"):
            reason = "missing_xhs_storage_state: run scripts/xhs_login.py for the selected account"
            run = skipped_command(cmd, log_dir, reason)
            output = summarize_output(save_path, args.keyword)
            return {
                "platform": platform_key,
                "label": platform["label"],
                "status": "failed",
                "ok": False,
                "media_enabled": image_download_enabled,
                "video_enabled": False,
                "login_state": {
                    "ok": False,
                    "reason": "missing_storage_snapshot",
                    "storage_snapshot": xhs_storage_info,
                },
                "run": run,
                "output": output,
            }
        cmd.extend(["--enable_cdp_mode", "true"])
        extra_env.update(
            {
                "TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS": "1",
                "TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL": "1",
                "TRIPPOSTCOLLECT_SHARE_CDP_PROFILE": "1",
                "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH": str(xhs_storage_path),
                "TRIPPOSTCOLLECT_XHS_PROFILE_DIR": str(Path(args.xhs_profile_dir).expanduser().resolve()),
                "TRIPPOSTCOLLECT_XHS_ACCOUNT_ID": str(args.xhs_account_id),
                "TRIPPOSTCOLLECT_XHS_DISCOVERY_TARGET_KEY": str(
                    args.xhs_discovery_target_key
                ),
                "TRIPPOSTCOLLECT_XHS_DISCOVERY_QUERY_FINGERPRINT": str(
                    args.xhs_discovery_query_fingerprint
                ),
                "TRIPPOSTCOLLECT_XHS_POST_INTERACTION": str(args.xhs_post_interaction),
                "TRIPPOSTCOLLECT_XHS_INITIAL_SETTLE_SECONDS": "12",
                "TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS": "180" if args.headed else "0",
                "TRIPPOSTCOLLECT_XHS_NAVIGATION_DEADLINE_SECONDS": "60",
                "TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_WAIT_SECONDS": "600",
                "TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_POLL_SECONDS": "2",
                "TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS": "90",
                "TRIPPOSTCOLLECT_XHS_QR_ATTEMPTS": "5",
            }
        )
    elif platform_key == "zhihu":
        cmd.extend(["--enable_cdp_mode", "true"])
        extra_env.update(
            {
                "TRIPPOSTCOLLECT_SHARE_CDP_PROFILE": "1",
                "TRIPPOSTCOLLECT_CLEAN_BROWSER_TABS": "1",
                "TRIPPOSTCOLLECT_ZHIHU_INITIAL_SETTLE_SECONDS": "8",
            }
        )
    elif platform_key == "douyin":
        extra_env.update(
            {
                "TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS": "1",
                "TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES": "1",
                "TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH": str(source_candidate_hard_limit),
                "TRIPPOSTCOLLECT_DOUYIN_CREATOR_SLEEP_SECONDS": "0.25",
            }
        )
    if platform_key in {"xhs", "zhihu"}:
        browser_path = discover_cdp_browser_path()
        if browser_path:
            extra_env.setdefault("TRIPPOSTCOLLECT_CUSTOM_BROWSER_PATH", browser_path)
        if platform_key == "zhihu":
            cookie_export = export_profile_cookies(platform_key, browser_path)
            if not cookie_export:
                reason = (
                    "missing_zhihu_login_cookies: run "
                    ".venv/bin/python scripts/mediacrawler_login_warmup.py --platforms zhihu "
                    "until d_c0/z_c0 are verified and snapshotted"
                )
                run = skipped_command(cmd, log_dir, reason)
                output = summarize_output(save_path, args.keyword)
                return {
                    "platform": platform_key,
                    "label": platform["label"],
                    "status": "failed",
                    "ok": False,
                    "media_enabled": image_download_enabled,
                    "video_enabled": False,
                    "login_state": {
                        "ok": False,
                        "reason": "missing_required_cookies",
                        "required_cookie_names": list(required_cookie_names(platform_key)),
                        "snapshot_path": str(cookie_snapshot_path(platform_key)),
                    },
                    "run": run,
                    "output": output,
                }
            extra_env["TRIPPOSTCOLLECT_COOKIES"] = str(cookie_export["cookie_header"])
            login_state = {"ok": True, **public_cookie_export(cookie_export)}
    timeout = args.timeout_per_platform
    if platform_key == "xhs" and (args.login_type == "qrcode" or args.headed):
        timeout = max(timeout, 420)
    if platform_key == "zhihu":
        timeout = max(timeout, 300)
    run = run_command(
        cmd,
        MEDIACRAWLER_DIR,
        timeout + HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS,
        log_dir,
        extra_env=extra_env,
    )
    behavior_evidence = load_behavior_evidence(behavior_evidence_path)
    output = summarize_output(save_path, args.keyword)
    status = "completed" if output["non_video_content_records"] > 0 and output["parse_errors"] == 0 else "failed"
    if output["content_records"] > 0 and output["non_video_content_records"] == 0 and output["video_like_records"] > 0:
        status = "skipped_video_only"
    if (
        output["content_records"] == 0
        and platform_key in {"xhs", "douyin"}
        and "skip video" in str(run.get("stderr_tail") or "").lower()
    ):
        status = "skipped_video_only"
    if run["timed_out"] and output["non_video_content_records"] > 0:
        status = "partial_completed"
    if not behavior_evidence_valid(behavior_evidence):
        status = "behavior_failed"
    return {
        "platform": platform_key,
        "label": platform["label"],
        "status": status,
        "ok": status in {"completed", "partial_completed", "skipped_video_only"},
        "media_enabled": image_download_enabled,
        "video_enabled": False,
        "login_state": login_state,
        "behavior_evidence": behavior_evidence,
        "run": run,
        "output": output,
    }


def run_platform(platform_key: str, args: argparse.Namespace, batch_dir: Path) -> dict[str, Any]:
    platform = PLATFORMS[platform_key]
    site = get_site(platform_key)
    log_dir = batch_dir / "logs" / platform_key
    save_path = batch_dir / platform_key / "data"
    policy_events: list[dict[str, Any]] = []
    try:
        with site_request_guard(site, label="mediacrawler:formal_platform_session") as event:
            policy_events.append(event)
            record = _run_platform_without_policy(platform_key, args, batch_dir)
    except CrawlPolicyBlocked as exc:
        policy_events.append(exc.event)
        reason = json.dumps(exc.event, ensure_ascii=False, sort_keys=True)
        return {
            "platform": platform_key,
            "label": platform["label"],
            "status": "policy_blocked",
            "ok": False,
            "media_enabled": False,
            "video_enabled": False,
            "login_state": None,
            "behavior_evidence": {
                "status": "skipped",
                "reason": "policy_blocked_before_behavior",
            },
            "policy_events": policy_events,
            "failure_classification": {
                "status": "retry_wait",
                "failure_type": "policy_blocked",
                "retryable": True,
                "wait_seconds": int(exc.event.get("wait_seconds") or 0),
                "reason": str(exc.event.get("reason") or "policy_blocked"),
            },
            "run": skipped_command(["crawl_policy", platform_key], log_dir, reason),
            "output": summarize_output(save_path, args.keyword),
        }
    except Exception as exc:
        reason = f"platform_session_failed:{type(exc).__name__}:{exc}"
        record = {
            "platform": platform_key,
            "label": platform["label"],
            "status": "failed",
            "ok": False,
            "media_enabled": False,
            "video_enabled": False,
            "login_state": None,
            "behavior_evidence": load_behavior_evidence(log_dir / "behavior_evidence.json"),
            "run": skipped_command(["mediacrawler", platform_key], log_dir, reason),
            "output": summarize_output(save_path, args.keyword),
        }

    record["policy_events"] = policy_events
    run = record.get("run") or {}
    evidence = record.get("behavior_evidence") or {}
    effective_exit_code = 0 if record.get("ok") else int(run.get("returncode") or 1)
    classification = classify_attempt(
        exit_code=effective_exit_code,
        stdout=str(run.get("stdout_tail") or ""),
        stderr=str(run.get("stderr_tail") or ""),
        meta={"structured_markers": evidence.get("visible_markers") or {}},
    )
    record["failure_classification"] = classification
    if classification.get("failure_type") in {"captcha_detected", "rate_limited", "blocked_or_forbidden"}:
        record["cooldown_event"] = record_site_cooldown(
            site,
            reason=str(classification.get("failure_type")),
            evidence=[str(classification.get("reason") or "")],
        )
    return record


def collect_behavior_validation(
    records: list[dict[str, Any]],
    platforms: list[str],
    keyword: str,
    xhs_post_interaction: str = "none",
) -> dict[str, Any]:
    latest_by_platform: dict[str, dict[str, Any]] = {}
    for record in reversed(records):
        platform_key = str(record.get("platform") or "")
        if platform_key in platforms and platform_key not in latest_by_platform:
            latest_by_platform[platform_key] = record

    platform_results: dict[str, Any] = {}
    for platform_key in platforms:
        record = latest_by_platform.get(platform_key) or {}
        evidence = record.get("behavior_evidence") or {}
        behavior_profile = str(evidence.get("profile") or "")
        expected_profile = "xhs_guarded" if platform_key == "xhs" else "social_high_risk"
        profile_ok = behavior_profile == expected_profile
        pacing_events = [
            item
            for item in evidence.get("request_pacing_events") or []
            if isinstance(item, dict)
        ]
        pacing_stages = {str(item.get("stage") or "") for item in pacing_events}
        continuity_events = [
            item
            for item in evidence.get("continuity_events") or []
            if isinstance(item, dict)
        ]
        continuity_stages = {
            str(item.get("stage") or "")
            for item in continuity_events
            if item.get("status") == "completed"
        }
        required_pacing_stages = (
            {"search_results", "note_detail", "creator_profile"}
            if platform_key == "xhs"
            else set()
        )
        pacing_ok = required_pacing_stages.issubset(pacing_stages)
        continuity_ok = platform_key != "xhs" or "search_results" in continuity_stages
        post_interactions = [
            item
            for item in evidence.get("post_interactions") or []
            if isinstance(item, dict)
        ]
        interaction_requested = platform_key == "xhs" and xhs_post_interaction != "none"
        interaction_ok = (not interaction_requested) or any(
            item.get("requested_mode") == xhs_post_interaction and item.get("status") == "completed"
            for item in post_interactions
        )
        policy_events = record.get("policy_events") or []
        policy_allowed = any(
            isinstance(event, dict)
            and event.get("allowed") is True
            and event.get("disabled") is not True
            for event in policy_events
        )
        behavior_url = str(evidence.get("url") or "")
        target_url_ok = bool(keyword) and keyword in unquote(behavior_url)
        platform_results[platform_key] = {
            "behavior_ok": (
                behavior_evidence_valid(evidence)
                and target_url_ok
                and profile_ok
                and pacing_ok
                and continuity_ok
            ),
            "behavior_status": str(evidence.get("status") or "missing"),
            "behavior_profile": behavior_profile,
            "behavior_profile_ok": profile_ok,
            "behavior_event_count": len(evidence.get("events") or []),
            "request_pacing_event_count": len(pacing_events),
            "request_pacing_stages": sorted(pacing_stages),
            "request_pacing_ok": pacing_ok,
            "continuity_event_count": len(continuity_events),
            "continuity_stages": sorted(continuity_stages),
            "continuity_ok": continuity_ok,
            "post_interaction_requested": interaction_requested,
            "post_interaction_mode": xhs_post_interaction if platform_key == "xhs" else "none",
            "post_interaction_ok": interaction_ok,
            "post_interactions": post_interactions,
            "behavior_url": behavior_url,
            "target_url_ok": target_url_ok,
            "policy_allowed": policy_allowed,
            "policy_event_count": len(policy_events),
            "evidence_path": str(evidence.get("evidence_path") or ""),
        }

    behavior_ok = bool(platform_results) and all(item["behavior_ok"] for item in platform_results.values())
    policy_ok = bool(platform_results) and all(item["policy_allowed"] for item in platform_results.values())
    return {
        "required": True,
        "ok": behavior_ok and policy_ok,
        "behavior_ok": behavior_ok,
        "policy_ok": policy_ok,
        "required_platforms": platforms,
        "platforms": platform_results,
    }


def latest_platform_result_counts(
    records: list[dict[str, Any]],
    platforms: list[str],
) -> dict[str, int]:
    latest_by_platform: dict[str, dict[str, Any]] = {}
    for record in records:
        platform_key = str(record.get("platform") or "")
        if platform_key in platforms:
            latest_by_platform[platform_key] = record
    latest_records = list(latest_by_platform.values())
    return {
        "ok_count": sum(1 for record in latest_records if record.get("ok")),
        "skipped_video_only_count": sum(
            1 for record in latest_records if record.get("status") == "skipped_video_only"
        ),
        "failed_count": sum(1 for record in latest_records if not record.get("ok")),
    }


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    lines = [
        "# MediaCrawler 结构化抓取摘要",
        "",
        f"- 时间：`{summary['captured_at']}`",
        f"- 关键词：`{summary['keyword']}`",
        f"- 输出目录：`{summary['batch_dir']}`",
        "",
        "| 平台 | 状态 | 图文内容记录 | 发帖时间记录 | 跳过视频记录 | 图片文件 | 视频文件(应为0) | 作者字段 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for record in summary["records"]:
        output = record["output"]
        lines.append(
            "| {label} | {status} | {contents} | {published_at} | {skipped_videos} | {images} | {videos} | {fields} |".format(
                label=record["label"],
                status=record["status"],
                contents=output["non_video_content_records"],
                published_at=output["published_at_records"],
                skipped_videos=output["video_like_records"],
                images=output["image_file_count"],
                videos=output["video_file_count"],
                fields=", ".join(output["author_like_fields"]) or "无",
            )
        )
    lines.extend(["", "## 样本", ""])
    for record in summary["records"]:
        lines.append(f"### {record['label']}")
        samples = record["output"].get("samples") or []
        if not samples:
            lines.append("")
            lines.append("无")
            lines.append("")
            continue
        for sample in samples[:2]:
            lines.append(f"- `{json.dumps(sample, ensure_ascii=False)}`")
        lines.append("")
    validation = summary.get("formal_validation") or {}
    if validation:
        pagination = validation.get("pagination_evidence") or {}
        lines.extend(
            [
                "## 正式校验",
                "",
                f"- 实际候选：`{validation.get('candidate_count', 0)}` / 硬上限 `{validation.get('candidate_hard_limit', 0)}`",
                f"- 有效新增图文：`{validation.get('valid_new_count', 0)}` / 目标 `{validation.get('target_new_posts', 0)}`",
                f"- 有效旧记录：`{validation.get('valid_existing_count', 0)}`（只更新，不计目标）",
                f"- 有效新增目标达成：`{validation.get('new_target_met', False)}`",
                f"- 停止原因：`{validation.get('stop_reason', '')}`",
                f"- 停止细节：`{validation.get('stop_detail', '')}`",
                f"- 已处理分页批次：`{pagination.get('batch_count', 0)}`",
                f"- 正常停止事件：`{pagination.get('stopped', False)}`",
                f"- 无效原因计数：`{json.dumps(validation.get('invalid_reason_counts') or {}, ensure_ascii=False, sort_keys=True)}`",
                "",
            ]
        )
    behavior_validation = summary.get("behavior_validation") or {}
    if behavior_validation:
        lines.extend(
            [
                "## 行为与策略门禁",
                "",
                f"- 总体通过：`{behavior_validation.get('ok', False)}`",
                f"- 人类行为证据通过：`{behavior_validation.get('behavior_ok', False)}`",
                f"- 请求预算与冷却门禁通过：`{behavior_validation.get('policy_ok', False)}`",
                f"- 平台证据：`{json.dumps(behavior_validation.get('platforms') or {}, ensure_ascii=False, sort_keys=True)}`",
                "",
            ]
        )
    import_result = summary.get("import_result") or {}
    if import_result:
        db_sync = import_result.get("db_sync") or {}
        db_value = import_result.get("db") or db_sync.get("db", "")
        lines.extend(
            [
                "## 入库",
                "",
                f"- 数据库：`{db_value}`",
                f"- JSONL 文件数：`{import_result.get('jsonl_files', 0)}`",
                f"- 处理行：`{import_result.get('processed_rows', 0)}`",
                f"- 新增行：`{import_result.get('inserted_rows', 0)}`",
                f"- 更新行：`{import_result.get('updated_rows', 0)}`",
                f"- 跳过记录：`{import_result.get('skipped', 0)}`",
                f"- 跳过视频记录：`{import_result.get('skipped_video', 0)}`",
                f"- 解析错误：`{import_result.get('parse_errors', 0)}`",
                f"- 同步任务：`{db_sync.get('synced_jobs', '')}`",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    if (
        args.target_new_posts < 0
        or args.candidate_hard_limit <= 0
        or args.max_stagnant_batches <= 0
    ):
        raise SystemExit("--candidate-hard-limit must be positive; other record limits cannot be negative")
    target_new_posts = args.target_new_posts
    candidate_hard_limit = args.candidate_hard_limit
    if target_new_posts > candidate_hard_limit:
        raise SystemExit("--target-new-posts cannot exceed --candidate-hard-limit")
    if args.required_fields_profile != "image_post_with_followers_v1":
        raise SystemExit(f"unsupported required fields profile: {args.required_fields_profile}")
    if not args.no_import and target_new_posts <= 0:
        raise SystemExit("--target-new-posts must be positive unless --no-import is used")
    if args.start_page <= 0:
        raise SystemExit("--start-page must be positive")
    if args.start_offset < 0 or args.top_refresh_max_pages < 0:
        raise SystemExit("--start-offset and --top-refresh-max-pages cannot be negative")
    discovery_values = (
        args.discovery_job_id,
        args.discovery_query_fingerprint,
        args.discovery_run_id,
    )
    if any(value not in (None, "") for value in discovery_values) and not all(
        value not in (None, "") for value in discovery_values
    ):
        raise SystemExit("discovery job id, query fingerprint and run id must be supplied together")
    if args.no_import:
        args.no_checkpoint_write = True
    xhs_managed_resume = bool(args.xhs_account_id and args.start_cursor)
    if (
        args.start_page > 1
        and not args.resume_summary
        and args.discovery_job_id is None
        and not xhs_managed_resume
    ):
        raise SystemExit("--start-page greater than 1 requires --resume-summary")
    if args.get_media:
        raise SystemExit("--get-media 已禁用：当前项目只采集图文内容和图片 URL，不下载媒体或视频。")
    ensure_prerequisites()
    platforms = selected_platforms(args.platforms)
    args.zhihu_detail_urls = []
    if args.zhihu_detail_urls_file:
        if platforms != ["zhihu"]:
            raise SystemExit("--zhihu-detail-urls-file requires --platforms zhihu only")
        if not args.no_import:
            raise SystemExit("--zhihu-detail-urls-file is diagnostic-only and requires --no-import")
        if args.resume_summary or args.start_page != 1 or args.discovery_job_id is not None:
            raise SystemExit("Zhihu detail diagnosis cannot use discovery resume arguments")
        args.zhihu_detail_urls = load_zhihu_detail_urls(args.zhihu_detail_urls_file)
        if len(args.zhihu_detail_urls) > candidate_hard_limit:
            raise SystemExit(
                "--candidate-hard-limit must cover every URL in --zhihu-detail-urls-file"
            )
    if "douyin" in platforms and args.start_page > 1 and not args.start_cursor:
        raise SystemExit(
            "Douyin continuation requires --start-cursor together with --start-page"
        )
    if "xhs" in platforms:
        if len(platforms) != 1:
            raise SystemExit("XHS must run alone through scripts/xhs_runner.py")
        if not args.xhs_account_id or not args.xhs_profile_dir or not args.xhs_storage_state:
            raise SystemExit("XHS requires --xhs-account-id, --xhs-profile-dir and --xhs-storage-state")
        if not args.xhs_discovery_target_key or not args.xhs_discovery_query_fingerprint:
            raise SystemExit("XHS requires runner-managed discovery target and query fingerprint")
        if args.behavior_profile != "xhs_guarded":
            raise SystemExit("XHS requires --behavior-profile xhs_guarded")
        if args.start_page > 1 and not args.start_cursor:
            raise SystemExit("XHS continuation requires --start-cursor together with --start-page")
        if not Path(args.xhs_profile_dir).expanduser().is_dir():
            raise SystemExit("XHS isolated profile directory does not exist")
        if not Path(args.xhs_storage_state).expanduser().is_file():
            raise SystemExit("XHS decrypted storage state does not exist")
    elif args.xhs_post_interaction != "none":
        raise SystemExit("--xhs-post-interaction is only supported for XHS")
    elif args.behavior_profile != "social_high_risk":
        raise SystemExit("generic MediaCrawler platforms require --behavior-profile social_high_risk")
    if args.download_images and any(platform != "xhs" for platform in platforms):
        raise SystemExit("--download-images 目前只允许 xhs：其他平台可能混入视频媒体。")
    resume_records: list[dict[str, Any]] = []
    resume_info: dict[str, Any] | None = None
    resume_identity_values: list[str] = []
    args.source_target_new_posts = max(1, target_new_posts or candidate_hard_limit)
    args.source_candidate_hard_limit = candidate_hard_limit
    if args.resume_summary:
        resume_path = Path(args.resume_summary).expanduser().resolve()
        try:
            resume_summary = json.loads(resume_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"cannot load --resume-summary: {exc}") from exc
        previous_keyword = str(resume_summary.get("keyword") or "")
        if (
            previous_keyword != args.keyword
            and city_name_from_keyword(previous_keyword) != city_name_from_keyword(args.keyword)
        ):
            raise SystemExit("--resume-summary keyword must resolve to the same city as --keyword")
        resume_records = [
            record
            for record in (resume_summary.get("records") or [])
            if isinstance(record, dict) and record.get("platform") in platforms
        ]
        if not resume_records:
            raise SystemExit("--resume-summary has no records for the selected platform")
        resume_validation, _ = collect_formal_records(
            {"records": resume_records},
            candidate_hard_limit=candidate_hard_limit,
            target_new_posts=0,
            db_path=args.db,
            enforce_candidate_limit=False,
        )
        prior_new_count = int(resume_validation.get("valid_new_count") or 0)
        resume_identity_values = [
            identity.split(":id:", 1)[1]
            for identity in (
                list(resume_validation.get("new_identities") or [])
                + list(resume_validation.get("existing_identities") or [])
            )
            if ":id:" in identity
        ]
        previous_candidate_count = int(
            (resume_summary.get("formal_validation") or {}).get("candidate_count") or 0
        )
        consumed_candidates = max(
            int(resume_validation.get("candidate_count") or 0),
            previous_candidate_count,
        )
        remaining_target = max(0, target_new_posts - prior_new_count)
        if remaining_target == 0:
            raise SystemExit("--resume-summary already meets the configured new-post target")
        args.source_target_new_posts = remaining_target
        args.source_candidate_hard_limit = candidate_hard_limit
        resume_info = {
            "summary_path": str(resume_path),
            "valid_new_count": prior_new_count,
            "candidate_count": consumed_candidates,
            "remaining_target_new_posts": remaining_target,
            "run_candidate_hard_limit": candidate_hard_limit,
            "start_page": args.start_page,
        }

    batch_dir = ensure_dir(Path(args.output_dir).expanduser() / utc_stamp()).resolve()
    args.resume_identities_path = None
    if resume_identity_values:
        resume_identities_path = batch_dir / "resume_identities.json"
        resume_identities_path.write_text(
            json.dumps(sorted(set(resume_identity_values)), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        args.resume_identities_path = str(resume_identities_path)

    records = list(resume_records)
    for platform_key in platforms:
        print(f"[mediacrawler] {platform_key}", flush=True)
        records.append(run_platform(platform_key, args, batch_dir))

    result_counts = latest_platform_result_counts(records, platforms)
    summary = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "keyword": args.keyword,
        "batch_dir": str(batch_dir),
        "records": records,
        **result_counts,
    }
    behavior_validation = collect_behavior_validation(
        records,
        platforms,
        args.keyword,
        args.xhs_post_interaction,
    )
    summary["behavior_validation"] = behavior_validation
    if resume_info:
        summary["resume"] = resume_info
    pagination_evidence = load_pagination_evidence(
        os.environ.get("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", "").strip()
    )
    summary["pagination_evidence"] = pagination_evidence
    validation, valid_records = collect_formal_records(
        summary,
        candidate_hard_limit=candidate_hard_limit,
        target_new_posts=target_new_posts,
        db_path=args.db,
        pagination_evidence=pagination_evidence,
        enforce_candidate_limit=not bool(resume_info),
    )
    validation["content_new_target_met"] = bool(validation.get("new_target_met"))
    validation["behavior_evidence_ok"] = bool(behavior_validation.get("behavior_ok"))
    validation["policy_evidence_ok"] = bool(behavior_validation.get("policy_ok"))
    if not behavior_validation["ok"]:
        validation["new_target_met"] = False
        validation["stop_reason"] = (
            "behavior_evidence_failed"
            if not behavior_validation["behavior_ok"]
            else "crawl_policy_evidence_failed"
        )
    summary["required_fields_profile"] = args.required_fields_profile
    summary["formal_validation"] = validation
    if args.no_import:
        summary["import_result"] = {"skipped": True, "reason": "no_import"}
    elif target_new_posts > 0 and not validation["new_target_met"]:
        summary["import_result"] = {
            "skipped": True,
            "reason": validation["stop_reason"],
            "processed_rows": 0,
            "inserted_rows": 0,
            "updated_rows": 0,
        }
    else:
        summary["import_result"] = import_valid_records(
            summary,
            valid_records,
            Path(args.db).expanduser(),
        )
    inserted = int((summary.get("import_result") or {}).get("inserted_rows") or 0)
    summary["target_new_posts"] = target_new_posts
    summary["import_new_target_met"] = (
        validation["new_target_met"]
        and (args.no_import or target_new_posts <= 0 or inserted >= target_new_posts)
    )
    if not summary["import_new_target_met"]:
        summary["failure_reason"] = (
            "import_new_target_not_met: "
            f"valid_new={validation['valid_new_count']} required={target_new_posts} "
            f"stop_reason={validation['stop_reason']} "
            f"behavior_ok={behavior_validation['behavior_ok']} "
            f"policy_ok={behavior_validation['policy_ok']}"
        )
    summary_path = batch_dir / "summary.json"
    report_path = batch_dir / "summary.md"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    checkpoint_ok = True
    try:
        summary["discovery_checkpoint"] = persist_discovery_checkpoint(
            args,
            platforms[0],
            pagination_evidence,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        checkpoint_ok = False
        summary["discovery_checkpoint"] = {
            "skipped": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        summary["failure_reason"] = "discovery_checkpoint_write_failed"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(summary, report_path)
    print(json.dumps({"summary": str(summary_path), "report": str(report_path), "batch_dir": str(batch_dir), **summary}, ensure_ascii=False, indent=2))
    return (
        0
        if summary["failed_count"] == 0
        and summary["import_new_target_met"]
        and checkpoint_ok
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
