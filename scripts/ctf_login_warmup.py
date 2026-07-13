#!/usr/bin/env python3
"""Warm up CTF-resource-crawl browser profiles with manual login.

This script opens the same persistent Chromium profile that
``ctf_resource_crawl.py`` uses for a given site, lets a human complete login
manually in a headed window, verifies login markers, and persists the login
state inside the profile directory. It is the CTF-link counterpart of
``mediacrawler_login_warmup.py``.

Currently supported site: ``douban_group`` (login marker: cookie ``dbcl2``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from browser_runtime import browser_launch_environment, browser_runtime_args
from trippostcollect.core.paths import CTF_BROWSER_PROFILE_ROOT, CTF_LOGIN_OUTPUT, PROJECT_ROOT, ensure_dir
from trippostcollect.platforms.registry import get_site, site_keys


ROOT = PROJECT_ROOT
DEFAULT_OUTPUT = CTF_LOGIN_OUTPUT
COOKIE_SNAPSHOT_FILENAME = "trippostcollect_cookie_snapshot.json"


# Per-site login verification config. ``marker_cookies`` are checked against the
# persistent profile cookie jar; any one hit counts as logged in. ``check_urls``
# are the URLs passed to ``BrowserContext.cookies`` so the jar returns cookies
# for the right domain. Site URL/login config comes from the platform registry.
LOGIN_CHECKERS: dict[str, dict[str, Any]] = {
    "douban_group": {
        "label": "豆瓣小组",
        "marker_cookies": ("dbcl2",),
        "check_urls": ("https://www.douban.com/group/",),
        "required_hint": "cookie dbcl2 (豆瓣登录态凭证，HttpOnly 持久 cookie)",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open CTF-resource-crawl profiles for manual login and verify login markers.",
    )
    parser.add_argument(
        "--sites",
        nargs="+",
        default=["douban_group"],
        help="Site keys to warm up. Currently supported: douban_group.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=600, help="Maximum wait per site.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT), help="Output root for login verification records.")
    parser.add_argument("--browser-path", help="Explicit Chrome/Chromium executable path. Defaults to playwright bundled chromium.")
    parser.add_argument("--no-close-on-success", action="store_true", help="Keep browser window open after login succeeds.")
    parser.add_argument("--skip-reopen-verify", action="store_true", help="Do not reopen the profile to verify login persistence.")
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%z")


def selected_sites(values: list[str]) -> list[str]:
    valid = set(site_keys(include_no_login=True))
    result: list[str] = []
    for value in values:
        if value not in valid:
            raise SystemExit(f"Unknown site: {value}. Choices: {', '.join(sorted(valid))}")
        if value not in LOGIN_CHECKERS:
            raise SystemExit(
                f"Login check not implemented for site: {value}. "
                f"Supported: {', '.join(sorted(LOGIN_CHECKERS))}"
            )
        if value not in result:
            result.append(value)
    return result


def profile_dir_for(site_key: str) -> Path:
    site = get_site(site_key)
    if site.profile_dir_override is not None:
        return site.profile_dir_override
    return CTF_BROWSER_PROFILE_ROOT / site_key


def cookie_dict(cookies: list[dict[str, Any]]) -> dict[str, str]:
    return {item["name"]: item.get("value", "") for item in cookies}


def cookie_snapshot_path(site_key: str) -> Path:
    return profile_dir_for(site_key) / COOKIE_SNAPSHOT_FILENAME


def cookie_snapshot_info(path: Path, cookies: list[dict[str, Any]], saved_at: str) -> dict[str, Any]:
    return {
        "path": str(path),
        "saved_at": saved_at,
        "cookie_names": sorted({item["name"] for item in cookies if item.get("name")}),
    }


def write_cookie_snapshot(
    site_key: str,
    profile_dir: Path,
    cookies: list[dict[str, Any]],
    *,
    source: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    saved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshot_path = profile_dir / COOKIE_SNAPSHOT_FILENAME
    payload = {
        "site": site_key,
        "label": LOGIN_CHECKERS[site_key]["label"],
        "saved_at": saved_at,
        "source": source,
        "login_url": get_site(site_key).login_url,
        "marker_cookies": list(LOGIN_CHECKERS[site_key]["marker_cookies"]),
        "state_markers": state.get("markers") if isinstance(state, dict) else {},
        "cookies": cookies,
    }
    snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        snapshot_path.chmod(0o600)
    except OSError:
        pass
    return cookie_snapshot_info(snapshot_path, cookies, saved_at)


def resolve_browser_path(args: argparse.Namespace) -> str | None:
    if args.browser_path:
        path = Path(args.browser_path).expanduser()
        if not path.is_file():
            raise SystemExit(f"Browser executable does not exist: {path}")
        return str(path)
    return None


async def launch_login_context(playwright, profile_dir: Path, browser_path: str | None) -> BrowserContext:
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
            *browser_runtime_args(),
        ],
        "env": browser_launch_environment(),
    }
    if browser_path:
        kwargs["executable_path"] = browser_path
    return await playwright.chromium.launch_persistent_context(**kwargs)


async def current_state(context: BrowserContext, page: Page, site_key: str) -> dict[str, Any]:
    checker = LOGIN_CHECKERS[site_key]
    cookies = cookie_dict(await context.cookies(list(checker["check_urls"])))
    markers = {name: bool(cookies.get(name)) for name in checker["marker_cookies"]}
    ok = any(markers.values())
    return {
        "ok": ok,
        "markers": markers,
        "cookie_names": sorted(cookies),
        "url": page.url,
    }


async def warmup_one(playwright, site_key: str, batch_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    site = get_site(site_key)
    checker = LOGIN_CHECKERS[site_key]
    profile_dir = profile_dir_for(site_key)
    ensure_dir(profile_dir)
    out_path = batch_dir / f"{site_key}.json"
    browser_path = resolve_browser_path(args)

    print(f"[login] {checker['label']} profile={profile_dir}", flush=True)
    print(f"[login] browser={browser_path or 'playwright bundled chromium'}", flush=True)
    print(f"[login] 请在打开的浏览器窗口中手动完成登录；检测条件：{checker['required_hint']}", flush=True)

    context = await launch_login_context(playwright, profile_dir, browser_path)
    page = context.pages[0] if context.pages else await context.new_page()

    nav_error = ""
    login_url = site.login_url or site.default_url
    try:
        await page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError as exc:
        nav_error = f"{type(exc).__name__}: {exc}"

    started = time.monotonic()
    last_print = 0.0
    state: dict[str, Any] = {}
    while time.monotonic() - started < args.timeout_seconds:
        state = await current_state(context, page, site_key)
        if state["ok"]:
            break
        now = time.monotonic()
        if now - last_print >= 10:
            print(f"[login] waiting {checker['label']} markers={state.get('markers')}", flush=True)
            last_print = now
        await page.wait_for_timeout(2_000)

    elapsed = round(time.monotonic() - started, 2)
    session_cookies = await context.cookies(list(checker["check_urls"])) if state.get("ok") else []
    result: dict[str, Any] = {
        "site": site_key,
        "label": checker["label"],
        "ok": bool(state.get("ok")),
        "session_ok": bool(state.get("ok")),
        "persisted_ok": None,
        "cookie_snapshot": None,
        "elapsed_seconds": elapsed,
        "profile_dir": str(profile_dir),
        "browser_path": browser_path,
        "login_url": login_url,
        "nav_error": nav_error,
        "state": state,
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if result["ok"] and args.no_close_on_success:
        result["cookie_snapshot"] = write_cookie_snapshot(
            site_key, profile_dir, session_cookies, source="session", state=state,
        )
        print("[login] --no-close-on-success enabled; press Ctrl+C when you want to close this browser.", flush=True)
        try:
            while True:
                await page.wait_for_timeout(60_000)
        except KeyboardInterrupt:
            pass

    await context.close()
    if result["ok"] and not args.skip_reopen_verify and not args.no_close_on_success:
        verify_context = await launch_login_context(playwright, profile_dir, browser_path)
        verify_page = verify_context.pages[0] if verify_context.pages else await verify_context.new_page()
        verify_nav_error = ""
        try:
            await verify_page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeoutError as exc:
            verify_nav_error = f"{type(exc).__name__}: {exc}"
        verify_state = await current_state(verify_context, verify_page, site_key)
        verify_cookies = await verify_context.cookies(list(checker["check_urls"])) if verify_state.get("ok") else []
        await verify_context.close()
        result["persisted_ok"] = bool(verify_state.get("ok"))
        result["ok"] = bool(result["session_ok"] and result["persisted_ok"])
        if result["ok"]:
            result["cookie_snapshot"] = write_cookie_snapshot(
                site_key, profile_dir, verify_cookies, source="reopen_verify", state=verify_state,
            )
        result["reopen_verify"] = {
            "ok": result["persisted_ok"],
            "nav_error": verify_nav_error,
            "state": verify_state,
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        print(f"[login] reopen verify {checker['label']} markers={verify_state.get('markers')}", flush=True)
    elif result["ok"] and args.skip_reopen_verify:
        result["cookie_snapshot"] = write_cookie_snapshot(
            site_key, profile_dir, session_cookies, source="session_skip_reopen_verify", state=state,
        )

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[login] {checker['label']} {'ok' if result['ok'] else 'not verified'} -> {out_path}", flush=True)
    return result


async def main_async() -> int:
    args = parse_args()
    sites = selected_sites(args.sites)
    batch_dir = ensure_dir(Path(args.output_dir).expanduser() / utc_stamp())

    async with async_playwright() as playwright:
        results = []
        for site_key in sites:
            results.append(await warmup_one(playwright, site_key, batch_dir, args))

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
