#!/usr/bin/env python3
"""页面证据专用的浏览器辅助流程。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import BrowserContext


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

