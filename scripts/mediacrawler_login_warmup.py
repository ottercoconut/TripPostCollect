#!/usr/bin/env python3
"""Warm up MediaCrawler browser profiles with manual login."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from browser_runtime import browser_launch_environment, browser_runtime_args
from mediacrawler_crawl import discover_cdp_browser_path
from trippostcollect.core.paths import MEDIACRAWLER_DIR, MEDIACRAWLER_LOGIN_OUTPUT, PROJECT_ROOT, ensure_dir


ROOT = PROJECT_ROOT
DEFAULT_OUTPUT = MEDIACRAWLER_LOGIN_OUTPUT
COOKIE_SNAPSHOT_FILENAME = "trippostcollect_cookie_snapshot.json"

PLATFORMS: dict[str, dict[str, Any]] = {
    "douyin": {
        "code": "dy",
        "label": "抖音",
        "urls": ["https://www.douyin.com"],
        "required": "localStorage HasUserLogin=1 or cookie LOGIN_STATUS=1",
    },
    "zhihu": {
        "code": "zhihu",
        "label": "知乎",
        "urls": ["https://www.zhihu.com/signin?next=%2F", "https://www.zhihu.com"],
        "required": "cookies z_c0 and d_c0, plus /api/v4/me success when possible",
    },
    "weibo": {
        "code": "wb",
        "label": "微博",
        "urls": ["https://m.weibo.cn", "https://www.weibo.com"],
        "verify_url": "https://m.weibo.cn",
        "login_url": "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog",
        "required": "m.weibo.cn /api/config login=true with a non-empty uid",
    },
    "bilibili": {
        "code": "bili",
        "label": "B站",
        "urls": ["https://www.bilibili.com"],
        "required": "cookie SESSDATA or DedeUserID",
    },
}

ALIASES = {
    "dy": "douyin",
    "douyin": "douyin",
    "抖音": "douyin",
    "zhihu": "zhihu",
    "知乎": "zhihu",
    "wb": "weibo",
    "weibo": "weibo",
    "微博": "weibo",
    "bili": "bilibili",
    "bilibili": "bilibili",
    "b站": "bilibili",
    "B站": "bilibili",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open MediaCrawler profiles for manual login and verify login markers.")
    parser.add_argument(
        "--platforms",
        nargs="+",
        default=["douyin", "zhihu"],
        help="Platforms to warm up: douyin zhihu weibo bilibili, or all. XHS logs in inside each xhs_runner.py run.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=600, help="Maximum wait per platform.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT), help="Output root for login verification records.")
    parser.add_argument("--browser-path", help="Explicit Chrome/Chromium executable path. Defaults to MediaCrawler CDP discovery.")
    parser.add_argument("--no-close-on-success", action="store_true", help="Keep browser window open after login succeeds.")
    parser.add_argument("--skip-reopen-verify", action="store_true", help="Do not reopen the profile to verify login persistence.")
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f%z")


def selected_platforms(values: list[str]) -> list[str]:
    if "all" in values:
        return list(PLATFORMS)
    result: list[str] = []
    for value in values:
        key = ALIASES.get(value)
        if not key:
            raise SystemExit(f"Unknown platform: {value}")
        if key not in result:
            result.append(key)
    return result


def profile_dir_for(platform_key: str) -> Path:
    code = PLATFORMS[platform_key]["code"]
    return MEDIACRAWLER_DIR / "browser_data" / f"{code}_user_data_dir"


def cookie_dict(cookies: list[dict[str, Any]]) -> dict[str, str]:
    return {item["name"]: item.get("value", "") for item in cookies}


def required_cookie_names(platform_key: str) -> tuple[str, ...]:
    if platform_key == "zhihu":
        return ("d_c0", "z_c0")
    return ()


def cookie_snapshot_path(platform_key: str) -> Path:
    return profile_dir_for(platform_key) / COOKIE_SNAPSHOT_FILENAME


def cookie_snapshot_info(path: Path, cookies: list[dict[str, Any]], saved_at: str) -> dict[str, Any]:
    return {
        "path": str(path),
        "saved_at": saved_at,
        "cookie_names": sorted({item["name"] for item in cookies if item.get("name")}),
    }


def write_cookie_snapshot(
    platform_key: str,
    profile_dir: Path,
    cookies: list[dict[str, Any]],
    *,
    source: str,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    cookie_values = cookie_dict(cookies)
    missing = [name for name in required_cookie_names(platform_key) if not cookie_values.get(name)]
    if missing:
        return None
    saved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshot_path = profile_dir / COOKIE_SNAPSHOT_FILENAME
    payload = {
        "platform": platform_key,
        "label": PLATFORMS[platform_key]["label"],
        "saved_at": saved_at,
        "source": source,
        "urls": PLATFORMS[platform_key]["urls"],
        "required_cookie_names": list(required_cookie_names(platform_key)),
        "state_markers": state.get("markers") if isinstance(state, dict) else {},
        "cookies": cookies,
    }
    snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        snapshot_path.chmod(0o600)
    except OSError:
        pass
    return cookie_snapshot_info(snapshot_path, cookies, saved_at)


def browser_path_for(args: argparse.Namespace) -> str | None:
    if args.browser_path:
        path = Path(args.browser_path).expanduser()
        if not path.is_file():
            raise SystemExit(f"Browser executable does not exist: {path}")
        return str(path)
    return discover_cdp_browser_path()


async def launch_login_context(
    playwright,
    profile_dir: Path,
    browser_path: str | None,
    *,
    native_window_size: tuple[int, int] | None = None,
) -> BrowserContext:
    kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": False,
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
    if native_window_size:
        width, height = native_window_size
        kwargs.pop("viewport")
        kwargs["no_viewport"] = True
        kwargs["args"].append(f"--window-size={width},{height}")
    if browser_path:
        kwargs["executable_path"] = browser_path
    return await playwright.chromium.launch_persistent_context(**kwargs)


async def safe_local_storage(page: Page) -> dict[str, Any]:
    try:
        value = await page.evaluate("() => Object.assign({}, window.localStorage)")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


async def zhihu_api_check(page: Page) -> dict[str, Any]:
    try:
        result = await page.evaluate(
            """
            async () => {
              try {
                const res = await fetch('/api/v4/me?include=email,is_active,is_bind_phone', {
                  credentials: 'include'
                });
                const text = await res.text();
                let parsed = null;
                try { parsed = JSON.parse(text); } catch (_) {}
                return {
                  ok: res.ok,
                  status: res.status,
                  uid: parsed && parsed.uid,
                  name: parsed && parsed.name,
                  text: text.slice(0, 300)
                };
              } catch (error) {
                return { ok: false, error: String(error) };
              }
            }
            """
        )
        return result if isinstance(result, dict) else {"ok": False, "error": "unexpected result"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def weibo_api_check(page: Page) -> dict[str, Any]:
    try:
        result = await page.evaluate(
            """
            async () => {
              try {
                const res = await fetch('https://m.weibo.cn/api/config', {credentials: 'include'});
                const payload = await res.json();
                const data = payload && payload.data || {};
                return {
                  ok: res.ok && payload && payload.ok === 1,
                  status: res.status,
                  login: data.login === true,
                  uid: data.uid || null
                };
              } catch (error) {
                return {ok: false, login: false, error: String(error)};
              }
            }
            """
        )
        return result if isinstance(result, dict) else {"ok": False, "login": False, "error": "unexpected result"}
    except Exception as exc:
        return {"ok": False, "login": False, "error": f"{type(exc).__name__}: {exc}"}


async def current_state(context: BrowserContext, page: Page, platform_key: str) -> dict[str, Any]:
    urls = PLATFORMS[platform_key]["urls"]
    cookie_urls = [PLATFORMS[platform_key]["verify_url"]] if platform_key == "weibo" else urls
    cookies = cookie_dict(await context.cookies(cookie_urls))
    local_storage = await safe_local_storage(page)
    api: dict[str, Any] = {}

    if platform_key == "douyin":
        ok = local_storage.get("HasUserLogin") == "1" or cookies.get("LOGIN_STATUS") == "1"
        markers = {
            "HasUserLogin": local_storage.get("HasUserLogin"),
            "LOGIN_STATUS": cookies.get("LOGIN_STATUS"),
        }
    elif platform_key == "zhihu":
        api = await zhihu_api_check(page)
        ok = bool(cookies.get("z_c0") and cookies.get("d_c0") and api.get("ok") and api.get("uid"))
        markers = {
            "z_c0": bool(cookies.get("z_c0")),
            "d_c0": bool(cookies.get("d_c0")),
            "api_ok": api.get("ok"),
            "api_uid": api.get("uid"),
        }
    elif platform_key == "weibo":
        api = await weibo_api_check(page)
        current_cookie_ok = bool(cookies.get("SUB") and cookies.get("MLOGIN"))
        ok = bool(api.get("ok") and api.get("login") and api.get("uid"))
        markers = {
            "SSOLoginState": bool(cookies.get("SSOLoginState")),
            "WBPSESS": bool(cookies.get("WBPSESS")),
            "SUB": bool(cookies.get("SUB")),
            "MLOGIN": bool(cookies.get("MLOGIN")),
            "current_cookie_pair": current_cookie_ok,
            "api_ok": api.get("ok"),
            "api_login": api.get("login"),
            "api_uid": api.get("uid"),
        }
    elif platform_key == "bilibili":
        ok = bool(cookies.get("SESSDATA") or cookies.get("DedeUserID"))
        markers = {
            "SESSDATA": bool(cookies.get("SESSDATA")),
            "DedeUserID": bool(cookies.get("DedeUserID")),
        }
    else:
        ok = False
        markers = {}

    return {
        "ok": ok,
        "markers": markers,
        "cookie_names": sorted(cookies),
        "local_storage_keys": sorted(local_storage),
        "api": api,
        "url": page.url,
    }


async def weibo_desktop_login_state(context: BrowserContext) -> dict[str, Any]:
    cookies = cookie_dict(
        await context.cookies(
            [
                "https://weibo.com",
                "https://www.weibo.com",
                "https://passport.weibo.com",
            ]
        )
    )
    return {
        "SSOLoginState": cookies.get("SSOLoginState"),
        "WBPSESS": cookies.get("WBPSESS"),
    }


def weibo_desktop_login_completed(initial: dict[str, Any], current: dict[str, Any]) -> bool:
    if current.get("SSOLoginState"):
        return True
    current_session = current.get("WBPSESS")
    return bool(current_session and current_session != initial.get("WBPSESS"))


async def warmup_one(playwright, platform_key: str, batch_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    platform = PLATFORMS[platform_key]
    profile_dir = profile_dir_for(platform_key)
    ensure_dir(profile_dir)
    out_path = batch_dir / f"{platform_key}.json"
    browser_path = browser_path_for(args)

    print(f"[login] {platform['label']} profile={profile_dir}", flush=True)
    print(f"[login] browser={browser_path or 'playwright default chromium'}", flush=True)
    print(f"[login] 正在验证持久登录态；若已失效，请在窗口中重新登录。检测条件：{platform['required']}", flush=True)

    context = await launch_login_context(playwright, profile_dir, browser_path)
    page = context.pages[0] if context.pages else await context.new_page()

    nav_error = ""
    initial_urls = [platform["verify_url"]] if platform.get("verify_url") else platform["urls"]
    for url in initial_urls:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            break
        except PlaywrightTimeoutError as exc:
            nav_error = f"{type(exc).__name__}: {exc}"

    started = time.monotonic()
    last_print = 0.0
    last_verify_refresh = started
    login_page: Page | None = None
    login_nav_error = ""
    desktop_login_initial: dict[str, Any] = {}
    desktop_login_state: dict[str, Any] = {}
    desktop_login_detected = False
    state: dict[str, Any] = {}
    initial_state: dict[str, Any] = {}
    while time.monotonic() - started < args.timeout_seconds:
        state = await current_state(context, page, platform_key)
        if not initial_state:
            initial_state = dict(state)
            login_url = platform.get("login_url")
            if not state["ok"] and login_url:
                login_page = await context.new_page()
                try:
                    await login_page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
                except PlaywrightTimeoutError as exc:
                    login_nav_error = f"{type(exc).__name__}: {exc}"
                await login_page.bring_to_front()
                if platform_key == "weibo":
                    desktop_login_initial = await weibo_desktop_login_state(context)
        if state["ok"]:
            if platform_key == "weibo" and login_page is not None:
                desktop_login_detected = True
                if not desktop_login_state:
                    desktop_login_state = await weibo_desktop_login_state(context)
            break
        now = time.monotonic()
        if platform_key == "weibo" and login_page is not None and not desktop_login_detected:
            desktop_login_state = await weibo_desktop_login_state(context)
            if weibo_desktop_login_completed(desktop_login_initial, desktop_login_state):
                desktop_login_detected = True
                try:
                    await login_page.goto(platform["verify_url"], wait_until="domcontentloaded", timeout=30_000)
                    await page.reload(wait_until="domcontentloaded", timeout=30_000)
                except PlaywrightTimeoutError as exc:
                    nav_error = f"{type(exc).__name__}: {exc}"
                last_verify_refresh = now
        if platform.get("verify_url") and login_page is not None and now - last_verify_refresh >= 10:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30_000)
            except PlaywrightTimeoutError as exc:
                nav_error = f"{type(exc).__name__}: {exc}"
            last_verify_refresh = now
        if now - last_print >= 10:
            print(f"[login] waiting {platform['label']} markers={state.get('markers')}", flush=True)
            last_print = now
        await page.wait_for_timeout(2_000)

    elapsed = round(time.monotonic() - started, 2)
    session_cookies = await context.cookies(platform["urls"]) if state.get("ok") else []
    result = {
        "platform": platform_key,
        "label": platform["label"],
        "ok": bool(state.get("ok")),
        "session_ok": bool(state.get("ok")),
        "persisted_ok": None,
        "initial_ok": bool(initial_state.get("ok")),
        "login_refreshed": not bool(initial_state.get("ok")) and bool(state.get("ok")),
        "initial_state": initial_state,
        "cookie_snapshot": None,
        "elapsed_seconds": elapsed,
        "profile_dir": str(profile_dir),
        "browser_path": browser_path,
        "nav_error": nav_error,
        "login_url": platform.get("login_url"),
        "login_nav_error": login_nav_error,
        "desktop_login_detected": desktop_login_detected,
        "desktop_login_state": {key: bool(value) for key, value in desktop_login_state.items()},
        "state": state,
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if result["ok"] and args.no_close_on_success:
        result["cookie_snapshot"] = write_cookie_snapshot(
            platform_key,
            profile_dir,
            session_cookies,
            source="session",
            state=state,
        )
        print("[login] --no-close-on-success enabled; press Ctrl+C when you want to close this browser.", flush=True)
        try:
            while True:
                await page.wait_for_timeout(60_000)
        except KeyboardInterrupt:
            pass

    if result["ok"] and args.skip_reopen_verify and not args.no_close_on_success:
        result["cookie_snapshot"] = write_cookie_snapshot(
            platform_key,
            profile_dir,
            session_cookies,
            source="session_skip_reopen_verify",
            state=state,
        )

    await context.close()
    if result["ok"] and not args.skip_reopen_verify and not args.no_close_on_success:
        verify_context = await launch_login_context(playwright, profile_dir, browser_path)
        verify_page = verify_context.pages[0] if verify_context.pages else await verify_context.new_page()
        verify_nav_error = ""
        try:
            verify_url = platform.get("verify_url") or platform["urls"][-1]
            await verify_page.goto(verify_url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeoutError as exc:
            verify_nav_error = f"{type(exc).__name__}: {exc}"
        verify_state = await current_state(verify_context, verify_page, platform_key)
        verify_cookies = await verify_context.cookies(platform["urls"]) if verify_state.get("ok") else []
        result["persisted_ok"] = bool(verify_state.get("ok"))
        result["ok"] = bool(result["session_ok"] and result["persisted_ok"])
        if result["ok"]:
            result["cookie_snapshot"] = write_cookie_snapshot(
                platform_key,
                profile_dir,
                verify_cookies,
                source="reopen_verify",
                state=verify_state,
            )
        await verify_context.close()
        result["reopen_verify"] = {
            "ok": result["persisted_ok"],
            "nav_error": verify_nav_error,
            "state": verify_state,
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        print(f"[login] reopen verify {platform['label']} markers={verify_state.get('markers')}", flush=True)

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[login] {platform['label']} {'ok' if result['ok'] else 'not verified'} -> {out_path}", flush=True)
    return result


async def main_async() -> int:
    args = parse_args()
    platforms = selected_platforms(args.platforms)
    batch_dir = ensure_dir(Path(args.output_dir).expanduser() / utc_stamp())

    if not MEDIACRAWLER_DIR.exists():
        raise SystemExit(f"MediaCrawler is missing: {MEDIACRAWLER_DIR}")

    async with async_playwright() as playwright:
        results = []
        for platform_key in platforms:
            results.append(await warmup_one(playwright, platform_key, batch_dir, args))

    summary = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "batch_dir": str(batch_dir),
        "ok_count": sum(1 for item in results if item["ok"]),
        "failed_count": sum(1 for item in results if not item["ok"]),
        "records": results,
    }
    summary_path = batch_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": str(summary_path), "ok_count": summary["ok_count"]}, ensure_ascii=False, indent=2))
    return 0 if summary["failed_count"] == 0 else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
