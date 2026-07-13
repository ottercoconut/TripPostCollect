#!/usr/bin/env python3
"""Batch MediaCrawler validation with immediate per-batch field checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mediacrawler_crawl import (
    MEDIACRAWLER_DIR,
    PLATFORMS,
    canonical_url_for_record,
    content_text_for_record,
    dedupe_image_urls,
    discover_cdp_browser_path,
    ensure_dir,
    ensure_prerequisites,
    first_value,
    is_video_record,
    post_id_for_record,
    published_at_for_record,
    run_command,
    summarize_output,
    utc_stamp,
)
from browser_runtime import browser_launch_environment, browser_runtime_args
from trippostcollect.core.paths import OUTPUTS_ROOT


DEFAULT_OUTPUT = OUTPUTS_ROOT / "mediacrawler_batch_validation"
REQUIRED_METRIC_KEYS = ("liked_count", "collected_count", "comment_count", "share_count")
EXPECTED_FOLLOWER_SOURCES = {"xhs": "creator_profile", "douyin": "creator_profile"}
XHS_HOME_URL = "https://www.xiaohongshu.com"
XHS_COOKIE_URLS = ("https://www.xiaohongshu.com", "https://www.rednote.com")
XHS_SECURITY_TEXTS = ("请通过验证", "安全验证", "验证码", "身份验证", "操作频繁", "环境异常", "风险")
XHS_LOGIN_TEXTS = ("扫码登录", "二维码", "打开小红书扫一扫", "确认登录", "登录确认", "手机号登录")
XHS_STORAGE_SNAPSHOT_FILENAME = "trippostcollect_storage_state.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run xhs/douyin in 10-record batches and abort on incomplete fields.")
    parser.add_argument("--keyword", default="济南旅游")
    parser.add_argument("--platforms", nargs="+", default=["xhs", "douyin"], choices=("xhs", "douyin"))
    parser.add_argument("--target-count", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--timeout-per-batch", type=int, default=600)
    parser.add_argument("--login-type", default="cookie", choices=("cookie", "qrcode", "phone"))
    parser.add_argument("--headless", action="store_true", help="Use only for explicit headless validation.")
    parser.add_argument(
        "--skip-xhs-preflight",
        action="store_true",
        help="Skip the Xiaohongshu browser settle/login-state preflight.",
    )
    parser.add_argument(
        "--xhs-initial-delay-seconds",
        type=float,
        default=15.0,
        help="Visible-browser settle delay after opening Xiaohongshu before running batches.",
    )
    parser.add_argument(
        "--xhs-preflight-timeout",
        type=int,
        default=300,
        help="Maximum seconds to wait for Xiaohongshu login/security confirmation during preflight.",
    )
    parser.add_argument(
        "--xhs-login-wait-seconds",
        type=int,
        default=180,
        help="Maximum seconds MediaCrawler itself waits if Xiaohongshu opens a login/security checkpoint.",
    )
    parser.add_argument(
        "--xhs-preflight-only",
        action="store_true",
        help="Run only the Xiaohongshu visual/session preflight and exit.",
    )
    parser.add_argument(
        "--xhs-screenshot-interval",
        type=float,
        default=5.0,
        help="Seconds between Xiaohongshu preflight screenshots. 0 disables periodic screenshots.",
    )
    parser.add_argument(
        "--xhs-candidate-multiplier",
        type=float,
        default=2.0,
        help="Candidate records requested per Xiaohongshu batch, relative to --batch-size.",
    )
    parser.add_argument(
        "--douyin-candidate-multiplier",
        type=float,
        default=1.0,
        help="Candidate records requested per Douyin batch, relative to --batch-size.",
    )
    parser.add_argument(
        "--xhs-inter-batch-delay-seconds",
        type=float,
        default=20.0,
        help="Cooldown between Xiaohongshu batches to reduce checkpoint/CAPTCHA triggers.",
    )
    return parser.parse_args()


def content_records(save_path: Path, platform_key: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(save_path.rglob("*.jsonl")):
        if "_contents_" not in path.name:
            continue
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError as exc:
                    records.append(
                        {
                            "__invalid_json__": True,
                            "__source_path__": str(path),
                            "__line_number__": line_number,
                            "__error__": str(exc),
                        }
                    )
                    continue
                if isinstance(record, dict):
                    record["__source_path__"] = str(path)
                    record["__line_number__"] = line_number
                    record["__platform_key__"] = platform_key
                    records.append(record)
    return records


def record_identity(platform_key: str, record: dict[str, Any]) -> str:
    post_id = post_id_for_record(platform_key, record)
    if post_id:
        return f"{platform_key}:id:{post_id}"
    canonical_url = canonical_url_for_record(platform_key, record)
    if canonical_url:
        return f"{platform_key}:url:{canonical_url}"
    return ""


def metric_value(record: dict[str, Any], key: str) -> Any:
    value = first_value(record, key)
    return value


def validate_record(platform_key: str, record: dict[str, Any], seen: set[str]) -> tuple[str | None, dict[str, Any]]:
    identity = record_identity(platform_key, record)
    canonical_url = canonical_url_for_record(platform_key, record)
    content_text = content_text_for_record(platform_key, record).strip()
    images = [item for item in dedupe_image_urls(record) if item.get("role") == "content"]
    author_id = first_value(record, "user_id", "creator_id", "creator_hash", "author_id")
    author_name = first_value(record, "nickname", "user_nickname", "user_name", "author_name")
    published_at = published_at_for_record(record)
    followers_count = first_value(
        record,
        "author_followers_count",
        "followers_count",
        "follower_count",
        "fans_count",
        "fans",
    )
    followers_observed = record.get("followers_observed") is True
    followers_source = first_value(record, "author_followers_source", "followers_source")

    missing: list[str] = []
    if record.get("__invalid_json__"):
        missing.append("valid_json")
    if not identity:
        missing.append("platform_post_id_or_url")
    if identity and identity in seen:
        missing.append("unique_identity")
    if is_video_record(platform_key, record):
        missing.append("non_video_image_text_record")
    if not canonical_url:
        missing.append("canonical_url")
    if not content_text:
        missing.append("content_text_or_title")
    if not author_id:
        missing.append("author_platform_id")
    if not author_name:
        missing.append("author_display_name")
    if not published_at:
        missing.append("published_at")
    if not images:
        missing.append("content_image_url")
    if followers_count in (None, ""):
        missing.append("author_followers_count")
    if not followers_observed:
        missing.append("followers_observed")
    if followers_source in (None, "", "missing"):
        missing.append("author_followers_source")
    elif followers_source != EXPECTED_FOLLOWER_SOURCES.get(platform_key):
        missing.append("trusted_author_followers_source")

    missing_metrics = [key for key in REQUIRED_METRIC_KEYS if metric_value(record, key) in (None, "")]
    if missing_metrics:
        missing.append("interaction_metrics:" + ",".join(missing_metrics))

    detail = {
        "identity": identity,
        "source_path": record.get("__source_path__", ""),
        "line_number": record.get("__line_number__"),
        "missing": missing,
        "canonical_url": canonical_url,
        "content_length": len(content_text),
        "author_id_present": bool(author_id),
        "author_name_present": bool(author_name),
        "published_at": published_at,
        "content_image_count": len(images),
        "followers_count": followers_count,
        "followers_observed": followers_observed,
        "followers_source": followers_source,
        "metric_values": {key: metric_value(record, key) for key in REQUIRED_METRIC_KEYS},
        "sample": {
            "title": first_value(record, "title"),
            "desc": first_value(record, "desc", "content", "content_text"),
            "note_id": first_value(record, "note_id"),
            "aweme_id": first_value(record, "aweme_id"),
            "nickname": author_name,
        },
    }
    return (None if not missing else "missing_required_fields", detail)


def validate_batch(
    *,
    platform_key: str,
    batch_size: int,
    save_path: Path,
    seen: set[str],
) -> dict[str, Any]:
    raw_records = content_records(save_path, platform_key)
    selected_details: list[dict[str, Any]] = []
    surplus_valid_details: list[dict[str, Any]] = []
    skipped_candidates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    local_seen: set[str] = set()

    for record in raw_records:
        reason, detail = validate_record(platform_key, record, seen | local_seen)
        missing = set(detail.get("missing") or [])
        is_duplicate = "unique_identity" in missing
        is_video = "non_video_image_text_record" in missing

        if is_duplicate:
            skipped_candidates.append({"reason": "duplicate_identity", **detail})
            continue
        if is_video:
            skipped_candidates.append({"reason": "video_candidate", **detail})
            continue
        if reason:
            failures.append({"reason": reason, **detail})
            continue
        identity = str(detail["identity"])
        if len(selected_details) < batch_size:
            local_seen.add(identity)
            selected_details.append(detail)
        else:
            surplus_valid_details.append(detail)

    if len(selected_details) != batch_size:
        failures.append(
            {
                "reason": "batch_effective_count_mismatch",
                "expected": batch_size,
                "actual": len(selected_details),
                "raw_content_records": len(raw_records),
                "skipped_candidate_count": len(skipped_candidates),
                "surplus_valid_count": len(surplus_valid_details),
            }
        )

    if not failures:
        seen.update(local_seen)

    return {
        "ok": not failures,
        "expected": batch_size,
        "raw_content_records": len(raw_records),
        "valid_count": len(selected_details),
        "candidate_valid_count": len(selected_details) + len(surplus_valid_details),
        "skipped_candidate_count": len(skipped_candidates),
        "surplus_valid_count": len(surplus_valid_details),
        "skipped_candidates": skipped_candidates[:20],
        "surplus_valid_identities": [item["identity"] for item in surplus_valid_details[:20]],
        "failures": failures[:20],
        "valid_identities": [item["identity"] for item in selected_details],
        "valid_samples": selected_details[:3],
    }


def command_for_batch(
    *,
    platform_key: str,
    keyword: str,
    batch_size: int,
    batch_no: int,
    page_start: int,
    save_path: Path,
    login_type: str,
    headless: bool,
    xhs_initial_delay_seconds: float,
    xhs_login_wait_seconds: int,
    xhs_candidate_multiplier: float,
    douyin_candidate_multiplier: float,
) -> tuple[list[str], dict[str, str]]:
    platform = PLATFORMS[platform_key]
    crawler_candidate_count = batch_size
    if platform_key == "xhs":
        crawler_candidate_count = max(batch_size, int(round(batch_size * max(1.0, xhs_candidate_multiplier))))
    elif platform_key == "douyin":
        crawler_candidate_count = max(
            batch_size,
            int(round(batch_size * batch_no * max(1.0, douyin_candidate_multiplier))),
        )
    cmd = [
        "uv",
        "run",
        "python",
        "main.py",
        "--platform",
        platform["mediacrawler"],
        "--lt",
        login_type,
        "--type",
        "search",
        "--start",
        str(page_start),
        "--keywords",
        keyword,
        "--get_comment",
        "false",
        "--get_sub_comment",
        "false",
        "--get_media",
        "false",
        "--headless",
        "true" if headless else "false",
        "--save_data_option",
        "jsonl",
        "--save_data_path",
        str(save_path),
        "--crawler_max_notes_count",
        str(crawler_candidate_count),
        "--max_concurrency_num",
        "1",
        "--enable_ip_proxy",
        "false",
    ]
    env: dict[str, str] = {}
    if platform_key == "xhs":
        profile_dir = MEDIACRAWLER_DIR / "browser_data" / "xhs_user_data_dir"
        storage_state_path = profile_dir / XHS_STORAGE_SNAPSHOT_FILENAME
        cmd.extend(["--enable_cdp_mode", "true"])
        env.update(
            {
                "TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS": "1",
                "TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL": "1",
                "TRIPPOSTCOLLECT_SHARE_CDP_PROFILE": "1",
                "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH": str(storage_state_path),
                "TRIPPOSTCOLLECT_XHS_INITIAL_SETTLE_SECONDS": str(max(0.0, xhs_initial_delay_seconds)),
                "TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS": str(max(0, xhs_login_wait_seconds)),
                "TRIPPOSTCOLLECT_XHS_SEARCH_MAX_PAGES_PER_BATCH": "5",
                "TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS": "90",
                "TRIPPOSTCOLLECT_XHS_QR_ATTEMPTS": "5",
            }
        )
        browser_path = discover_cdp_browser_path()
        if browser_path:
            env["TRIPPOSTCOLLECT_CUSTOM_BROWSER_PATH"] = browser_path
    elif platform_key == "douyin":
        env.update(
            {
                "TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS": "1",
                "TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES": "1",
                "TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH": str(crawler_candidate_count),
                "TRIPPOSTCOLLECT_DOUYIN_CREATOR_SLEEP_SECONDS": "0.25",
            }
        )
    return cmd, env


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def safe_locator_count(page: Any, selector: str) -> int:
    try:
        return await page.locator(selector).count()
    except Exception:
        return 0


async def xhs_page_snapshot(page: Any) -> dict[str, Any]:
    try:
        title = await page.title()
    except Exception as exc:
        title = f"{type(exc).__name__}: {exc}"
    try:
        content = await page.content()
    except Exception:
        content = ""

    profile_links = await safe_locator_count(page, "xpath=//a[contains(@href, '/user/profile/')]")
    me_visible = await safe_locator_count(page, "xpath=//a[contains(@href, '/user/profile/')]//span[text()='我']")
    qr_images = await safe_locator_count(page, "xpath=//img[contains(@class, 'qrcode') or contains(@src, 'qr')]")
    qr_canvas = await safe_locator_count(page, "canvas")
    security_markers = sorted({text for text in XHS_SECURITY_TEXTS if text in content})
    login_markers = sorted({text for text in XHS_LOGIN_TEXTS if text in content})
    try:
        url = page.url
    except Exception:
        url = ""

    return {
        "url": url,
        "title": title,
        "profile_links": profile_links,
        "me_visible": me_visible,
        "profile_ui": bool(me_visible),
        "qr_images": qr_images,
        "qr_canvas": qr_canvas,
        "security_markers": security_markers,
        "login_markers": login_markers,
    }


async def xhs_context_state(context: Any) -> dict[str, Any]:
    pages = [page for page in context.pages if not page.is_closed()]
    page_states = [await xhs_page_snapshot(page) for page in pages]
    cookies = await context.cookies(list(XHS_COOKIE_URLS))
    cookie_names = sorted({item.get("name", "") for item in cookies if item.get("name")})
    cookie_map = {item.get("name"): item.get("value") for item in cookies if item.get("name")}
    markers = {
        "web_session": bool(cookie_map.get("web_session")),
        "a1": bool(cookie_map.get("a1")),
        "webId": bool(cookie_map.get("webId")),
        "gid": bool(cookie_map.get("gid")),
        "profile_ui": any(item.get("profile_ui") for item in page_states),
        "security_checkpoint": any(item.get("security_markers") for item in page_states),
        "login_or_qr_prompt": any(
            item.get("login_markers") or item.get("qr_images") or item.get("qr_canvas")
            for item in page_states
        ),
    }
    ok = bool(markers["profile_ui"])
    return {
        "ok": ok,
        "markers": markers,
        "cookie_names": cookie_names,
        "pages": page_states,
    }


def xhs_state_signature(state: dict[str, Any]) -> str:
    pages = [
        {
            "url": item.get("url"),
            "profile_ui": item.get("profile_ui"),
            "security": item.get("security_markers"),
            "login": item.get("login_markers"),
            "qr_images": item.get("qr_images"),
            "qr_canvas": item.get("qr_canvas"),
        }
        for item in state.get("pages", [])
    ]
    return json.dumps({"markers": state.get("markers", {}), "pages": pages}, ensure_ascii=False, sort_keys=True)


async def save_xhs_preflight_screenshots(context: Any, screenshot_dir: Path, label: str) -> list[str]:
    ensure_dir(screenshot_dir)
    saved: list[str] = []
    pages = [page for page in context.pages if not page.is_closed()]
    for index, page in enumerate(pages, start=1):
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
        path = screenshot_dir / f"{safe_label}_page{index:02d}.png"
        try:
            await page.screenshot(path=str(path), full_page=False)
        except Exception:
            continue
        saved.append(str(path))
    return saved


def xhs_cookie_for_restore(cookie: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    restored = {key: value for key, value in cookie.items() if key in allowed_keys and value is not None}
    if restored.get("expires") == -1:
        restored.pop("expires", None)
    return restored


async def restore_xhs_storage_snapshot(context: Any, snapshot_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(snapshot_path),
        "restored": False,
        "reason": "",
        "cookie_names": [],
        "origin_count": 0,
        "runtime_storage_count": 0,
    }
    if not snapshot_path.exists():
        result["reason"] = "missing_snapshot"
        return result

    try:
        state = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["reason"] = f"read_failed:{type(exc).__name__}"
        result["error"] = str(exc)
        return result

    cookies = [
        xhs_cookie_for_restore(cookie)
        for cookie in state.get("cookies", [])
        if isinstance(cookie, dict) and cookie.get("name") and cookie.get("value")
    ]
    if cookies:
        try:
            await context.add_cookies(cookies)
        except Exception as exc:
            result["cookie_restore_error"] = f"{type(exc).__name__}: {exc}"

    storage_by_origin: dict[str, dict[str, dict[str, str]]] = {}
    for origin_item in state.get("origins", []):
        if not isinstance(origin_item, dict):
            continue
        origin = origin_item.get("origin")
        if not origin:
            continue
        bucket = storage_by_origin.setdefault(origin, {"localStorage": {}, "sessionStorage": {}})
        for item in origin_item.get("localStorage", []):
            if isinstance(item, dict) and item.get("name") is not None and item.get("value") is not None:
                bucket["localStorage"][str(item["name"])] = str(item["value"])

    for item in state.get("trippostcollect", {}).get("runtime_storage", []):
        if not isinstance(item, dict):
            continue
        origin = item.get("origin")
        if not origin:
            continue
        bucket = storage_by_origin.setdefault(origin, {"localStorage": {}, "sessionStorage": {}})
        for storage_key in ("localStorage", "sessionStorage"):
            values = item.get(storage_key)
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if value is not None:
                    bucket[storage_key][str(key)] = str(value)

    if storage_by_origin:
        script_payload = json.dumps(storage_by_origin, ensure_ascii=False)
        await context.add_init_script(
            script=f"""
            (() => {{
              const storageByOrigin = {script_payload};
              const state = storageByOrigin[location.origin];
              if (!state) return;
              for (const [key, value] of Object.entries(state.localStorage || {{}})) {{
                try {{ window.localStorage.setItem(key, value); }} catch (_) {{}}
              }}
              for (const [key, value] of Object.entries(state.sessionStorage || {{}})) {{
                try {{ window.sessionStorage.setItem(key, value); }} catch (_) {{}}
              }}
            }})();
            """
        )

    cookie_names = sorted({item.get("name", "") for item in cookies if item.get("name")})
    result.update(
        {
            "restored": bool(cookies or storage_by_origin),
            "reason": "restored" if cookies or storage_by_origin else "empty_snapshot",
            "cookie_names": cookie_names,
            "origin_count": len(storage_by_origin),
            "runtime_storage_count": len(state.get("trippostcollect", {}).get("runtime_storage", [])),
        }
    )
    return result


async def capture_xhs_storage_snapshot(context: Any, snapshot_path: Path) -> dict[str, Any]:
    state = await context.storage_state()
    runtime_storage: list[dict[str, Any]] = []
    for page in [page for page in context.pages if not page.is_closed()]:
        url = page.url or ""
        if "xiaohongshu.com" not in url and "rednote.com" not in url:
            continue
        try:
            storage = await page.evaluate(
                """
                () => ({
                  origin: location.origin,
                  url: location.href,
                  localStorage: Object.fromEntries(Object.entries(window.localStorage || {})),
                  sessionStorage: Object.fromEntries(Object.entries(window.sessionStorage || {}))
                })
                """
            )
        except Exception as exc:
            storage = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
        runtime_storage.append(storage)

    state["trippostcollect"] = {
        "platform": "xhs",
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_storage": runtime_storage,
    }
    write_json(snapshot_path, state)
    try:
        snapshot_path.chmod(0o600)
    except OSError:
        pass
    return {
        "path": str(snapshot_path),
        "cookie_names": sorted({item.get("name", "") for item in state.get("cookies", []) if item.get("name")}),
        "origin_count": len(state.get("origins", [])),
        "runtime_storage_count": len(runtime_storage),
    }


async def run_xhs_preflight(args: argparse.Namespace, preflight_dir: Path) -> dict[str, Any]:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    profile_dir = MEDIACRAWLER_DIR / "browser_data" / "xhs_user_data_dir"
    ensure_dir(profile_dir)
    storage_snapshot_path = profile_dir / XHS_STORAGE_SNAPSHOT_FILENAME
    browser_path = discover_cdp_browser_path()
    screenshot_dir = ensure_dir(preflight_dir / "screenshots")
    observed_pages: list[dict[str, Any]] = []
    screenshots: list[str] = []
    nav_error = ""
    restored_snapshot: dict[str, Any] | None = None
    started = time.monotonic()

    async with async_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": bool(args.headless),
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1280, "height": 900},
            "accept_downloads": True,
            "ignore_https_errors": True,
            "args": [
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--no-sandbox",
                *browser_runtime_args(),
            ],
            "env": browser_launch_environment(),
        }
        if browser_path:
            launch_kwargs["executable_path"] = browser_path

        context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
        restored_snapshot = await restore_xhs_storage_snapshot(context, storage_snapshot_path)
        if restored_snapshot.get("restored"):
            print(
                "[preflight] restored xhs storage snapshot "
                f"cookies={len(restored_snapshot.get('cookie_names', []))} "
                f"origins={restored_snapshot.get('origin_count', 0)}",
                flush=True,
            )
        context.on(
            "page",
            lambda page: observed_pages.append({"event": "new_page", "url_at_open": page.url, "seen_at": utc_stamp()}),
        )
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            await page.goto(XHS_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError as exc:
            nav_error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            nav_error = f"{type(exc).__name__}: {exc}"

        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass

        if args.xhs_initial_delay_seconds > 0:
            print(f"[preflight] xhs initial settle {args.xhs_initial_delay_seconds:.1f}s", flush=True)
            await page.wait_for_timeout(int(args.xhs_initial_delay_seconds * 1000))

        state: dict[str, Any] = {}
        last_print = 0.0
        last_screenshot = 0.0
        last_signature = ""
        while time.monotonic() - started < args.xhs_preflight_timeout:
            state = await xhs_context_state(context)
            signature = xhs_state_signature(state)
            now = time.monotonic()
            should_capture = signature != last_signature
            if args.xhs_screenshot_interval > 0 and now - last_screenshot >= args.xhs_screenshot_interval:
                should_capture = True
            if should_capture:
                label = f"{int(now - started):04d}s"
                screenshots.extend(await save_xhs_preflight_screenshots(context, screenshot_dir, label))
                last_screenshot = now
                last_signature = signature
            if state["ok"]:
                break
            if now - last_print >= 10:
                print(f"[preflight] waiting xhs markers={state.get('markers')}", flush=True)
                last_print = now
            await page.wait_for_timeout(2_000)

        elapsed = round(time.monotonic() - started, 2)
        if not state:
            state = await xhs_context_state(context)
        storage_snapshot: dict[str, Any] | None = None
        if state.get("ok"):
            storage_snapshot = await capture_xhs_storage_snapshot(context, storage_snapshot_path)
        await context.close()

    result = {
        "platform": "xhs",
        "ok": bool(state.get("ok")),
        "reason": "ready" if state.get("ok") else "timeout_waiting_for_login_or_security_confirmation",
        "elapsed_seconds": elapsed,
        "timeout_seconds": args.xhs_preflight_timeout,
        "initial_delay_seconds": args.xhs_initial_delay_seconds,
        "profile_dir": str(profile_dir),
        "browser_path": browser_path,
        "headless": bool(args.headless),
        "nav_error": nav_error,
        "observed_pages": observed_pages,
        "screenshots": screenshots,
        "restored_snapshot": restored_snapshot,
        "storage_snapshot": storage_snapshot,
        "state": state,
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_json(preflight_dir / "preflight.json", result)
    return result


def main() -> int:
    args = parse_args()
    if args.target_count <= 0 or args.batch_size <= 0:
        raise SystemExit("--target-count and --batch-size must be positive")
    if args.target_count % args.batch_size != 0:
        raise SystemExit("--target-count must be divisible by --batch-size")

    ensure_prerequisites()
    batch_root = ensure_dir(Path(args.output_dir).expanduser() / utc_stamp())
    platform_states: dict[str, dict[str, Any]] = {}
    aborted = False
    abort_reason = ""

    summary: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "keyword": args.keyword,
        "target_count_per_platform": args.target_count,
        "batch_size": args.batch_size,
        "platforms": args.platforms,
        "batch_root": str(batch_root),
        "ok": False,
        "aborted": False,
        "abort_reason": "",
        "platforms_state": platform_states,
    }

    if args.xhs_preflight_only:
        preflight_dir = ensure_dir(batch_root / "xhs" / "preflight")
        print("[preflight] xhs browser settle/login-state check only", flush=True)
        preflight = asyncio.run(run_xhs_preflight(args, preflight_dir))
        summary["platforms_state"]["xhs"] = {
            "preflight": {
                "ok": preflight["ok"],
                "reason": preflight["reason"],
                "elapsed_seconds": preflight["elapsed_seconds"],
                "preflight_json": str(preflight_dir / "preflight.json"),
                "markers": preflight.get("state", {}).get("markers", {}),
                "screenshots": preflight.get("screenshots", []),
                "restored_snapshot": preflight.get("restored_snapshot"),
                "storage_snapshot": preflight.get("storage_snapshot"),
            }
        }
        summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        summary["ok"] = bool(preflight["ok"])
        summary["aborted"] = not bool(preflight["ok"])
        summary["abort_reason"] = "" if preflight["ok"] else preflight["reason"]
        write_json(batch_root / "summary.json", summary)
        print(json.dumps({"summary": str(batch_root / "summary.json"), **summary}, ensure_ascii=False, indent=2))
        return 0 if preflight["ok"] else 2

    for platform_key in args.platforms:
        seen: set[str] = set()
        platform_state = {
            "target_count": args.target_count,
            "completed_batches": 0,
            "valid_count": 0,
            "seen_identities": [],
            "batches": [],
        }
        platform_states[platform_key] = platform_state
        total_batches = args.target_count // args.batch_size

        if platform_key == "xhs" and not args.skip_xhs_preflight:
            preflight_dir = ensure_dir(batch_root / platform_key / "preflight")
            print("[preflight] xhs browser settle/login-state check", flush=True)
            preflight = asyncio.run(run_xhs_preflight(args, preflight_dir))
            platform_state["preflight"] = {
                "ok": preflight["ok"],
                "reason": preflight["reason"],
                "elapsed_seconds": preflight["elapsed_seconds"],
                "preflight_json": str(preflight_dir / "preflight.json"),
                "markers": preflight.get("state", {}).get("markers", {}),
                "restored_snapshot": preflight.get("restored_snapshot"),
                "storage_snapshot": preflight.get("storage_snapshot"),
            }
            write_json(batch_root / "summary.json", summary)
            if not preflight["ok"]:
                aborted = True
                abort_reason = "xhs preflight failed before batch crawl"
                summary["aborted"] = True
                summary["abort_reason"] = abort_reason
                summary["failed_preflight"] = preflight
                write_json(batch_root / "summary.json", summary)
                print(json.dumps({"summary": str(batch_root / "summary.json"), **summary}, ensure_ascii=False, indent=2))
                return 2

        for batch_index in range(total_batches):
            batch_no = batch_index + 1
            page_start = batch_no
            if platform_key == "douyin":
                page_start = 1
            batch_dir = ensure_dir(batch_root / platform_key / f"batch_{batch_no:02d}")
            save_path = ensure_dir(batch_dir / "data")
            log_dir = ensure_dir(batch_dir / "logs")
            cmd, env = command_for_batch(
                platform_key=platform_key,
                keyword=args.keyword,
                batch_size=args.batch_size,
                batch_no=batch_no,
                page_start=page_start,
                save_path=save_path,
                login_type=args.login_type,
                headless=args.headless,
                xhs_initial_delay_seconds=args.xhs_initial_delay_seconds,
                xhs_login_wait_seconds=args.xhs_login_wait_seconds,
                xhs_candidate_multiplier=args.xhs_candidate_multiplier,
                douyin_candidate_multiplier=args.douyin_candidate_multiplier,
            )
            print(f"[batch] {platform_key} batch={batch_no} start={page_start} cmd={shlex.join(cmd)}", flush=True)
            run = run_command(cmd, MEDIACRAWLER_DIR, args.timeout_per_batch, log_dir, extra_env=env)
            output = summarize_output(save_path, args.keyword)
            validation = validate_batch(
                platform_key=platform_key,
                batch_size=args.batch_size,
                save_path=save_path,
                seen=seen,
            )
            batch_summary = {
                "platform": platform_key,
                "batch_no": batch_no,
                "page_start": page_start,
                "ok": run["returncode"] == 0 and not run["timed_out"] and validation["ok"],
                "run": run,
                "output": output,
                "validation": validation,
            }
            write_json(batch_dir / "batch_summary.json", batch_summary)
            platform_state["batches"].append(
                {
                    "batch_no": batch_no,
                    "ok": batch_summary["ok"],
                    "valid_count": validation["valid_count"],
                    "raw_content_records": validation["raw_content_records"],
                    "batch_summary": str(batch_dir / "batch_summary.json"),
                }
            )

            if not batch_summary["ok"]:
                aborted = True
                abort_reason = f"{platform_key} batch {batch_no} failed validation or crawler run"
                summary["aborted"] = True
                summary["abort_reason"] = abort_reason
                summary["failed_batch"] = batch_summary
                write_json(batch_root / "summary.json", summary)
                print(json.dumps({"summary": str(batch_root / "summary.json"), **summary}, ensure_ascii=False, indent=2))
                return 2

            platform_state["completed_batches"] = batch_no
            platform_state["valid_count"] += validation["valid_count"]
            platform_state["seen_identities"] = sorted(seen)
            write_json(batch_root / "summary.json", summary)
            if (
                platform_key == "xhs"
                and args.xhs_inter_batch_delay_seconds > 0
                and batch_no < total_batches
            ):
                print(
                    f"[batch] xhs cooldown {args.xhs_inter_batch_delay_seconds:.1f}s before next batch",
                    flush=True,
                )
                time.sleep(args.xhs_inter_batch_delay_seconds)

        if platform_state["valid_count"] != args.target_count:
            aborted = True
            abort_reason = f"{platform_key} valid count {platform_state['valid_count']} != {args.target_count}"
            break

    summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary["ok"] = not aborted
    summary["aborted"] = aborted
    summary["abort_reason"] = abort_reason
    write_json(batch_root / "summary.json", summary)
    print(json.dumps({"summary": str(batch_root / "summary.json"), **summary}, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
