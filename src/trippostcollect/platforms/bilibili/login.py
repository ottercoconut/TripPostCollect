"""B站行为会话与 Cookie 导出。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from urllib.parse import quote

from trippostcollect.application.contracts import BilibiliBehaviorPorts
from trippostcollect.core.paths import ensure_dir
from trippostcollect.records.formal import VIDEO_URL_RE


async def run_bilibili_behavior_session(
    args: argparse.Namespace,
    evidence_path: Path,
    *,
    ports: BilibiliBehaviorPorts,
) -> tuple[dict[str, Any], dict[str, Any]]:
    discover_cdp_browser_path = ports.discover_cdp_browser_path
    profile_dir_for = ports.profile_dir_for
    async_playwright = ports.async_playwright
    browser_runtime_args = ports.browser_runtime_args
    browser_launch_environment = ports.browser_launch_environment
    launch_profile_context = ports.launch_profile_context
    install_runtime_hints = ports.install_runtime_hints
    run_page_behavior = ports.run_page_behavior
    write_evidence = ports.write_evidence
    platform_cookie_url = ports.platform_cookie_url
    cookies_to_header = ports.cookies_to_header
    cookie_snapshot_path = ports.cookie_snapshot_path
    cookie_names_from_header = ports.cookie_names_from_header
    required_cookie_names = ports.required_cookie_names
    browser_path = discover_cdp_browser_path()
    profile_dir = ensure_dir(profile_dir_for("bilibili"))
    target_url = "https://search.bilibili.com/article?keyword=" + quote(args.keyword)

    async with async_playwright() as playwright:
        # #136：与 warmup、通用平台 crawl 标准模式共用持久 context 启动配置：沙箱开启、locale/timezone 相同，
        # 不再单设 1440×900 视口与 screen，也不再传 ignore_default_args。#139：按原生窗口启动，不做设备模拟。
        context = await launch_profile_context(
            playwright.chromium,
            profile_dir,
            headless=not args.headed,
            browser_path=browser_path,
            runtime_args=browser_runtime_args(),
            env=browser_launch_environment(),
        )
        try:
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
                write_evidence=write_evidence,
            )
            cookies = await context.cookies([platform_cookie_url("bilibili")])
            # #127：本轮浏览器自报的 UA，随 Cookie 一起交给浏览器外的 B站请求。
            user_agent = str(await page.evaluate("() => navigator.userAgent") or "")
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
        "user_agent": user_agent,
    }
    return cookie_export, evidence

