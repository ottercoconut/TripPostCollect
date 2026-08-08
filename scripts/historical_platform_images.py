#!/usr/bin/env python3
"""Materialize one bounded historical image batch without keyword discovery."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import subprocess
import tempfile
import time
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mediacrawler_crawl import (
    download_bilibili_record_images,
    fetch_remote_image_bytes,
    load_cookie_snapshot,
)
from repair_bilibili_articles import check_bilibili_login
from trippostcollect.artifacts.historical_image_materialization import (
    apply_relationship_plan,
    build_relationship_plan,
    database_integrity,
    projection_inventory,
    promote_downloaded_plan,
    protected_database_digests,
    relationship_source_digest,
    sha256_file,
    sqlite_backup,
    table_digest,
)
from trippostcollect.artifacts.image_candidates import content_image_candidates
from trippostcollect.artifacts.image_manifest import (
    ImageManifestEntry,
    write_manifest_atomic,
)
from trippostcollect.artifacts.image_materialization import (
    DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
    ImageMaterializationError,
    SUPPORTED_IMAGE_MIME_TYPES,
    safe_platform_post_id,
    write_staging_image,
)
from trippostcollect.artifacts.image_proxy import (
    RemoteImageFetchError,
    remote_image_failure_code,
)
from trippostcollect.core.paths import (
    DATA_ROOT,
    DEFAULT_DB,
    IMAGE_MATERIALIZATION_RUNTIME,
    LOCAL_MEDIA_ROOT,
    MEDIACRAWLER_DIR,
    PROJECT_ROOT,
    UV_CACHE_ROOT,
)


DEFAULT_CAMPAIGN = (
    PROJECT_ROOT
    / "docs"
    / "plans"
    / "2026-08-07-historical-image-h00-input-freeze.json"
)
SUPPORTED_PLATFORMS = ("bilibili", "weibo", "zhihu", "douyin")
STAGE_BY_PLATFORM = {
    "bilibili": "h04",
    "weibo": "h05",
    "zhihu": "h06",
    "douyin": "h07",
}
MAX_BATCH_BY_PLATFORM = {
    "bilibili": 50,
    "weibo": 50,
    "zhihu": 20,
    "douyin": 20,
}
DEFAULT_BATCH_TIMEOUT = {
    "bilibili": 3600,
    "weibo": 3600,
    "zhihu": 7200,
    "douyin": 7200,
}
REQUEST_TIMEOUT_SECONDS = {
    "bilibili": 30,
    "weibo": 60,
    "zhihu": 10,
    "douyin": 60,
}
REQUEST_CONCURRENCY = {
    "bilibili": 1,
    "weibo": 2,
    "zhihu": 2,
    "douyin": 1,
}
MAX_IMAGE_ATTEMPTS = 3
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
)
SESSION_COOKIE_MARKERS = {
    "weibo": frozenset({"SUB"}),
    "zhihu": frozenset({"d_c0", "z_c0"}),
    "douyin": frozenset({"sessionid", "sessionid_ss", "sid_tt", "uid_tt"}),
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=SUPPORTED_PLATFORMS, required=True)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--campaign", default=str(DEFAULT_CAMPAIGN))
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--batch-timeout", type=int)
    parser.add_argument("--report")
    parser.add_argument("--backup-dir")
    parser.add_argument("--deferred-posts-file", help=argparse.SUPPRESS)
    parser.add_argument(
        "--max-image-attempts", type=int, default=MAX_IMAGE_ATTEMPTS, help=argparse.SUPPRESS
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


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


def _campaign_id(campaign: dict[str, Any]) -> str:
    campaign_id = str(campaign.get("campaign_id") or "")
    if (
        int(campaign.get("schema_version") or 0) != 1
        or campaign.get("status") != "input_frozen"
        or not campaign_id
    ):
        raise ValueError("campaign is not an H-00 frozen input")
    return campaign_id


def _assert_h00_protected(
    campaign: dict[str, Any], current: dict[str, Any]
) -> None:
    expected = campaign.get("invariants") or {}
    if (
        current.get("web_posts_non_image_sha256")
        != expected.get("web_posts_non_image_sha256")
        or current.get("discovery_table_sha256")
        != expected.get("discovery_table_sha256")
    ):
        raise ValueError("protected post or discovery state differs from H-00")


def _load_raw_records(
    conn: sqlite3.Connection,
    post_ids: Sequence[int],
    *,
    platform_key: str,
) -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    for post_id in post_ids:
        row = conn.execute(
            "SELECT raw_sample_json FROM web_posts WHERE id=? AND platform_key=?",
            (post_id, platform_key),
        ).fetchone()
        if row is None:
            raise ValueError(f"planned historical post disappeared: {post_id}")
        record = json.loads(row[0] or "{}")
        if not isinstance(record, dict):
            raise ValueError(f"historical raw record is not an object: {post_id}")
        records[post_id] = record
    return records


def _bilibili_session() -> tuple[str, dict[str, Any], dict[str, Any]]:
    snapshot = load_cookie_snapshot("bilibili")
    if not snapshot:
        raise RuntimeError(
            "missing Bilibili cookie snapshot; run scripts/login_warmup.py --targets bilibili"
        )
    cookie_header = str(snapshot.get("cookie_header") or "")
    login = check_bilibili_login(cookie_header)
    public_snapshot = {
        key: value for key, value in snapshot.items() if key != "cookie_header"
    }
    return cookie_header, public_snapshot, login


def _cookie_names(snapshot: dict[str, Any]) -> set[str]:
    return {str(value) for value in snapshot.get("cookie_names") or [] if value}


def _weibo_login(cookie_header: str) -> dict[str, Any]:
    checked_at = utc_iso()
    request = Request(
        "https://m.weibo.cn/api/config",
        headers={
            "User-Agent": MOBILE_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://m.weibo.cn/",
            "Cookie": cookie_header,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read(1024 * 1024))
            data = payload.get("data") if isinstance(payload, dict) else {}
            data = data if isinstance(data, dict) else {}
            login = data.get("login") is True
            uid_present = data.get("uid") not in (None, "", 0, "0")
            return {
                "ok": bool(response.status == 200 and payload.get("ok") == 1 and login and uid_present),
                "source": "m_weibo_cn_api_config",
                "http_status": int(response.status),
                "login": login,
                "uid_present": uid_present,
                "checked_at": checked_at,
            }
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "source": "m_weibo_cn_api_config",
            "error": f"{type(exc).__name__}: {exc}",
            "checked_at": checked_at,
        }


def _snapshot_session(platform_key: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    snapshot = load_cookie_snapshot(platform_key)
    if not snapshot:
        raise RuntimeError(
            f"missing {platform_key} cookie snapshot; run scripts/login_warmup.py --targets {platform_key}"
        )
    names = _cookie_names(snapshot)
    markers = SESSION_COOKIE_MARKERS[platform_key]
    if platform_key == "douyin":
        marker_ok = bool(names & markers)
    else:
        marker_ok = markers.issubset(names)
    if not marker_ok:
        raise RuntimeError(
            f"{platform_key} cookie snapshot lacks required login markers; "
            f"run scripts/login_warmup.py --targets {platform_key}"
        )
    cookie_header = str(snapshot.get("cookie_header") or "")
    public_snapshot = {
        key: value for key, value in snapshot.items() if key != "cookie_header"
    }
    if platform_key == "weibo":
        login = _weibo_login(cookie_header)
        if not login.get("ok"):
            raise RuntimeError(
                "Weibo online login check failed; run scripts/login_warmup.py --targets weibo"
            )
    else:
        login = {
            "ok": True,
            "source": "reopened_cookie_snapshot",
            "required_markers_present": True,
            "checked_at": utc_iso(),
        }
    return cookie_header, public_snapshot, login


def _platform_session(platform_key: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if platform_key == "bilibili":
        return _bilibili_session()
    return _snapshot_session(platform_key)


def _session_after(platform_key: str, cookie_header: str) -> dict[str, Any]:
    if platform_key == "bilibili":
        return check_bilibili_login(cookie_header)
    if platform_key == "weibo":
        return _weibo_login(cookie_header)
    return {
        "ok": True,
        "source": "unchanged_reopened_cookie_snapshot",
        "checked_at": utc_iso(),
    }


def _weibo_archive_url(source_url: str) -> str:
    without_scheme = source_url.split("://", 1)[-1]
    parts = without_scheme.split("/")
    if len(parts) < 3:
        return source_url
    parts[1] = "large"
    return f"https://i1.wp.com/{'/'.join(parts)}"


def _record_referer(platform_key: str, record: dict[str, Any], post_id: str) -> str:
    for key in ("content_url", "note_url", "aweme_url"):
        value = str(record.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return {
        "weibo": f"https://m.weibo.cn/detail/{post_id}",
        "zhihu": "https://www.zhihu.com/",
        "douyin": f"https://www.douyin.com/video/{post_id}",
    }[platform_key]


def _archive_headers(
    platform_key: str,
    *,
    cookie_header: str,
    referer: str,
) -> dict[str, str]:
    return {
        "User-Agent": MOBILE_USER_AGENT if platform_key == "weibo" else DESKTOP_USER_AGENT,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer,
        "Cookie": cookie_header,
    }


def _failed_entry(
    candidate: Any,
    *,
    attempts: int,
    http_status: int | None,
    error_code: str,
) -> ImageManifestEntry:
    return ImageManifestEntry(
        schema_version=1,
        platform_key=candidate.platform_key,
        platform_post_id=candidate.platform_post_id,
        image_role=candidate.image_role,
        source_index=candidate.source_index,
        source_key=candidate.source_key,
        source_asset_key=candidate.source_asset_key,
        source_url=candidate.source_url,
        fetch_status="failed",
        attempts=attempts,
        http_status=http_status,
        staging_path=None,
        size_bytes=None,
        mime_type=None,
        width=None,
        height=None,
        sha256=None,
        error_code=error_code,
    )


def _failure_evidence(entries: Sequence[ImageManifestEntry]) -> list[dict[str, Any]]:
    return [
        {
            "source_index": entry.source_index,
            "source_asset_key": entry.source_asset_key,
            "attempts": entry.attempts,
            "http_status": entry.http_status,
            "error_code": entry.error_code,
        }
        for entry in entries
        if entry.fetch_status == "failed"
    ]


def _download_candidate(
    candidate: Any,
    *,
    platform_key: str,
    cookie_header: str,
    referer: str,
    staging_root: Path,
    deadline: float,
    fetch_url_override: str | None = None,
    max_attempts: int = MAX_IMAGE_ATTEMPTS,
    attempts_offset: int = 0,
) -> ImageManifestEntry:
    staging_root = staging_root.expanduser().resolve()
    attempts = 0
    http_status: int | None = None
    fetch_url = fetch_url_override or (
        _weibo_archive_url(candidate.source_url)
        if platform_key == "weibo"
        else candidate.source_url
    )
    headers = _archive_headers(
        platform_key,
        cookie_header=cookie_header,
        referer=referer,
    )
    while attempts < max_attempts:
        if time.monotonic() >= deadline:
            raise TimeoutError("historical image batch timeout reached before image request")
        attempts += 1
        try:
            response = fetch_remote_image_bytes(
                fetch_url,
                headers=headers,
                max_bytes=DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
                timeout_seconds=REQUEST_TIMEOUT_SECONDS[platform_key],
                allowed_media_types=SUPPORTED_IMAGE_MIME_TYPES,
            )
            http_status = response.http_status
            staged = write_staging_image(
                [response.content],
                staging_root=staging_root,
                relative_stem=(
                    f"images/{safe_platform_post_id(candidate.platform_post_id)}/"
                    f"{candidate.source_index:03d}"
                ),
                content_type=response.media_type,
                content_length=len(response.content),
                source_url=response.final_url,
            )
            return ImageManifestEntry(
                schema_version=1,
                platform_key=candidate.platform_key,
                platform_post_id=candidate.platform_post_id,
                image_role=candidate.image_role,
                source_index=candidate.source_index,
                source_key=candidate.source_key,
                source_asset_key=candidate.source_asset_key,
                source_url=candidate.source_url,
                fetch_status="downloaded",
                attempts=attempts_offset + attempts,
                http_status=http_status,
                staging_path=staged.path.relative_to(staging_root).as_posix(),
                size_bytes=staged.size_bytes,
                mime_type=staged.mime_type,
                width=staged.width,
                height=staged.height,
                sha256=staged.sha256,
                error_code=None,
            )
        except RemoteImageFetchError as exc:
            http_status = exc.http_status
            error_code = remote_image_failure_code(exc)
            if exc.retryable and attempts < max_attempts:
                time.sleep(random.uniform(1.0, 2.0) * (2 ** (attempts - 1)))
                continue
            return _failed_entry(
                candidate,
                attempts=attempts_offset + attempts,
                http_status=http_status,
                error_code=error_code,
            )
        except ImageMaterializationError as exc:
            return _failed_entry(
                candidate,
                attempts=attempts_offset + attempts,
                http_status=http_status,
                error_code=exc.code,
            )
        except (OSError, TimeoutError):
            if attempts < max_attempts:
                time.sleep(random.uniform(1.0, 2.0) * (2 ** (attempts - 1)))
                continue
            return _failed_entry(
                candidate,
                attempts=attempts_offset + attempts,
                http_status=http_status,
                error_code="image_download_retryable",
            )
    raise AssertionError("unreachable image attempt state")


def _refresh_douyin_urls(platform_post_id: str, staging_root: Path) -> dict[int, str]:
    output_path = staging_root / f"douyin-refresh-{safe_platform_post_id(platform_post_id)}.json"
    uv = shutil.which("uv")
    if not uv:
        return {}
    command = [
        uv,
        "run",
        "python",
        str(PROJECT_ROOT / "scripts" / "refresh_douyin_image_urls.py"),
        "--aweme-id",
        platform_post_id,
        "--output",
        str(output_path),
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(PROJECT_ROOT / "src"), str(PROJECT_ROOT / "scripts")]
    )
    environment["UV_CACHE_DIR"] = str(UV_CACHE_ROOT)
    result = subprocess.run(
        command,
        cwd=MEDIACRAWLER_DIR,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0 or not output_path.is_file():
        return {}
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        payload.get("status") != "completed"
        or int(payload.get("video_requests") or 0)
        or int(payload.get("music_requests") or 0)
        or int(payload.get("cover_requests") or 0)
    ):
        return {}
    assets = payload.get("image_assets")
    if not isinstance(assets, list):
        return {}
    refreshed: dict[int, str] = {}
    for item in assets:
        if not isinstance(item, dict):
            continue
        try:
            source_index = int(item.get("source_index"))
        except (TypeError, ValueError):
            continue
        url = str(item.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            refreshed[source_index] = url
    return refreshed


def _download_generic_post(
    post: Any,
    record: dict[str, Any],
    *,
    platform_key: str,
    staging_root: Path,
    cookie_header: str,
    deadline: float,
    max_attempts: int = MAX_IMAGE_ATTEMPTS,
) -> tuple[list[ImageManifestEntry], dict[str, Any]]:
    started = time.monotonic()
    referer = _record_referer(platform_key, record, post.platform_post_id)
    candidates = content_image_candidates(platform_key, record)
    detail_refresh_attempted = False
    detail_refresh_succeeded = False
    refreshed_urls: dict[int, str] = {}
    entries: list[ImageManifestEntry] = []
    for candidate in candidates:
        direct_attempts = 1 if platform_key == "douyin" else max_attempts
        entry = _download_candidate(
            candidate,
            platform_key=platform_key,
            cookie_header=cookie_header,
            referer=referer,
            staging_root=staging_root,
            deadline=deadline,
            max_attempts=direct_attempts,
        )
        if platform_key == "douyin" and entry.fetch_status == "failed" and max_attempts > 1:
            if not detail_refresh_attempted:
                detail_refresh_attempted = True
                refreshed_urls = _refresh_douyin_urls(post.platform_post_id, staging_root)
                detail_refresh_succeeded = len(refreshed_urls) == len(candidates)
            refreshed_url = refreshed_urls.get(candidate.source_index)
            if detail_refresh_succeeded and refreshed_url:
                entry = _download_candidate(
                    candidate,
                    platform_key=platform_key,
                    cookie_header=cookie_header,
                    referer=referer,
                    staging_root=staging_root,
                    deadline=deadline,
                    fetch_url_override=refreshed_url,
                    max_attempts=max_attempts - 1,
                    attempts_offset=1,
                )
        entries.append(entry)
    failures = [entry for entry in entries if entry.fetch_status == "failed"]
    return entries, {
        "platform_post_id": post.platform_post_id,
        "expected_images": post.authoritative_images,
        "manifest_rows": len(entries),
        "downloaded_images": len(entries) - len(failures),
        "failed_images": len(failures),
        "failure_codes": sorted({str(entry.error_code or "") for entry in failures}),
        "failures": _failure_evidence(failures),
        "detail_refresh_attempted": detail_refresh_attempted,
        "detail_refresh_succeeded": detail_refresh_succeeded,
        "refreshed_images": len(refreshed_urls),
        "video_requests": 0,
        "music_requests": 0,
        "cover_requests": 0,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _download_generic_batch(
    plan: Any,
    records: dict[int, dict[str, Any]],
    *,
    platform_key: str,
    staging_root: Path,
    cookie_header: str,
    deadline: float,
    max_attempts: int = MAX_IMAGE_ATTEMPTS,
) -> tuple[Path, list[ImageManifestEntry], list[dict[str, Any]]]:
    staging_root = staging_root.expanduser().resolve()
    manifest_path = staging_root / "image_manifest.jsonl"
    staging_root.mkdir(parents=True, exist_ok=True)
    concurrency = REQUEST_CONCURRENCY[platform_key]
    ordered: dict[int, tuple[list[ImageManifestEntry], dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                _download_generic_post,
                post,
                records[post.web_post_id],
                platform_key=platform_key,
                staging_root=staging_root,
                cookie_header=cookie_header,
                deadline=deadline,
                max_attempts=max_attempts,
            ): index
            for index, post in enumerate(plan.posts)
        }
        for future in as_completed(futures):
            ordered[futures[future]] = future.result()
    entries: list[ImageManifestEntry] = []
    post_reports: list[dict[str, Any]] = []
    for index in range(len(plan.posts)):
        post_entries, post_report = ordered[index]
        entries.extend(post_entries)
        post_reports.append(post_report)
    write_manifest_atomic(manifest_path, entries)
    return manifest_path, entries, post_reports


def _download_bilibili_batch(
    plan: Any,
    records: dict[int, dict[str, Any]],
    *,
    staging_root: Path,
    cookie_header: str,
    deadline: float,
    max_attempts: int = MAX_IMAGE_ATTEMPTS,
) -> tuple[Path, list[ImageManifestEntry], list[dict[str, Any]]]:
    manifest_path = staging_root / "image_manifest.jsonl"
    entries: list[ImageManifestEntry] = []
    post_reports: list[dict[str, Any]] = []
    write_manifest_atomic(manifest_path, entries)
    for post in plan.posts:
        if time.monotonic() >= deadline:
            raise TimeoutError("historical Bilibili batch timeout reached before next post")
        started = time.monotonic()
        download_record = dict(records[post.web_post_id])
        download_record["image_urls"] = [
            str(item["url"]) for item in post.prepared_images
        ]
        download_record["detail_image_urls"] = list(download_record["image_urls"])
        download_record["detail_image_count"] = len(download_record["image_urls"])
        post_entries = download_bilibili_record_images(
            download_record,
            cookie_header=cookie_header,
            platform_data_root=staging_root,
            max_attempts=max_attempts,
        )
        entries.extend(post_entries)
        write_manifest_atomic(manifest_path, entries)
        downloaded = sum(entry.fetch_status == "downloaded" for entry in post_entries)
        failures = [entry for entry in post_entries if entry.fetch_status == "failed"]
        post_reports.append(
            {
                "platform_post_id": post.platform_post_id,
                "expected_images": post.authoritative_images,
                "manifest_rows": len(post_entries),
                "downloaded_images": downloaded,
                "failed_images": len(failures),
                "failure_codes": sorted(
                    {str(entry.error_code or "") for entry in failures}
                ),
                "failures": _failure_evidence(failures),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        if len(post_entries) != post.authoritative_images or failures:
            return manifest_path, entries, post_reports
    return manifest_path, entries, post_reports


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    platform_key = args.platform
    max_batch = MAX_BATCH_BY_PLATFORM[platform_key]
    if not 1 <= args.batch_size <= max_batch:
        raise SystemExit(f"--batch-size must be between 1 and {max_batch}")
    if not 1 <= args.max_image_attempts <= MAX_IMAGE_ATTEMPTS:
        raise SystemExit(
            f"--max-image-attempts must be between 1 and {MAX_IMAGE_ATTEMPTS}"
        )
    batch_timeout = int(
        args.batch_timeout or DEFAULT_BATCH_TIMEOUT[platform_key]
    )
    if batch_timeout != DEFAULT_BATCH_TIMEOUT[platform_key]:
        raise SystemExit(
            f"--batch-timeout must equal the H-02 frozen value {DEFAULT_BATCH_TIMEOUT[platform_key]}"
        )
    db_path = Path(args.db).expanduser().resolve()
    if not db_path.is_file():
        raise SystemExit(f"database does not exist: {db_path}")
    campaign_path = Path(args.campaign).expanduser().resolve(strict=True)
    campaign = _read_object(campaign_path)
    campaign_id = _campaign_id(campaign)
    deferred_post_ids: list[str] = []
    if args.deferred_posts_file:
        deferred_payload = _read_object(
            Path(args.deferred_posts_file).expanduser().resolve(strict=True)
        )
        if deferred_payload.get("campaign_id") != campaign_id:
            raise SystemExit("deferred post registry belongs to another campaign")
        platform_values = (deferred_payload.get("platform_post_ids") or {}).get(
            platform_key, []
        )
        if not isinstance(platform_values, list):
            raise SystemExit("deferred post registry platform value must be a list")
        deferred_post_ids = sorted({str(value) for value in platform_values if value})
    run_id = utc_stamp()
    stage = STAGE_BY_PLATFORM[platform_key]
    run_dir = (
        IMAGE_MATERIALIZATION_RUNTIME
        / campaign_id
        / stage
        / f"{platform_key}-download-{run_id}"
    )
    staging_root = run_dir / "staging" / platform_key
    report_path = (
        Path(args.report).expanduser().resolve()
        if args.report
        else run_dir / "report.json"
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "stage": stage.upper(),
        "run_id": run_id,
        "created_at": utc_iso(),
        "mode": "apply" if args.apply else "dry-run",
        "status": "planning",
        "platform": platform_key,
        "batch_size": args.batch_size,
        "batch_timeout_seconds": batch_timeout,
        "keyword_discovery": False,
        "avatar_download": False,
        "video_download": False,
        "preview_image_download": False,
        "max_image_attempts": args.max_image_attempts,
        "deferred_platform_post_ids": deferred_post_ids,
    }
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            integrity_before = database_integrity(conn)
            if integrity_before != {"quick_check": "ok", "foreign_key_violations": 0}:
                raise RuntimeError(f"input database integrity failed: {integrity_before}")
            protected_before = protected_database_digests(conn)
            _assert_h00_protected(campaign, protected_before)
            inventory_before = projection_inventory(conn)
            relationships_before = table_digest(conn, "web_post_images")
            plan = build_relationship_plan(
                conn,
                platforms=(platform_key,),
                excluded_platform_post_ids={platform_key: deferred_post_ids},
                batch_size=args.batch_size,
                project_root=PROJECT_ROOT,
                media_root=LOCAL_MEDIA_ROOT,
                require_missing_local=True,
            )
            records = _load_raw_records(
                conn,
                [post.web_post_id for post in plan.posts],
                platform_key=platform_key,
            )
        planned_images = sum(post.authoritative_images for post in plan.posts)
        report.update(
            {
                "database_sha256_before": sha256_file(db_path),
                "integrity_before": integrity_before,
                "protected_invariants_before": protected_before,
                "content_relationship_sha256_before": relationships_before,
                "inventory_before": inventory_before,
                "planned_posts": len(plan.posts),
                "planned_images": planned_images,
                "planned_platform_post_ids": [post.platform_post_id for post in plan.posts],
            }
        )
        if not plan.posts:
            reason = (
                "no_eligible_missing_platform_images"
                if deferred_post_ids and inventory_before[platform_key]["local_gap"] > 0
                else "no_missing_platform_images"
            )
            report.update({"status": "completed", "reason": reason})
            _write_json_atomic(report_path, report)
            print(json.dumps({"status": "completed", "report": str(report_path)}))
            return 0
        if not args.apply:
            report["status"] = "planned"
            _write_json_atomic(report_path, report)
            print(
                json.dumps(
                    {
                        "status": "planned",
                        "platform": platform_key,
                        "posts": len(plan.posts),
                        "images": planned_images,
                        "report": str(report_path),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0

        backup_root = (
            Path(args.backup_dir).expanduser().resolve()
            if args.backup_dir
            else DATA_ROOT
            / "backups"
            / "historical_images"
            / campaign_id
            / stage
            / f"{platform_key}-download-{run_id}"
        )
        report["backup"] = sqlite_backup(db_path, backup_root / db_path.name)
        cookie_header, session_snapshot, login_before = _platform_session(platform_key)
        report["session_snapshot"] = session_snapshot
        report["login_before"] = login_before
        deadline = time.monotonic() + batch_timeout
        if platform_key == "bilibili":
            manifest_path, entries, post_reports = _download_bilibili_batch(
                plan,
                records,
                staging_root=staging_root,
                cookie_header=cookie_header,
                deadline=deadline,
                max_attempts=args.max_image_attempts,
            )
        else:
            manifest_path, entries, post_reports = _download_generic_batch(
                plan,
                records,
                platform_key=platform_key,
                staging_root=staging_root,
                cookie_header=cookie_header,
                deadline=deadline,
                max_attempts=args.max_image_attempts,
            )
        report["downloaded_posts"] = post_reports
        report["manifest_rows"] = len(entries)
        failed_images = sum(
            int(item["failed_images"]) for item in post_reports
        )
        if len(post_reports) != len(plan.posts) or len(entries) != planned_images or failed_images:
            raise RuntimeError(
                f"{platform_key} image download incomplete: "
                f"posts={len(post_reports)}/{len(plan.posts)} "
                f"manifest={len(entries)}/{planned_images} failed={failed_images}"
            )
        report["login_after"] = _session_after(platform_key, cookie_header)
        if not report["login_after"].get("ok"):
            raise RuntimeError(f"{platform_key} login state failed after image download")
        promoted_plan, promotion = promote_downloaded_plan(
            plan,
            manifest_paths=[manifest_path],
            project_root=PROJECT_ROOT,
            media_root=LOCAL_MEDIA_ROOT,
        )

        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            try:
                if table_digest(conn, "web_post_images") != relationships_before:
                    raise RuntimeError("image relationships changed during historical download")
                if protected_database_digests(conn) != protected_before:
                    raise RuntimeError("protected state changed during historical download")
                if relationship_source_digest(
                    conn, [post.web_post_id for post in plan.posts]
                ) != plan.source_digest:
                    raise RuntimeError("planned historical image source changed before apply")
                apply_result = apply_relationship_plan(conn, promoted_plan)
                integrity_after = database_integrity(conn)
                if integrity_after != {"quick_check": "ok", "foreign_key_violations": 0}:
                    raise RuntimeError(f"database integrity failed after apply: {integrity_after}")
                if protected_database_digests(conn) != protected_before:
                    raise RuntimeError("protected state changed during historical image apply")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            inventory_after = projection_inventory(conn)
            relationships_after = table_digest(conn, "web_post_images")
        expected_gap = inventory_before[platform_key]["local_gap"] - planned_images
        if inventory_after[platform_key]["local_gap"] != expected_gap:
            raise RuntimeError("platform local image gap did not decrease by the planned amount")
        report.update(
            {
                "status": "completed",
                "promotion": promotion,
                "apply_result": apply_result,
                "database_sha256_after_apply": sha256_file(db_path),
                "content_relationship_sha256_after": relationships_after,
                "inventory_after": inventory_after,
                "integrity_after": integrity_after,
                "protected_invariants_after": protected_before,
                "elapsed_seconds": round(batch_timeout - max(0.0, deadline - time.monotonic()), 3),
                "finished_at": utc_iso(),
            }
        )
    except Exception as exc:
        report.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "finished_at": utc_iso(),
            }
        )
    finally:
        report["database_sha256_final"] = sha256_file(db_path)
        _write_json_atomic(report_path, report)

    print(
        json.dumps(
            {
                "status": report["status"],
                "platform": platform_key,
                "posts": report.get("planned_posts", 0),
                "images": report.get("planned_images", 0),
                "report": str(report_path),
                "backup": (report.get("backup") or {}).get("path"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
