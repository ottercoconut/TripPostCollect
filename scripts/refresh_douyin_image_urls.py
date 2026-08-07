#!/usr/bin/env python3
"""Refresh only Douyin note-image assets for one known aweme ID."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Sequence

from playwright.async_api import async_playwright

from browser_runtime import browser_launch_environment, browser_runtime_args
from mediacrawler_crawl import discover_cdp_browser_path
from trippostcollect.core.paths import MEDIACRAWLER_DIR


DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aweme-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--browser-path")
    return parser.parse_args(argv)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            json.dump(payload, file_handle, ensure_ascii=False, indent=2, sort_keys=True)
            file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


async def refresh(args: argparse.Namespace) -> dict[str, Any]:
    if str(MEDIACRAWLER_DIR) not in sys.path:
        sys.path.insert(0, str(MEDIACRAWLER_DIR))
    from media_platform.douyin.client import DouYinClient  # noqa: PLC0415
    from store.douyin import _extract_note_image_assets  # noqa: PLC0415
    from tools import utils  # noqa: PLC0415

    profile_dir = MEDIACRAWLER_DIR / "browser_data" / "dy_user_data_dir"
    if not profile_dir.is_dir():
        raise RuntimeError("Douyin browser profile is missing")
    browser_path = args.browser_path or discover_cdp_browser_path()
    launch: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": True,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "args": [
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            *browser_runtime_args(),
        ],
        "env": browser_launch_environment(),
    }
    if browser_path:
        launch["executable_path"] = browser_path
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(**launch)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            target_url = f"https://www.douyin.com/note/{args.aweme_id}"
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                pass
            await page.wait_for_timeout(1000)
            cookie_urls = [
                "https://douyin.com",
                "https://www.douyin.com",
                "https://creator.douyin.com",
                "https://douhot.douyin.com",
                "https://live.douyin.com",
            ]
            cookie_header, cookie_dict = await utils.convert_browser_context_cookies(
                context,
                urls=cookie_urls,
            )
            client = DouYinClient(
                proxy=None,
                headers={
                    "User-Agent": await page.evaluate("() => navigator.userAgent") or DESKTOP_USER_AGENT,
                    "Cookie": cookie_header,
                    "Host": "www.douyin.com",
                    "Origin": "https://www.douyin.com/",
                    "Referer": target_url,
                    "Content-Type": "application/json;charset=UTF-8",
                },
                playwright_page=page,
                cookie_dict=cookie_dict,
            )
            detail = await client.get_video_by_id(str(args.aweme_id))
            if not isinstance(detail, dict) or str(detail.get("aweme_id") or "") != str(args.aweme_id):
                raise RuntimeError("Douyin detail response did not match the requested aweme")
            assets = _extract_note_image_assets(detail)
            if not assets:
                raise RuntimeError("Douyin detail response has no note image assets")
            return {
                "schema_version": 1,
                "status": "completed",
                "created_at": utc_iso(),
                "aweme_id": str(args.aweme_id),
                "image_assets": assets,
                "image_count": len(assets),
                "video_requests": 0,
                "music_requests": 0,
                "cover_requests": 0,
            }
        finally:
            await context.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output_path = Path(args.output).expanduser().resolve()
    try:
        payload = asyncio.run(refresh(args))
    except Exception as exc:
        payload = {
            "schema_version": 1,
            "status": "failed",
            "created_at": utc_iso(),
            "aweme_id": str(args.aweme_id),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "video_requests": 0,
            "music_requests": 0,
            "cover_requests": 0,
        }
    _write_json_atomic(output_path, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "aweme_id": payload["aweme_id"],
                "image_count": payload.get("image_count", 0),
                "output": str(output_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if payload["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
