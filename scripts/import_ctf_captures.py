#!/usr/bin/env python3
"""Import CTF capture artifacts into SQLite."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from trippostcollect.core.paths import DEFAULT_DB, OUTPUTS_ROOT, PROJECT_ROOT, ensure_parent
from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.records.sanitization import sanitize_author_avatar_data
from trippostcollect.records.topic_relevance import is_topic_relevant


ROOT = PROJECT_ROOT
DEFAULT_OUTPUTS = OUTPUTS_ROOT
FLAG_PATTERN = re.compile(r"(?:flag\{[^}\r\n]{1,200}\}|(?:CTF|DASCTF|[A-Z0-9_]{2,20}CTF)\{[^}\r\n]{1,200}\}|FLAG[-_:][A-Za-z0-9_./+=-]{8,160})", re.I)
CHINA_TZ = timezone(timedelta(hours=8))
TIMESTAMP_MIN = 946_684_800
TIMESTAMP_MAX = 4_102_444_800
DATETIME_TEXT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
)
PUBLISHED_AT_KEYS = (
    "published_at",
    "date_published",
    "datePublished",
    "publish_at",
    "publishTime",
    "publish_time",
    "publish_date",
    "publishedAt",
    "createdAt",
    "created_at",
    "created_time",
    "dateCreated",
    "uploadDate",
)
VISIBLE_TEXT_DATETIME_RE = re.compile(
    r"((?:19|20)\d{2}(?:年\d{1,2}月\d{1,2}日|\-\d{1,2}\-\d{1,2}|/\d{1,2}/\d{1,2})\s+\d{1,2}:\d{2}(?::\d{2})?)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import CTF capture_meta.json artifacts into SQLite.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path.")
    parser.add_argument("--outputs-root", default=str(DEFAULT_OUTPUTS), help="Root containing ctf_* output directories.")
    parser.add_argument("--capture-meta", nargs="+", help="Explicit capture_meta.json files to import.")
    parser.add_argument("--site", help="Optional site filter, for example douyin.")
    parser.add_argument("--only-ok", action="store_true", help="Import only captures with ok=true.")
    return parser.parse_args()


def utc_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def truncate_text(value: str, limit: int = 12000) -> str:
    text = (value or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip()


def load_text(path_value: str | None, limit: int = 12000) -> str:
    if not path_value:
        return ""
    path = Path(path_value)
    if not path.exists():
        return ""
    return truncate_text(path.read_text(encoding="utf-8", errors="replace"), limit=limit)


def parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    try:
        return int(float(text))
    except ValueError:
        return None


def timestamp_to_iso(value: Any) -> str | None:
    parsed = parse_int(value)
    if parsed is None:
        return None
    if parsed > 10_000_000_000:
        parsed = parsed // 1000
    if parsed < TIMESTAMP_MIN or parsed > TIMESTAMP_MAX:
        return None
    try:
        return datetime.fromtimestamp(parsed, CHINA_TZ).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def parse_datetime_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text or text in {"-", "无"}:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in DATETIME_TEXT_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            parsed = None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed.astimezone(CHINA_TZ).isoformat(timespec="seconds")


def datetime_value_to_iso(value: Any) -> str | None:
    return timestamp_to_iso(value) or parse_datetime_text(value)


def iter_nested_values_for_keys(value: Any, keys: tuple[str, ...], *, depth: int = 0) -> list[Any]:
    if depth > 5:
        return []
    found: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and child not in (None, ""):
                found.append(child)
            found.extend(iter_nested_values_for_keys(child, keys, depth=depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.extend(iter_nested_values_for_keys(child, keys, depth=depth + 1))
    return found


def extract_published_at(meta: dict[str, Any]) -> str | None:
    explicit_candidates = [
        meta.get("published_at"),
        (meta.get("published_at_evidence") or {}).get("published_at")
        if isinstance(meta.get("published_at_evidence"), dict)
        else None,
        (meta.get("navigation") or {}).get("published_at") if isinstance(meta.get("navigation"), dict) else None,
        ((meta.get("navigation") or {}).get("readiness") or {}).get("published_at")
        if isinstance(meta.get("navigation"), dict) and isinstance((meta.get("navigation") or {}).get("readiness"), dict)
        else None,
    ]
    evidence = meta.get("published_at_evidence")
    if isinstance(evidence, dict):
        explicit_candidates.append(evidence.get("raw_value"))
        for item in evidence.get("candidates") or []:
            if isinstance(item, dict):
                explicit_candidates.append(item.get("value"))
    for value in explicit_candidates:
        parsed = datetime_value_to_iso(value)
        if parsed:
            return parsed
    for value in iter_nested_values_for_keys(meta.get("payload") or {}, PUBLISHED_AT_KEYS):
        parsed = datetime_value_to_iso(value)
        if parsed:
            return parsed
    return None


def extract_visible_text_published_at(site_key: str, visible_text_path: str | None) -> str | None:
    if site_key != "bilibili":
        return None
    text = load_text(visible_text_path, limit=4000)
    for match in VISIBLE_TEXT_DATETIME_RE.finditer(text):
        parsed = datetime_value_to_iso(match.group(1))
        if parsed:
            return parsed
    return None


def discover_capture_meta(outputs_root: Path) -> list[Path]:
    paths: list[Path] = []
    for pattern in ("ctf_resource_crawls/*/*/capture_meta.json",):
        paths.extend(outputs_root.glob(pattern))
    return sorted(path.resolve() for path in paths)


def infer_capture_kind(path: Path) -> str:
    parts = set(path.parts)
    if "ctf_resource_crawls" in parts:
        return "resource"
    return "flag"


def source_summary_path(path: Path, kind: str) -> Path | None:
    if kind != "resource":
        return None
    candidate = path.parent.parent / "summary.json"
    return candidate if candidate.exists() else None


def summary_captured_at(path: Path, kind: str) -> str | None:
    summary_path = source_summary_path(path, kind)
    if not summary_path:
        return None
    try:
        summary = load_json(summary_path)
    except Exception:
        return None
    captured_at = summary.get("captured_at")
    return str(captured_at) if captured_at else None


def artifact_value(meta: dict[str, Any], key: str) -> str | None:
    artifacts = meta.get("artifacts") or {}
    value = artifacts.get(key)
    return str(value) if value else None


def normalize_meta(path: Path) -> dict[str, Any]:
    meta = load_json(path)
    sanitized_meta = sanitize_author_avatar_data(meta).value
    if not isinstance(sanitized_meta, dict):
        raise ValueError(f"capture metadata must remain an object after sanitization: {path}")
    meta = sanitized_meta
    kind = infer_capture_kind(path)
    target = meta.get("target") or {}
    navigation = meta.get("navigation") or {}
    readiness = navigation.get("readiness") or {}
    enrichment = navigation.get("enrichment") or {}
    image_summary = meta.get("image_summary") or {}
    flags = meta.get("flags") or []
    artifact_dir = str(meta.get("artifact_dir") or path.parent)
    captured_at = (
        meta.get("captured_at")
        or summary_captured_at(path, kind)
        or utc_from_timestamp(path.stat().st_mtime)
    )
    site_key = str(meta.get("site") or target.get("site") or "")
    target_url = str(meta.get("url") or target.get("url") or "")
    visible_text_path = artifact_value(meta, "visible_text")
    published_at = extract_published_at(meta) or extract_visible_text_published_at(site_key, visible_text_path)
    meta_title = readiness.get("title")
    row = {
        "capture_kind": kind,
        "site_key": site_key,
        "target_url": target_url,
        "final_url": navigation.get("final_url") or readiness.get("url"),
        "title": meta_title,
        "published_at": published_at,
        "ok": int(bool(meta.get("ok"))),
        "nav_error": str(meta.get("nav_error") or ""),
        "content_ready": int(bool(navigation.get("content_ready") or readiness.get("ready"))),
        "ready_reasons_json": json_dump(readiness.get("ready_reasons") or []),
        "ready_state": readiness.get("ready_state"),
        "body_text_length": int(readiness.get("body_text_length") or 0),
        "root_text_length": int(readiness.get("root_text_length") or 0),
        "image_count": int(readiness.get("image_count") or 0),
        "loaded_image_count": int(readiness.get("loaded_image_count") or 0),
        "enriched": None if "enriched" not in enrichment else int(bool(enrichment.get("enriched"))),
        "flag_count": len(flags),
        "primary_flag": flags[0] if flags else None,
        "flags_json": json_dump(flags),
        "total_image_requests": int(image_summary.get("total_requests") or 0),
        "successful_image_responses": int(image_summary.get("successful_responses") or 0),
        "http_failed_image_responses": int(image_summary.get("http_failed_responses") or 0),
        "request_failed_images": int(image_summary.get("request_failed") or 0),
        "saved_images": int(image_summary.get("saved_images") or 0),
        "captured_at": str(captured_at),
        "artifact_dir": artifact_dir,
        "capture_meta_path": str(path),
        "rendered_html_path": artifact_value(meta, "rendered_html"),
        "visible_text_path": visible_text_path,
        "network_path": artifact_value(meta, "network"),
        "storage_path": artifact_value(meta, "storage"),
        "images_json_path": artifact_value(meta, "images_json"),
        "failed_images_json_path": artifact_value(meta, "failed_images_json"),
        "screenshot_path": artifact_value(meta, "screenshot"),
        "navigation_json": json_dump(navigation),
        "enrichment_json": json_dump(enrichment),
        "cookie_events_json": json_dump(meta.get("cookie_events") or []),
        "policy_events_json": json_dump(meta.get("policy_events") or meta.get("crawl_policy_events") or []),
        "payload_json": json_dump(meta.get("payload") or {}),
        "image_summary_json": json_dump(image_summary),
        "raw_meta_json": json_dump(meta),
        "source_summary_path": str(source_summary_path(path, kind)) if source_summary_path(path, kind) else None,
    }
    row["validation_json"] = json_dump(validate_row(row, meta))
    return row


def validate_file(path_value: str | None) -> dict[str, Any]:
    if not path_value:
        return {"present": False, "exists": False}
    path = Path(path_value)
    return {"present": True, "exists": path.exists(), "path": str(path), "bytes": path.stat().st_size if path.exists() else None}


def validate_row(row: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    if not row["site_key"]:
        errors.append("missing_site_key")
    if not row["target_url"]:
        errors.append("missing_target_url")
    if not row["artifact_dir"]:
        errors.append("missing_artifact_dir")
    if row["ok"] and not row["content_ready"]:
        warnings.append("ok_without_content_ready")
    flags = json.loads(row["flags_json"])
    invalid_flags = [flag for flag in flags if not FLAG_PATTERN.search(str(flag))]
    if invalid_flags:
        warnings.append("flags_do_not_match_default_regex")
    files = {
        "capture_meta": validate_file(row["capture_meta_path"]),
        "rendered_html": validate_file(row["rendered_html_path"]),
        "visible_text": validate_file(row["visible_text_path"]),
        "network": validate_file(row["network_path"]),
        "storage": validate_file(row["storage_path"]),
        "images_json": validate_file(row["images_json_path"]),
        "failed_images_json": validate_file(row["failed_images_json_path"]),
        "screenshot": validate_file(row["screenshot_path"]),
    }
    for key, info in files.items():
        if info["present"] and not info["exists"]:
            warnings.append(f"missing_artifact_file:{key}")
    image_validation = validate_images(meta)
    warnings.extend(image_validation["warnings"])
    errors.extend(image_validation["errors"])
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "files": files,
        "image_validation": image_validation,
    }


def validate_images(meta: dict[str, Any]) -> dict[str, Any]:
    artifacts = meta.get("artifacts") or {}
    images_path = artifacts.get("images_json")
    failed_path = artifacts.get("failed_images_json")
    image_summary = meta.get("image_summary") or {}
    result = {"checked": False, "warnings": [], "errors": [], "computed": {}}
    if not images_path:
        return result
    path = Path(str(images_path))
    if not path.exists():
        result["warnings"].append("images_json_missing")
        return result
    images = load_json(path)
    failed_images = []
    if failed_path and Path(str(failed_path)).exists():
        failed_images = load_json(Path(str(failed_path)))
    computed = {
        "unclassified_image_records": len(images),
        "unclassified_failed_image_records": len(failed_images),
    }
    if images or failed_images:
        result["warnings"].append("legacy_unclassified_image_records_ignored")
    if int(image_summary.get("saved_images") or 0) != 0:
        result["warnings"].append("legacy_saved_image_count_ignored")
    result["checked"] = True
    result["computed"] = computed
    return result


def ensure_schema(conn: sqlite3.Connection) -> None:
    bootstrap_connection(conn, sync_jobs=False)


def keyword_from_capture(row: dict[str, Any]) -> str | None:
    try:
        raw_meta = json.loads(row.get("raw_meta_json") or "{}")
    except json.JSONDecodeError:
        return None
    keyword = str(raw_meta.get("keyword") or "").strip()
    return keyword or None


def upsert_capture(conn: sqlite3.Connection, row: dict[str, Any]) -> int:
    columns = list(row)
    placeholders = ", ".join(f":{column}" for column in columns)
    update_columns = [column for column in columns if column != "artifact_dir"]
    updates = ", ".join(f"{column}=excluded.{column}" for column in update_columns)
    conn.execute(
        f"""
        INSERT INTO ctf_captures ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(artifact_dir) DO UPDATE SET
            {updates},
            updated_at=datetime('now')
        """,
        row,
    )
    capture_id = conn.execute("SELECT id FROM ctf_captures WHERE artifact_dir = ?", (row["artifact_dir"],)).fetchone()
    if not capture_id:
        raise RuntimeError(f"Failed to resolve ctf_capture row for {row['artifact_dir']}")
    return int(capture_id[0])


def replace_capture_images(conn: sqlite3.Connection, capture_id: int, _images_json_path: str | None) -> int:
    conn.execute("DELETE FROM ctf_capture_images WHERE ctf_capture_id = ?", (capture_id,))
    return 0


def capture_post_id(site_key: str, capture_id: int) -> str:
    return f"capture:{site_key}:{capture_id}"


def web_post_for_capture(row: dict[str, Any], capture_id: int) -> dict[str, Any] | None:
    site_key = str(row["site_key"])
    visible_text = load_text(row.get("visible_text_path"))
    title = str(row.get("title") or "").strip()
    content_text = visible_text or title
    if not row["ok"]:
        return None
    if not row["content_ready"]:
        return None
    keyword = keyword_from_capture(row)
    if not content_text:
        return None
    try:
        raw_sample = json.loads(row.get("raw_meta_json") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("capture raw_meta_json must be valid JSON") from exc
    sanitized_raw_sample = sanitize_author_avatar_data(raw_sample).value
    metrics = {
        "capture_kind": row.get("capture_kind"),
        "body_text_length": row.get("body_text_length"),
        "root_text_length": row.get("root_text_length"),
        "image_count": row.get("image_count"),
        "loaded_image_count": row.get("loaded_image_count"),
        "flag_count": row.get("flag_count"),
        "saved_images": row.get("saved_images"),
    }
    author = {
        "source": "ctf_capture",
        "follower_count_available": False,
    }
    return {
        "platform_key": site_key,
        "source_capture_id": capture_id,
        "platform_post_id": capture_post_id(site_key, capture_id),
        "source_type": "ctf_capture_page",
        "source_url": row["target_url"],
        "canonical_url": row.get("final_url") or row["target_url"],
        "title": title or None,
        "author_display_name": None,
        "author_platform_id": None,
        "author_profile_url": None,
        "author_description": None,
        "author_followers_count": None,
        "author_following_count": None,
        "author_posts_count": None,
        "author_platform_level": None,
        "author_verified": None,
        "author_verified_text": None,
        "published_at": row.get("published_at"),
        "captured_at": row["captured_at"],
        "keyword": keyword,
        "topic_relevant": int(
            is_topic_relevant(title=title, content_text=content_text, keyword=keyword)
        ),
        "content_text": content_text,
        "content_length": len(content_text),
        "post_likes_count": None,
        "post_favorites_count": None,
        "post_comments_count": None,
        "post_shares_count": None,
        "post_reposts_count": None,
        "post_views_count": None,
        "post_images_count": 0,
        "metrics_json": json_dump(metrics),
        "author_json": json_dump(author),
        "raw_sample_json": json_dump(sanitized_raw_sample),
        "artifact_dir": row["artifact_dir"],
        "capture_method": "import",
        "status": "captured" if row["ok"] else "partial",
    }


def find_existing_web_post(conn: sqlite3.Connection, row: dict[str, Any]) -> int | None:
    if row.get("source_capture_id"):
        found = conn.execute("SELECT id FROM web_posts WHERE source_capture_id=?", (row["source_capture_id"],)).fetchone()
        if found:
            return int(found[0])
    if row.get("platform_post_id"):
        found = conn.execute(
            "SELECT id FROM web_posts WHERE platform_key=? AND platform_post_id=?",
            (row["platform_key"], row["platform_post_id"]),
        ).fetchone()
        if found:
            return int(found[0])
    if row.get("canonical_url"):
        found = conn.execute(
            "SELECT id FROM web_posts WHERE platform_key=? AND canonical_url=?",
            (row["platform_key"], row["canonical_url"]),
        ).fetchone()
        if found:
            return int(found[0])
    return None


def upsert_web_post_from_capture(conn: sqlite3.Connection, row: dict[str, Any]) -> int:
    existing_id = find_existing_web_post(conn, row)
    columns = list(row)
    if existing_id:
        updates = ", ".join(f"{column}=:{column}" for column in columns)
        conn.execute(f"UPDATE web_posts SET {updates}, updated_at=datetime('now') WHERE id=:id", {**row, "id": existing_id})
        return existing_id
    placeholders = ", ".join(f":{column}" for column in columns)
    conn.execute(f"INSERT INTO web_posts ({', '.join(columns)}) VALUES ({placeholders})", row)
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def replace_web_post_images_from_capture(
    conn: sqlite3.Connection,
    web_post_id: int,
    _images_json_path: str | None,
) -> int:
    conn.execute("DELETE FROM web_post_images WHERE web_post_id=?", (web_post_id,))
    return 0


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser()
    db_path = ensure_parent(db_path)
    if args.capture_meta:
        capture_paths = [Path(path).expanduser().resolve() for path in args.capture_meta]
    else:
        capture_paths = discover_capture_meta(Path(args.outputs_root).expanduser())
    if not capture_paths:
        raise SystemExit("No CTF capture_meta.json files found.")

    imported = 0
    image_rows = 0
    post_rows = 0
    inserted_rows = 0
    updated_rows = 0
    post_image_rows = 0
    skipped = 0
    failures: list[str] = []
    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn)
        for path in capture_paths:
            row = normalize_meta(path)
            raw_meta = json.loads(row["raw_meta_json"])
            if raw_meta.get("skipped"):
                skipped += 1
                continue
            if args.site and row["site_key"] != args.site:
                skipped += 1
                continue
            if args.only_ok and not row["ok"]:
                skipped += 1
                continue
            validation = json.loads(row["validation_json"])
            if validation["errors"]:
                failures.append(f"{path}: {validation['errors']}")
                continue
            capture_id = upsert_capture(conn, row)
            image_rows += replace_capture_images(conn, capture_id, row.get("images_json_path"))
            post_row = web_post_for_capture(row, capture_id)
            if post_row:
                existing_post_id = find_existing_web_post(conn, post_row)
                web_post_id = upsert_web_post_from_capture(conn, post_row)
                if existing_post_id is None:
                    inserted_rows += 1
                else:
                    updated_rows += 1
                inserted_post_images = replace_web_post_images_from_capture(conn, web_post_id, row.get("images_json_path"))
                conn.execute(
                    "UPDATE web_posts SET post_images_count=0, updated_at=datetime('now') WHERE id=?",
                    (web_post_id,),
                )
                post_image_rows += inserted_post_images
                post_rows += 1
            imported += 1
        conn.commit()

    print(
        json.dumps(
            {
                "db": str(db_path),
                "imported": imported,
                "image_rows": image_rows,
                "post_rows": post_rows,
                "inserted_rows": inserted_rows,
                "updated_rows": updated_rows,
                "post_image_rows": post_image_rows,
                "skipped": skipped,
                "failed": len(failures),
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
