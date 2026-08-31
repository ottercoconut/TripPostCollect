#!/usr/bin/env python3
"""Browser navigation helpers for CTF sandbox captures."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError


DOUYIN_COOKIE_HOST_MARKERS = (
    "douyin.com",
    "iesdouyin.com",
    "amemv.com",
    "snssdk.com",
    "bytegoofy.com",
    "bytedance.com",
    "zijieapi.com",
)


def is_douyin_target(site_key: str | None, url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return (site_key or "").lower() == "douyin" or any(marker in host for marker in DOUYIN_COOKIE_HOST_MARKERS)


def clean_douyin_profile_cookies(profile_dir: Path) -> dict[str, Any]:
    event: dict[str, Any] = {
        "target": "douyin",
        "strategy": "sqlite_cookie_rotation",
        "profile_dir": str(profile_dir),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "deleted": 0,
        "cookies": [],
        "ok": True,
    }
    cookie_db = profile_dir / "Default" / "Cookies"
    event["cookie_db"] = str(cookie_db)
    if not cookie_db.exists():
        event["skipped"] = "cookie_db_missing"
        return event

    where = " OR ".join("host_key LIKE ?" for _ in DOUYIN_COOKIE_HOST_MARKERS)
    params = [f"%{marker}%" for marker in DOUYIN_COOKIE_HOST_MARKERS]
    try:
        con = sqlite3.connect(f"file:{cookie_db}?mode=rw", uri=True, timeout=3)
        try:
            rows = con.execute(
                f"SELECT host_key, name, path FROM cookies WHERE {where}",
                params,
            ).fetchall()
            event["cookies"] = [{"host": row[0], "name": row[1], "path": row[2]} for row in rows]
            if rows:
                con.execute(f"DELETE FROM cookies WHERE {where}", params)
                con.commit()
            event["deleted"] = len(rows)
        finally:
            con.close()
    except Exception as exc:
        event["ok"] = False
        event["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        event["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return event


async def clear_douyin_context_cookies(context: BrowserContext) -> dict[str, Any]:
    event: dict[str, Any] = {
        "target": "douyin",
        "strategy": "runtime_context_clear_cookies",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": True,
        "deleted": 0,
    }
    try:
        cookies = await context.cookies()
        related = [
            cookie
            for cookie in cookies
            if any(marker in cookie.get("domain", "").lower() for marker in DOUYIN_COOKIE_HOST_MARKERS)
        ]
        if related:
            await context.clear_cookies()
        event["deleted"] = len(related)
        event["cookies"] = [{"domain": cookie.get("domain"), "name": cookie.get("name")} for cookie in related]
    except Exception as exc:
        event["ok"] = False
        event["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        event["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return event


async def page_readiness_state(page: Page) -> dict[str, Any]:
    return await page.evaluate(
        """() => {
            const bodyText = (document.body && document.body.innerText || '').trim();
            const root = document.querySelector('#root');
            const rootText = root ? (root.innerText || '').trim() : '';
            const imageElements = Array.from(document.images || []);
            const imageCount = imageElements.filter(img => img.currentSrc || img.src).length;
            const loadedImageCount = imageElements.filter(img => img.complete && img.naturalWidth > 0).length;
            const html = document.documentElement ? document.documentElement.outerHTML : '';
            const ssrPattern = /(__SSR|SSR_DATA|INITIAL_STATE|ROUTER_DATA|RENDER_DATA|SIGI_STATE|MODERNJS_ROUTE_MANIFEST|aweme|status_code|flag\\{)/i;
            const hasSsrData = ssrPattern.test(html);
            const hasRoot = !!root && (root.children.length > 0 || rootText.length > 0);
            const readyReasons = [];
            if (bodyText.length > 0) readyReasons.push('body_text');
            if (hasRoot) readyReasons.push('#root');
            if (imageCount > 0) readyReasons.push('image');
            if (hasSsrData) readyReasons.push('ssr_data');
            return {
                ready: readyReasons.length > 0,
                ready_reasons: readyReasons,
                url: location.href,
                title: document.title,
                ready_state: document.readyState,
                body_text_length: bodyText.length,
                root_text_length: rootText.length,
                image_count: imageCount,
                loaded_image_count: loadedImageCount,
                has_root: hasRoot,
                has_ssr_data: hasSsrData,
            };
        }"""
    )


async def wait_for_content_ready(page: Page, timeout_ms: int = 15_000, poll_ms: int = 250) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + max(timeout_ms, poll_ms) / 1000
    last_state: dict[str, Any] = {"ready": False, "ready_reasons": [], "error": ""}
    while True:
        try:
            state = await page_readiness_state(page)
            last_state = state
            if state.get("ready"):
                state["timed_out"] = False
                return state
        except Exception as exc:
            last_state = {"ready": False, "ready_reasons": [], "error": f"{type(exc).__name__}: {exc}"}
        if asyncio.get_running_loop().time() >= deadline:
            last_state["timed_out"] = True
            return last_state
        await page.wait_for_timeout(poll_ms)


async def wait_for_content_enrichment(
    page: Page,
    timeout_ms: int = 8_000,
    poll_ms: int = 350,
    min_body_chars: int = 250,
    min_images: int = 8,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + max(timeout_ms, poll_ms) / 1000
    last_state: dict[str, Any] = {"enriched": False, "error": ""}
    while True:
        try:
            state = await page_readiness_state(page)
            enriched = bool(
                state.get("body_text_length", 0) >= min_body_chars
                or state.get("image_count", 0) >= min_images
                or state.get("loaded_image_count", 0) >= min_images
            )
            state["enriched"] = enriched
            state["enrichment_targets"] = {"min_body_chars": min_body_chars, "min_images": min_images}
            last_state = state
            if enriched:
                state["timed_out"] = False
                return state
        except Exception as exc:
            last_state = {"enriched": False, "error": f"{type(exc).__name__}: {exc}"}
        if asyncio.get_running_loop().time() >= deadline:
            last_state["timed_out"] = True
            return last_state
        await page.wait_for_timeout(poll_ms)


async def navigate_with_commit_and_readiness(
    page: Page,
    url: str,
    *,
    commit_timeout_ms: int,
    readiness_timeout_ms: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "strategy": "commit_then_content_ready",
        "url": url,
        "commit_timeout_ms": commit_timeout_ms,
        "readiness_timeout_ms": readiness_timeout_ms,
        "commit_ok": False,
        "content_ready": False,
        "fatal": False,
        "warnings": [],
    }
    try:
        await page.goto(url, wait_until="commit", timeout=commit_timeout_ms)
        result["commit_ok"] = True
    except PlaywrightTimeoutError as exc:
        result["warnings"].append(f"commit_timeout: {exc}")
    except Exception as exc:
        result["warnings"].append(f"commit_error: {type(exc).__name__}: {exc}")

    readiness = await wait_for_content_ready(page, timeout_ms=readiness_timeout_ms)
    result["readiness"] = readiness
    result["content_ready"] = bool(readiness.get("ready"))
    result["final_url"] = readiness.get("url", page.url)
    if not result["commit_ok"] and not result["content_ready"]:
        result["fatal"] = True
    if not result["content_ready"]:
        result["fatal"] = True
    return result
