#!/usr/bin/env python3
"""Scrapling static preflight helpers for CTF capture scripts."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scrapling import Fetcher, Selector

from project_paths import ensure_dir


MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
FLAG_PATTERNS = (
    re.compile(r"flag\{[^}\r\n]{1,200}\}", re.I),
    re.compile(r"(?:CTF|DASCTF|[A-Z0-9_]{2,20}CTF)\{[^}\r\n]{1,200}\}", re.I),
    re.compile(r"FLAG[-_:][A-Za-z0-9_./+=-]{8,160}", re.I),
    re.compile(r"flag:[A-Za-z0-9_.:-]{4,160}", re.I),
)
FALSE_FLAG_VALUES = {
    "flag_white_list",
    "flag_image_name",
    "flag_essence.jpg",
    "flag:function",
    "flag:decodeuricomponent",
    "flag:e.shapeflag",
    "flag:this.curargs",
    "flag:this.plugincenternewflag",
    "flag:window.wx.menu.plugincenternewflag",
}
CSS_FLAG_MARKERS = (
    "background",
    "border",
    "color:",
    "display",
    "font-size",
    "height",
    "left:",
    "line-height",
    "margin",
    "opacity",
    "padding",
    "position",
    "right:",
    "top:",
    "vertical-align",
    "width",
)
IMAGE_ATTRS = (
    "src",
    "data-src",
    "data-original",
    "data-original-src",
    "data-lazy-src",
    "data-actualsrc",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def decode_body(body: Any, encoding: str | None) -> str:
    if isinstance(body, str):
        return body
    if isinstance(body, (bytes, bytearray)):
        return body.decode(encoding or "utf-8", errors="replace")
    return str(body or "")


def extract_flags(text: str) -> list[str]:
    flags: list[str] = []
    seen: set[str] = set()
    for pattern in FLAG_PATTERNS:
        for match in pattern.findall(text):
            value = match if isinstance(match, str) else match[0]
            if is_plausible_flag(value) and value not in seen:
                seen.add(value)
                flags.append(value)
    return flags


def is_plausible_flag(value: Any) -> bool:
    text = str(value or "").strip()
    if not (4 <= len(text) <= 220):
        return False
    lowered = text.lower()
    if lowered in FALSE_FLAG_VALUES:
        return False
    if lowered.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".css", ".js")):
        return False
    if re.fullmatch(r"flag[_-]?(?:image|image_name|essence)(?:\.\w+)?", lowered):
        return False
    if lowered.startswith("flag:"):
        tail = lowered.split(":", 1)[1]
        if tail in {"function", "decodeuricomponent"} or "shapeflag" in tail or "curargs" in tail:
            return False
        if tail.startswith(("this.", "window.")) or "plugincenternewflag" in tail:
            return False
    if lowered.startswith("flag{"):
        body = lowered[5:-1] if lowered.endswith("}") else lowered[5:]
        if ";" in body or any(marker in body for marker in CSS_FLAG_MARKERS):
            return False
    return True


def douyin_mobile_share_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path
    if "douyin.com" not in host:
        return None
    if re.search(r"/share/note/\d+", path):
        return url
    note_match = re.search(r"/note/(\d+)", path)
    if note_match:
        return f"https://m.douyin.com/share/note/{note_match.group(1)}"
    return None


def should_preflight(site_key: str | None, url: str) -> bool:
    if site_key == "douyin":
        return True
    return bool(douyin_mobile_share_url(url))


def target_preflight_url(site_key: str | None, url: str) -> tuple[str, bool]:
    douyin_url = douyin_mobile_share_url(url)
    if douyin_url:
        return douyin_url, True
    return url, bool(site_key == "douyin")


def image_candidates(selector: Selector, limit: int = 80) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for img in selector.css("img"):
        for attr in IMAGE_ATTRS:
            value = img.attrib.get(attr)
            if not value:
                continue
            url = selector.urljoin(str(value))
            if url.startswith("data:") or url in seen:
                continue
            seen.add(url)
            urls.append(url)
            break
        if len(urls) >= limit:
            break
    return urls


def detect_structured_markers(html: str) -> dict[str, bool]:
    return {
        "router_data": "window._ROUTER_DATA" in html,
        "next_data": "__NEXT_DATA__" in html,
        "douyin_video_info": "videoInfoRes" in html,
        "json_ld": 'type="application/ld+json"' in html or "application/ld+json" in html,
        "cloudflare_challenge": bool(
            re.search(r"challenges\.cloudflare|cf[-_]?turnstile|cf_chl|Just a moment|Cloudflare", html, re.I)
        ),
        "captcha_or_verify": bool(re.search(r"captcha|验证码|安全验证|人机验证|滑块|verification|verify", html, re.I)),
    }


def run_scrapling_static_preflight(
    *,
    url: str,
    site_key: str | None,
    output_dir: Path,
    timeout_seconds: float = 30.0,
    artifact_prefix: str = "scrapling_preflight",
    referer: str | None = None,
) -> dict[str, Any]:
    start = time.monotonic()
    output_dir = ensure_dir(output_dir)
    fetch_url, mobile = target_preflight_url(site_key, url)
    headers: dict[str, str] = {}
    if mobile:
        headers["User-Agent"] = MOBILE_USER_AGENT
        headers["Referer"] = referer or "https://www.douyin.com/"
    elif referer is not None:
        headers["Referer"] = referer

    meta: dict[str, Any] = {
        "enabled": True,
        "started_at": utc_now(),
        "tool": "scrapling",
        "fetcher": "Fetcher",
        "mode": "static",
        "requested_url": url,
        "preflight_url": fetch_url,
        "mobile": mobile,
        "timeout_seconds": timeout_seconds,
        "ok": False,
        "flags": [],
        "artifact_prefix": artifact_prefix,
    }
    try:
        kwargs: dict[str, Any] = {
            "timeout": timeout_seconds,
            "follow_redirects": True,
            "impersonate": "safari_ios" if mobile else "chrome",
            "retries": 1,
        }
        if headers:
            kwargs["headers"] = headers
        response = Fetcher.get(fetch_url, **kwargs)
        body = response.body or b""
        html = decode_body(body, getattr(response, "encoding", None))
        text = str(response.get_all_text(separator="\n", strip=True))
        selector = Selector(html, url=str(getattr(response, "url", fetch_url) or fetch_url))
        flags = extract_flags("\n".join((html, text)))
        images = image_candidates(selector)

        html_path = output_dir / f"{artifact_prefix}.html"
        text_path = output_dir / f"{artifact_prefix}_text.txt"
        html_path.write_text(html, encoding="utf-8")
        text_path.write_text(text, encoding="utf-8")

        meta.update(
            {
                "ok": True,
                "status": getattr(response, "status", None),
                "reason": getattr(response, "reason", ""),
                "final_url": getattr(response, "url", fetch_url),
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "body_bytes": len(body),
                "visible_text_length": len(text),
                "title": str(selector.css("title::text").get(default="")).strip(),
                "flags": flags,
                "flag_count": len(flags),
                "image_candidate_count": len(images),
                "image_candidates": images[:40],
                "structured_markers": detect_structured_markers(html),
                "artifacts": {
                    "html": str(html_path),
                    "visible_text": str(text_path),
                },
            }
        )
    except Exception as exc:
        meta.update(
            {
                "ok": False,
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )

    (output_dir / f"{artifact_prefix}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta
