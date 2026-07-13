#!/usr/bin/env python3
"""MediaCrawler-first structured crawl entrypoint."""

from __future__ import annotations

import argparse
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
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from execution_state import FrozenExecutionState
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
from browser_runtime import browser_launch_environment, browser_runtime_args


ROOT = PROJECT_ROOT
DEFAULT_OUTPUT = MEDIACRAWLER_RUNS_OUTPUT
COOKIE_SNAPSHOT_FILENAME = "trippostcollect_cookie_snapshot.json"
STORAGE_SNAPSHOT_FILENAME = "trippostcollect_storage_state.json"
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
    parser.add_argument("--platforms", nargs="+", default=["xhs", "weibo", "douyin"], help="bilibili xhs weibo douyin zhihu")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT), help="Output root.")
    parser.add_argument("--timeout-per-platform", type=int, default=180, help="Timeout per MediaCrawler platform.")
    parser.add_argument("--login-type", default="cookie", choices=("cookie", "qrcode", "phone"), help="MediaCrawler login type.")
    parser.add_argument("--get-media", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--download-images", action="store_true", help="Download image files for supported image-only platforms. Videos remain disabled.")
    parser.add_argument("--headed", action="store_true", help="Run browser with visible UI.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path for web_posts import.")
    parser.add_argument("--target-valid-posts", type=int, default=0, help="Required unique records passing the formal field profile.")
    parser.add_argument("--candidate-hard-limit", type=int, required=True, help="Maximum actual content candidates processed for validation.")
    parser.add_argument("--required-fields-profile", default="image_post_with_followers_v1")
    parser.add_argument("--max-stagnant-batches", type=int, default=3)
    parser.add_argument("--no-import", action="store_true", help="Do not import MediaCrawler JSONL records into SQLite.")
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%z")


def selected_platforms(values: list[str]) -> list[str]:
    unknown = sorted(set(values) - set(PLATFORMS))
    if unknown:
        raise SystemExit(f"Unsupported MediaCrawler platform in this project: {', '.join(unknown)}")
    return values


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


def storage_snapshot_path(platform_key: str) -> Path:
    return profile_dir_for(platform_key) / STORAGE_SNAPSHOT_FILENAME


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
    "valid_unique_count",
    "new_valid_count",
    "source_page",
    "source_offset",
    "source_cursor",
    "next_cursor",
    "source_has_more",
    "raw_batch_count",
    "raw_response_count",
    "stagnant_batches",
    "stop_reason",
    "stop_detail",
)


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
        "stop_event": stopped_details,
    }


def collect_formal_records(
    summary: dict[str, Any],
    *,
    candidate_hard_limit: int,
    target_valid_posts: int,
    pagination_evidence: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
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
                    if candidate_count >= candidate_hard_limit:
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
                    selected.append(
                        {
                            "platform": platform_key,
                            "record": record,
                            "identity": validation["identity"],
                            "source_path": str(path),
                            "line_number": line_number,
                        }
                    )
                    if target_valid_posts > 0 and len(selected) >= target_valid_posts:
                        stop = True
                        break
            if stop:
                break
        if stop:
            break

    output_record_count = candidate_count
    pagination_evidence = pagination_evidence or {}
    candidate_count = max(candidate_count, int(pagination_evidence.get("candidate_count") or 0))
    target_met = target_valid_posts <= 0 or len(selected) >= target_valid_posts
    if target_met and target_valid_posts > 0:
        stop_reason = "target_met"
    elif candidate_count >= candidate_hard_limit:
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
        "output_record_count": output_record_count,
        "target_valid_posts": target_valid_posts,
        "valid_unique_count": len(selected),
        "target_met": target_met,
        "stop_reason": stop_reason,
        "stop_detail": str(pagination_evidence.get("stop_detail") or ""),
        "pagination_evidence": pagination_evidence,
        "parse_errors": parse_errors,
        "invalid_reason_counts": dict(sorted(reason_counts.items())),
        "valid_identities": [item["identity"] for item in selected],
        "valid_samples": [
            {
                "identity": item["identity"],
                "source_path": item["source_path"],
                "line_number": item["line_number"],
            }
            for item in selected[:5]
        ],
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
    valid_count = 0
    candidate_count = 0
    stagnant_pages = 0
    state_path = os.environ.get("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", "").strip()
    stderr = ""
    returncode = 0
    try:
        max_records = args.candidate_hard_limit
        target_valid = max(1, int(args.target_valid_posts or max_records))
        browser_path = discover_cdp_browser_path()
        cookie_export = export_profile_cookies(platform_key, browser_path)
        cookie_header = str((cookie_export or {}).get("cookie_header") or "")
        wbi_keys = fetch_bilibili_wbi_keys(cookie_header)
        page = 1
        while candidate_count < max_records:
            page_items = fetch_bilibili_article_page(
                args.keyword,
                page,
                wbi_keys=wbi_keys,
                cookie_header=cookie_header,
            )
            if not page_items:
                if state_path:
                    FrozenExecutionState(state_path).append_event(
                        "adaptive_search_stopped",
                        {
                            "platform": platform_key,
                            "candidate_count": candidate_count,
                            "valid_unique_count": valid_count,
                            "target": target_valid,
                            "hard_limit": max_records,
                            "stagnant_batches": stagnant_pages,
                            "stop_reason": "source_exhausted",
                            "stop_detail": "empty_page",
                            "pages_fetched": page - 1,
                            "source_page": page,
                            "raw_batch_count": 0,
                        },
                    )
                break
            valid_before = valid_count
            processed_in_batch = 0
            for item in page_items:
                if candidate_count >= max_records:
                    break
                candidate_count += 1
                processed_in_batch += 1
                normalized = normalize_bilibili_article_record(item, args.keyword)
                if not normalized:
                    continue
                creator_id = str(normalized.get("user_id") or "")
                if creator_id:
                    if creator_id not in follower_cache:
                        try:
                            follower_cache[creator_id] = fetch_bilibili_follower_count(creator_id, cookie_header)
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
                post_id = str(normalized.get("content_id") or "")
                if post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                records.append(normalized)
                validation = validate_formal_record(platform_key, normalized, valid_seen)
                if validation["valid"]:
                    valid_seen.add(str(validation["identity"]))
                    valid_count += 1
                if candidate_count >= max_records or valid_count >= target_valid:
                    break
            stagnant_pages = stagnant_pages + 1 if valid_count == valid_before else 0
            if valid_count >= target_valid:
                batch_stop_reason = "target_met"
            elif candidate_count >= max_records:
                batch_stop_reason = "candidate_hard_limit_reached"
            elif stagnant_pages >= max(1, args.max_stagnant_batches):
                batch_stop_reason = "stagnated"
            else:
                batch_stop_reason = "continue"
            if state_path:
                frozen_state = FrozenExecutionState(state_path)
                frozen_state.append_event(
                    "adaptive_batch_completed",
                    {
                        "platform": platform_key,
                        "batch_no": page,
                        "candidate_count": candidate_count,
                        "valid_unique_count": valid_count,
                        "new_valid_count": valid_count - valid_before,
                        "stagnant_batches": stagnant_pages,
                        "target": target_valid,
                        "hard_limit": max_records,
                        "stop_reason": batch_stop_reason,
                        "source_page": page,
                        "source_has_more": None,
                        "raw_batch_count": processed_in_batch,
                        "raw_response_count": len(page_items),
                    },
                )
                if batch_stop_reason != "continue":
                    frozen_state.append_event(
                        "adaptive_search_stopped",
                        {
                            "platform": platform_key,
                            "candidate_count": candidate_count,
                            "valid_unique_count": valid_count,
                            "target": target_valid,
                            "hard_limit": max_records,
                            "stagnant_batches": stagnant_pages,
                            "stop_reason": batch_stop_reason,
                            "pages_fetched": page,
                            "source_page": page,
                            "source_has_more": None,
                            "raw_batch_count": processed_in_batch,
                            "raw_response_count": len(page_items),
                        },
                    )
            if (
                candidate_count >= max_records
                or valid_count >= target_valid
                or stagnant_pages >= max(1, args.max_stagnant_batches)
            ):
                break
            page += 1
    except Exception as exc:
        returncode = 1
        stderr = repr(exc)
        if state_path:
            FrozenExecutionState(state_path).append_event(
                "adaptive_search_stopped",
                {
                    "platform": platform_key,
                    "candidate_count": candidate_count,
                    "valid_unique_count": valid_count,
                    "target": max(1, int(args.target_valid_posts or args.candidate_hard_limit)),
                    "hard_limit": args.candidate_hard_limit,
                    "stagnant_batches": stagnant_pages,
                    "stop_reason": "runtime_failed",
                    "stop_detail": type(exc).__name__,
                    "source_page": locals().get("page"),
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
    status = "completed" if records and returncode == 0 else "failed"
    return {
        "platform": platform_key,
        "label": platform["label"],
        "status": status,
        "ok": status == "completed",
        "media_enabled": False,
        "video_enabled": False,
        "login_state": None,
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


def run_platform(platform_key: str, args: argparse.Namespace, batch_dir: Path) -> dict[str, Any]:
    if platform_key == "bilibili":
        return run_bilibili_article_search(args, batch_dir)

    platform = PLATFORMS[platform_key]
    save_path = batch_dir / platform_key / "data"
    log_dir = batch_dir / "logs" / platform_key
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
        "search",
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
        str(args.candidate_hard_limit),
        "--max_concurrency_num",
        "1",
        "--enable_ip_proxy",
        "false",
    ]
    extra_env: dict[str, str] = {
        "TRIPPOSTCOLLECT_TARGET_VALID_POSTS": str(max(1, args.target_valid_posts or args.candidate_hard_limit)),
        "TRIPPOSTCOLLECT_CANDIDATE_HARD_LIMIT": str(args.candidate_hard_limit),
        "TRIPPOSTCOLLECT_MAX_STAGNANT_BATCHES": str(max(1, args.max_stagnant_batches)),
    }
    login_state: dict[str, Any] | None = None
    if platform_key == "xhs":
        xhs_storage_path = storage_snapshot_path(platform_key)
        xhs_storage_info = xhs_storage_snapshot_info(xhs_storage_path)
        login_state = {"ok": bool(xhs_storage_info.get("ok")), "storage_snapshot": xhs_storage_info}
        if args.login_type == "cookie" and not xhs_storage_info.get("ok"):
            reason = (
                "missing_xhs_storage_state: run "
                ".venv/bin/python scripts/mediacrawler_login_warmup.py --platforms xhs "
                "until profile_ui is verified and trippostcollect_storage_state.json is snapshotted"
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
                "TRIPPOSTCOLLECT_XHS_INITIAL_SETTLE_SECONDS": "12",
                "TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS": "180" if args.headed else "0",
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
                "TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH": str(args.candidate_hard_limit),
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
    run = run_command(cmd, MEDIACRAWLER_DIR, timeout, log_dir, extra_env=extra_env)
    output = summarize_output(save_path, args.keyword)
    status = "completed" if output["non_video_content_records"] > 0 and output["parse_errors"] == 0 else "failed"
    if output["content_records"] > 0 and output["non_video_content_records"] == 0 and output["video_like_records"] > 0:
        status = "skipped_video_only"
    if run["timed_out"] and output["non_video_content_records"] > 0:
        status = "partial_completed"
    return {
        "platform": platform_key,
        "label": platform["label"],
        "status": status,
        "ok": status in {"completed", "partial_completed", "skipped_video_only"},
        "media_enabled": image_download_enabled,
        "video_enabled": False,
        "login_state": login_state,
        "run": run,
        "output": output,
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
                f"- 有效唯一图文：`{validation.get('valid_unique_count', 0)}` / 目标 `{validation.get('target_valid_posts', 0)}`",
                f"- 目标达成：`{validation.get('target_met', False)}`",
                f"- 停止原因：`{validation.get('stop_reason', '')}`",
                f"- 停止细节：`{validation.get('stop_detail', '')}`",
                f"- 已处理分页批次：`{pagination.get('batch_count', 0)}`",
                f"- 正常停止事件：`{pagination.get('stopped', False)}`",
                f"- 无效原因计数：`{json.dumps(validation.get('invalid_reason_counts') or {}, ensure_ascii=False, sort_keys=True)}`",
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
        args.target_valid_posts < 0
        or args.candidate_hard_limit <= 0
        or args.max_stagnant_batches <= 0
    ):
        raise SystemExit("--candidate-hard-limit must be positive; other record limits cannot be negative")
    target_valid_posts = args.target_valid_posts
    candidate_hard_limit = args.candidate_hard_limit
    if target_valid_posts > candidate_hard_limit:
        raise SystemExit("--target-valid-posts cannot exceed --candidate-hard-limit")
    if args.required_fields_profile != "image_post_with_followers_v1":
        raise SystemExit(f"unsupported required fields profile: {args.required_fields_profile}")
    if not args.no_import and target_valid_posts <= 0:
        raise SystemExit("--target-valid-posts must be positive unless --no-import is used")
    if args.get_media:
        raise SystemExit("--get-media 已禁用：当前项目只采集图文内容和图片 URL，不下载媒体或视频。")
    ensure_prerequisites()
    platforms = selected_platforms(args.platforms)
    if args.download_images and any(platform != "xhs" for platform in platforms):
        raise SystemExit("--download-images 目前只允许 xhs：其他平台可能混入视频媒体。")
    batch_dir = ensure_dir(Path(args.output_dir).expanduser() / utc_stamp()).resolve()

    records = []
    for platform_key in platforms:
        print(f"[mediacrawler] {platform_key}", flush=True)
        records.append(run_platform(platform_key, args, batch_dir))

    summary = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "keyword": args.keyword,
        "batch_dir": str(batch_dir),
        "ok_count": sum(1 for record in records if record["ok"]),
        "skipped_video_only_count": sum(1 for record in records if record["status"] == "skipped_video_only"),
        "failed_count": sum(1 for record in records if not record["ok"]),
        "records": records,
    }
    pagination_evidence = load_pagination_evidence(
        os.environ.get("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", "").strip()
    )
    summary["pagination_evidence"] = pagination_evidence
    validation, valid_records = collect_formal_records(
        summary,
        candidate_hard_limit=candidate_hard_limit,
        target_valid_posts=target_valid_posts,
        pagination_evidence=pagination_evidence,
    )
    summary["required_fields_profile"] = args.required_fields_profile
    summary["formal_validation"] = validation
    if args.no_import:
        summary["import_result"] = {"skipped": True, "reason": "no_import"}
    elif target_valid_posts > 0 and not validation["target_met"]:
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
    processed = int((summary.get("import_result") or {}).get("processed_rows") or 0)
    summary["target_valid_posts"] = target_valid_posts
    summary["import_target_met"] = (
        validation["target_met"]
        and (args.no_import or target_valid_posts <= 0 or processed >= target_valid_posts)
    )
    if not summary["import_target_met"]:
        summary["failure_reason"] = (
            "import_target_not_met: "
            f"valid_unique={validation['valid_unique_count']} required={target_valid_posts} "
            f"stop_reason={validation['stop_reason']}"
        )
    summary_path = batch_dir / "summary.json"
    report_path = batch_dir / "summary.md"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(summary, report_path)
    print(json.dumps({"summary": str(summary_path), "report": str(report_path), "batch_dir": str(batch_dir), **summary}, ensure_ascii=False, indent=2))
    return 0 if summary["failed_count"] == 0 and summary["import_target_met"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
