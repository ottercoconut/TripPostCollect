#!/usr/bin/env python3
"""Formal Douban Group discovery and page-evidence crawl."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import sqlite3
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import ctf_resource_crawl as resource_crawl
import import_ctf_captures as capture_import
from trippostcollect.core.paths import CTF_RESOURCE_OUTPUT, DEFAULT_DB, ensure_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover and formally crawl Douban Group topics.")
    parser.add_argument("--search-url", required=True, help="Douban Group search/listing URL used for discovery.")
    parser.add_argument("--keyword", required=True, help="Keyword recorded on every topic capture.")
    parser.add_argument("--candidate-hard-limit", required=True, type=int)
    parser.add_argument("--target-new-posts", required=True, type=int)
    parser.add_argument("--max-stagnant-batches", required=True, type=int)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database used for existing-topic checks.")
    parser.add_argument("--output-dir", default=str(CTF_RESOURCE_OUTPUT), help="Output root for evidence artifacts.")
    parser.add_argument("--discovery-page-size", type=int, default=50)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--max-image-save", type=int, default=3)
    parser.add_argument("--max-scrolls", type=int, default=8)
    parser.add_argument("--behavior-profile", default="conservative")
    parser.add_argument("--timeout", type=int, default=60_000)
    parser.add_argument("--commit-timeout", type=int, default=12_000)
    parser.add_argument("--readiness-timeout", type=int, default=15_000)
    parser.add_argument("--settle-min-ms", type=int, default=2_000)
    parser.add_argument("--settle-max-ms", type=int, default=6_000)
    parser.add_argument("--scrapling-preflight", choices=("auto", "off"), default="auto")
    parser.add_argument("--scrapling-preflight-timeout", type=float, default=30.0)
    parser.add_argument("--douyin-cookie-cleanup", choices=("auto", "off"), default="off")
    parser.add_argument("--no-throttle", action="store_true")
    args = parser.parse_args()
    if args.target_new_posts <= 0 or args.candidate_hard_limit <= 0 or args.max_stagnant_batches <= 0:
        raise SystemExit("formal Douban limits must be positive")
    if args.target_new_posts > args.candidate_hard_limit:
        raise SystemExit("target_new_posts exceeds candidate_hard_limit")
    if args.discovery_page_size <= 0:
        raise SystemExit("discovery_page_size must be positive")
    return args


def canonical_topic_url(value: str, base_url: str) -> str | None:
    absolute = urljoin(base_url, html.unescape(value or "").strip())
    if not resource_crawl.is_douban_topic_url(absolute):
        return None
    parsed = urlparse(absolute)
    return urlunparse(("https", parsed.netloc.lower(), parsed.path.rstrip("/") + "/", "", "", ""))


def extract_topic_urls(rendered_html: str, base_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'href\s*=\s*["\']([^"\']+)["\']', rendered_html or "", re.IGNORECASE):
        normalized = canonical_topic_url(match.group(1), base_url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    return urls


def discovery_page_url(search_url: str, offset: int) -> str:
    parsed = urlparse(search_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["start"] = str(offset)
    return urlunparse(parsed._replace(query=urlencode(query)))


def existing_topic_urls(db_path: Path) -> set[str]:
    if not db_path.is_file():
        return set()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT canonical_url, source_url
            FROM web_posts
            WHERE platform_key='douban_group'
            """
        ).fetchall()
    existing: set[str] = set()
    for canonical_url, source_url in rows:
        normalized = canonical_topic_url(str(canonical_url or source_url or ""), "https://www.douban.com/")
        if normalized:
            existing.add(normalized)
    return existing


def capture_is_formally_valid(capture_meta_path: Path) -> tuple[bool, list[str]]:
    row = capture_import.normalize_meta(capture_meta_path)
    validation = json.loads(row["validation_json"])
    reasons = list(validation.get("errors") or [])
    post = capture_import.web_post_for_capture(row, capture_id=0)
    if post is None:
        reasons.append("not_importable_topic_detail")
        return False, reasons
    required = {
        "published_at": post.get("published_at"),
        "author_platform_id": post.get("author_platform_id"),
        "author_display_name": post.get("author_display_name"),
        "content_text": post.get("content_text"),
    }
    reasons.extend(f"missing_{key}" for key, value in required.items() if not value)
    if int(post.get("post_images_count") or 0) <= 0:
        reasons.append("missing_page_images")
    return not reasons, reasons


def configured_target(url: str, *, capture_role: str = "primary", parent_url: str = "") -> dict[str, Any]:
    target = {
        "site": "douban_group",
        "url": url,
        "configured": True,
        "mobile": False,
        "explicit_url": True,
        "capture_role": capture_role,
    }
    if parent_url:
        target["parent_url"] = parent_url
    return target


async def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    batch_dir = ensure_dir(Path(args.output_dir).expanduser() / resource_crawl.utc_stamp())
    db_path = Path(args.db).expanduser()
    existing = existing_topic_urls(db_path)
    discovered: set[str] = set()
    valid_new: list[str] = []
    valid_existing_count = 0
    invalid_candidates: list[dict[str, Any]] = []
    discovery_events: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    conditional_enrichments: list[dict[str, Any]] = []
    stagnant_batches = 0
    stop_reason = "runtime_failed"

    from playwright.async_api import async_playwright

    async with AsyncExitStack() as stack:
        drivers = {"playwright": await stack.enter_async_context(async_playwright())}
        if resource_crawl.target_engine(configured_target(args.search_url)) == "patchright":
            from patchright.async_api import async_playwright as async_patchright

            drivers["patchright"] = await stack.enter_async_context(async_patchright())
        driver = drivers[resource_crawl.target_engine(configured_target(args.search_url))]
        offset = 0
        batch_no = 0
        while len(discovered) < args.candidate_hard_limit and len(valid_new) < args.target_new_posts:
            batch_no += 1
            page_url = discovery_page_url(args.search_url, offset)
            search_capture = await resource_crawl.crawl_one(
                driver,
                configured_target(page_url, capture_role="discovery"),
                batch_dir,
                args,
            )
            records.append(search_capture)
            if search_capture.get("blocked_by_policy"):
                stop_reason = "policy_blocked"
                break
            if not search_capture.get("ok"):
                stop_reason = "runtime_failed"
                break
            rendered_path = Path(str((search_capture.get("artifacts") or {}).get("rendered_html") or ""))
            rendered_html = rendered_path.read_text(encoding="utf-8", errors="replace") if rendered_path.is_file() else ""
            links = extract_topic_urls(rendered_html, page_url)
            new_links = [url for url in links if url not in discovered]
            if not new_links:
                stop_reason = "source_exhausted"
                discovery_events.append(
                    {
                        "type": "adaptive_search_stopped",
                        "batch_no": batch_no,
                        "source_offset": offset,
                        "raw_batch_count": len(links),
                        "candidate_count": len(discovered),
                        "valid_new_count": len(valid_new),
                        "stop_reason": stop_reason,
                    }
                )
                break

            valid_before = len(valid_new)
            for topic_url in new_links:
                if len(discovered) >= args.candidate_hard_limit or len(valid_new) >= args.target_new_posts:
                    break
                discovered.add(topic_url)
                if topic_url in existing:
                    valid_existing_count += 1
                    continue
                topic_target = configured_target(topic_url)
                topic_capture = await resource_crawl.crawl_one(driver, topic_target, batch_dir, args)
                records.append(topic_capture)
                if topic_capture.get("blocked_by_policy"):
                    stop_reason = "policy_blocked"
                    break
                enrichment = await resource_crawl.run_douban_conditional_enrichment(
                    driver,
                    topic_target,
                    topic_capture,
                    batch_dir,
                    args,
                )
                if enrichment is not None:
                    conditional_enrichments.append(enrichment)
                capture_meta_path = Path(str(topic_capture["artifact_dir"])) / "capture_meta.json"
                is_valid, reasons = capture_is_formally_valid(capture_meta_path)
                if is_valid:
                    valid_new.append(topic_url)
                else:
                    invalid_candidates.append({"url": topic_url, "reasons": reasons[:8]})
                await asyncio.sleep(resource_crawl.varied_wait_seconds(2.0, ratio=0.8, floor_seconds=0.5, ceiling_seconds=5.0))
            if stop_reason == "policy_blocked":
                break

            batch_valid_new = len(valid_new) - valid_before
            stagnant_batches = stagnant_batches + 1 if batch_valid_new == 0 else 0
            event = {
                "type": "adaptive_batch_completed",
                "batch_no": batch_no,
                "source_offset": offset,
                "raw_batch_count": len(links),
                "new_candidate_count": len(new_links),
                "candidate_count": len(discovered),
                "valid_new_count": len(valid_new),
                "valid_existing_count": valid_existing_count,
                "batch_valid_new_count": batch_valid_new,
                "stagnant_batches": stagnant_batches,
            }
            discovery_events.append(event)
            if len(valid_new) >= args.target_new_posts:
                stop_reason = "target_new_met"
                break
            if len(discovered) >= args.candidate_hard_limit:
                stop_reason = "candidate_hard_limit_reached"
                break
            if stagnant_batches >= args.max_stagnant_batches:
                stop_reason = "stagnated"
                break
            offset += args.discovery_page_size

    summary = resource_crawl.aggregate(records, batch_dir)
    summary.update(
        {
            "job_kind": "douban_group_search",
            "search_url": args.search_url,
            "keyword": args.keyword,
            "candidate_hard_limit": args.candidate_hard_limit,
            "target_new_posts": args.target_new_posts,
            "candidate_count": len(discovered),
            "valid_new_count": len(valid_new),
            "valid_existing_count": valid_existing_count,
            "invalid_candidate_count": len(invalid_candidates),
            "invalid_candidate_samples": invalid_candidates[:20],
            "stop_reason": stop_reason,
            "discovery_events": discovery_events,
            "conditional_enrichments": conditional_enrichments,
            "conditional_enrichment_count": len(conditional_enrichments),
            "conditional_enrichment_failed_count": sum(1 for item in conditional_enrichments if not item.get("ok")),
            "formal_validation": {
                "new_target_met": len(valid_new) >= args.target_new_posts,
                "candidate_limit_respected": len(discovered) <= args.candidate_hard_limit,
            },
        }
    )
    summary_path = batch_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Summary: {summary_path}")
    return summary, 0 if summary["formal_validation"]["new_target_met"] else 2


def main() -> int:
    args = parse_args()
    _, exit_code = asyncio.run(run(args))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
