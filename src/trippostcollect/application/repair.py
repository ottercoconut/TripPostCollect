"""历史详情修复输入、证据与完成判定。"""

from __future__ import annotations


import json
import os
import re
import secrets
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from trippostcollect.artifacts.image_staging import MAX_IMAGE_BYTES, ImageStagingError, inspect_image_bytes
from trippostcollect.core import paths as core_paths

# 与 platforms/zhihu/models.py 的同名集合保持一致。应用层不导入知乎包，避免未选中知乎时加载它。
ADMIT_UNAVAILABLE_CODES = frozenset({
    "admit_redirect_home",
    "admit_redirect_article",
    "admit_not_found",
    "admit_id_mismatch",
    "admit_redirect_unexpected",
})


# #211：这些作者字段必须来自本次 sideUser，修复回退不得从旧行带回。
WEIBO_SIDE_USER_FIELDS = frozenset({
    "author_followers_count",
    "author_followers_source",
    "followers_observed",
    "followers_count",
    "author_description",
    "author_following_count",
    "author_posts_count",
    "following_count",
    "posts_count",
})

MISSING_WEIBO_AUTHOR_PLATFORM_ID = "missing_author_platform_id"


def without_weibo_side_user_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if key not in WEIBO_SIDE_USER_FIELDS}


def reject_keep_stored_images_scope(*, enabled: bool, post_repair: bool, platforms: list[str]) -> None:
    if enabled and (not post_repair or platforms != ["weibo"]):
        raise SystemExit("--keep-stored-images requires --post-repair and --platforms weibo")


def load_stored_content_images(
    db_path: str | Path | None,
    platform_key: str,
    post_ids: set[str],
) -> list[dict[str, Any]]:
    """只读查出本批可复用的正文图。同帖同键有两个 sha256 时丢掉该键。

    ``sha256_duplicate_sources`` 里的键映射到同一文件。worker 不读数据库。
    """

    if not db_path or not post_ids:
        return []
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        return []
    grouped: dict[tuple[str, str], dict[tuple[Any, ...], dict[str, Any]]] = {}

    def consider(post_id: str, key: str, entry: dict[str, Any]) -> None:
        asset_key = str(key or "").strip()
        if not post_id or not asset_key:
            return
        identity = (
            entry["sha256"],
            entry["local_path"],
            entry["mime_type"],
            entry["width"],
            entry["height"],
        )
        slot = grouped.setdefault((post_id, asset_key), {})
        slot[identity] = {**entry, "platform_post_id": post_id, "source_asset_key": asset_key}

    wanted = sorted({str(value).strip() for value in post_ids if str(value).strip()})
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for offset in range(0, len(wanted), 500):
            batch = wanted[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT p.platform_post_id, i.sha256, i.local_path, i.mime_type,
                       i.width, i.height, i.raw_image_json
                FROM web_post_images AS i
                JOIN web_posts AS p ON p.id = i.web_post_id
                WHERE p.platform_key = ?
                  AND p.platform_post_id IN ({placeholders})
                  AND i.image_role = 'content'
                  AND COALESCE(i.sha256, '') <> ''
                  AND COALESCE(i.local_path, '') <> ''
                  AND COALESCE(i.mime_type, '') <> ''
                  AND i.width IS NOT NULL AND i.width > 0
                  AND i.height IS NOT NULL AND i.height > 0
                ORDER BY p.platform_post_id, i.image_index
                """,
                [platform_key, *batch],
            ).fetchall()
            for row in rows:
                post_id = str(row["platform_post_id"] or "").strip()
                try:
                    raw = json.loads(str(row["raw_image_json"] or ""))
                except (TypeError, json.JSONDecodeError):
                    raw = {}
                if not isinstance(raw, dict):
                    raw = {}
                entry = {
                    "platform_post_id": post_id,
                    "source_asset_key": "",
                    "sha256": str(row["sha256"]),
                    "local_path": str(row["local_path"]),
                    "mime_type": str(row["mime_type"]),
                    "width": int(row["width"]),
                    "height": int(row["height"]),
                }
                consider(post_id, str(raw.get("source_asset_key") or ""), entry)
                local_file = raw.get("local_file")
                duplicates = local_file.get("sha256_duplicate_sources") if isinstance(local_file, dict) else None
                if not isinstance(duplicates, list):
                    continue
                for duplicate in duplicates:
                    if not isinstance(duplicate, dict):
                        continue
                    consider(post_id, str(duplicate.get("source_asset_key") or ""), entry)
    kept = [
        next(iter(identities.values()))
        for identities in grouped.values()
        if len(identities) == 1
    ]
    kept.sort(key=lambda item: (item["platform_post_id"], item["source_asset_key"]))
    return kept


def load_stored_image_index(path: str | Path) -> dict[tuple[str, str], dict[str, Any]]:
    """(platform_post_id, source_asset_key) -> 清单项。读失败时返回空表。"""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, list):
        return {}
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        post_id = str(item.get("platform_post_id") or "").strip()
        key = str(item.get("source_asset_key") or "").strip()
        if post_id and key:
            index[(post_id, key)] = item
    return index


def _symlink_component(path: Path) -> bool:
    current = path
    for _ in range(64):
        if current.is_symlink():
            return True
        if current.parent == current:
            return False
        current = current.parent
    return True


def open_stored_image(
    entry: dict[str, Any],
    *,
    project_root: Path | None = None,
    media_root: Path | None = None,
) -> tuple[bytes, Path] | str:
    """校验通过时返回 (字节, 文件路径)；否则返回原因码，不含路径与正文。"""

    root = project_root if project_root is not None else core_paths.PROJECT_ROOT
    media = media_root if media_root is not None else core_paths.LOCAL_MEDIA_ROOT
    text = str(entry.get("local_path") or "")
    raw = Path(text)
    if not text or raw.is_absolute():
        return "path_escape"
    candidate = root / raw
    if _symlink_component(candidate):
        return "symlink"
    if not candidate.is_file():
        return "missing_file"
    try:
        media_resolved = media.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError:
        return "path_escape"
    if resolved != media_resolved and media_resolved not in resolved.parents:
        return "path_escape"
    try:
        content = resolved.read_bytes()
    except OSError:
        return "missing_file"
    if not content or len(content) > MAX_IMAGE_BYTES:
        return "size_limit"
    if sha256(content).hexdigest() != str(entry.get("sha256") or ""):
        return "sha256_mismatch"
    try:
        inspected = inspect_image_bytes(content)
    except ImageStagingError as exc:
        return "size_limit" if exc.code == "image_too_large" else "format_mismatch"
    try:
        expected_width = int(entry.get("width"))
        expected_height = int(entry.get("height"))
    except (TypeError, ValueError):
        return "dimension_mismatch"
    if (
        inspected.mime_type != str(entry.get("mime_type") or "")
        or inspected.width != expected_width
        or inspected.height != expected_height
    ):
        return "dimension_mismatch"
    return content, resolved


def write_stored_content_images(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    """把旧图清单原子写到本批目录。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(rows, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    part = target.with_name(f".{target.name}.{secrets.token_hex(4)}.part")
    with part.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(part, target)
    return target


def load_post_repair_fallbacks(
    db_path: str | Path | None,
    platform_key: str,
    post_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not db_path or not post_ids:
        return {}
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        return {}
    placeholders = ",".join("?" for _ in post_ids)
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT *
                FROM web_posts
                WHERE platform_key=? AND platform_post_id IN ({placeholders})
                """,
                [platform_key, *sorted(post_ids)],
            ).fetchall()
    except sqlite3.Error:
        return {}

    fallbacks: dict[str, dict[str, Any]] = {}
    for row in rows:
        columns = set(row.keys())

        def row_value(key: str) -> Any:
            return row[key] if key in columns else None

        def available(*values: Any) -> Any:
            return next((value for value in values if value not in (None, "")), None)

        raw: dict[str, Any] = {}
        try:
            parsed = json.loads(str(row_value("raw_sample_json") or ""))
            if isinstance(parsed, dict):
                raw = parsed
        except (TypeError, json.JSONDecodeError):
            pass
        if platform_key == "zhihu":
            # #194：只回落发帖时间。作者字段必须来自本次作者页。
            fallback = {
                key: value
                for key, value in {
                    "keyword": row_value("keyword"),
                    "published_at": row_value("published_at"),
                    "created_time": raw.get("created_time"),
                }.items()
                if value not in (None, "")
            }
            fallbacks[str(row["platform_post_id"])] = fallback
            continue
        fallback = {
            key: value
            for key, value in {
                "keyword": row_value("keyword"),
                "published_at": row_value("published_at"),
                "author_followers_count": row_value("author_followers_count"),
                "author_display_name": row_value("author_display_name"),
                "author_platform_id": row_value("author_platform_id"),
                "author_profile_url": row_value("author_profile_url"),
                "author_description": row_value("author_description"),
                "created_time": raw.get("created_time"),
                "updated_time": raw.get("updated_time"),
                "creator_url_token": raw.get("creator_url_token"),
                "user_nickname": raw.get("user_nickname"),
                "author_followers_source": raw.get("author_followers_source"),
                "followers_observed": raw.get("followers_observed"),
                "followers_count": raw.get("followers_count"),
                "liked_count": available(
                    row_value("post_likes_count"),
                    raw.get("liked_count"),
                ),
                "collected_count": available(
                    row_value("post_favorites_count"),
                    raw.get("collected_count"),
                    raw.get("favorites_count"),
                ),
                "comment_count": available(
                    row_value("post_comments_count"),
                    raw.get("comment_count"),
                    raw.get("comments_count"),
                ),
                "share_count": available(
                    row_value("post_shares_count"),
                    raw.get("share_count"),
                    raw.get("shares_count"),
                ),
            }.items()
            if value not in (None, "")
        }
        if platform_key == "weibo":
            fallback = without_weibo_side_user_fields(fallback)
        fallbacks[str(row["platform_post_id"])] = fallback
    return fallbacks



def load_post_repair_targets(
    path_value: str | Path,
    platform_key: str,
    *,
    db_path: str | Path | None = None,
    admit: bool = False,
) -> list[dict[str, Any]]:
    if platform_key not in {"weibo", "zhihu"}:
        raise SystemExit(f"unsupported post repair platform: {platform_key}")
    if admit and platform_key != "zhihu":
        raise SystemExit("--admit-seen requires zhihu repair targets")
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid post repair target file: {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit("post repair target file must contain a JSON array")

    targets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    raw_targets: list[tuple[str, str, str, dict[str, Any]]] = []
    for index, value in enumerate(payload):
        if not isinstance(value, dict):
            raise SystemExit(f"post repair target {index} must be an object")
        post_id = str(value.get("platform_post_id") or "").strip()
        detail_target = str(value.get("detail_target") or "").strip()
        keyword = str(value.get("keyword") or "").strip()
        if not post_id or not detail_target or not keyword:
            raise SystemExit(
                f"post repair target {index} requires platform_post_id, detail_target, and keyword"
            )
        if post_id in seen_ids:
            raise SystemExit(f"duplicate post repair platform_post_id: {post_id}")
        flagged = value.get("admit") is True
        if admit and not flagged:
            raise SystemExit(f"post repair target {index} requires admit: true")

        if platform_key == "weibo":
            valid = bool(
                re.fullmatch(r"https://weibo\.com/[0-9A-Za-z]+/" + re.escape(post_id), detail_target)
                and re.fullmatch(r"[0-9]+", post_id)
            )
        else:
            parsed = urlparse(detail_target.split("#", 1)[0].split("?", 1)[0])
            answer = bool(
                parsed.hostname in {"zhihu.com", "www.zhihu.com"}
                and re.fullmatch(
                    rf"/question/[^/]+/answer/{re.escape(post_id)}/?",
                    parsed.path,
                )
            )
            article = bool(
                parsed.hostname == "zhuanlan.zhihu.com"
                and re.fullmatch(rf"/p/{re.escape(post_id)}/?", parsed.path)
            )
            bare_answer = bool(
                parsed.scheme == "https"
                and parsed.hostname in {"zhihu.com", "www.zhihu.com"}
                and re.fullmatch(rf"/answer/{re.escape(post_id)}/?", parsed.path)
                and re.fullmatch(r"[0-9]+", post_id)
            )
            # /answer/{id} 只在 admit 模式且条目带标志时接受。其他模式仍只认原两种形式。
            valid = bool(parsed.scheme == "https" and (answer or article or (admit and flagged and bare_answer)))
        if not valid:
            raise SystemExit(
                f"post repair target does not match {platform_key} ID {post_id}: {detail_target!r}"
            )
        seen_ids.add(post_id)
        payload_fallback = {} if admit else value.get("repair_fallback")
        raw_targets.append(
            (
                post_id,
                detail_target,
                keyword,
                payload_fallback if isinstance(payload_fallback, dict) else {},
            )
        )
    fallbacks = (
        {}
        if admit
        else load_post_repair_fallbacks(
            db_path,
            platform_key,
            {post_id for post_id, _, _, _ in raw_targets},
        )
    )
    for post_id, detail_target, keyword, payload_fallback in raw_targets:
        value = {
            "platform_post_id": post_id,
            "detail_target": detail_target,
            "keyword": keyword,
        }
        if admit:
            value["admit"] = True
        merged_fallback = {**payload_fallback, **fallbacks.get(post_id, {})}
        if merged_fallback:
            value["repair_fallback"] = merged_fallback
        targets.append(value)
    if not targets:
        raise SystemExit("post repair target file contains no targets")
    return targets



def load_xhs_repair_report(path_value: str | Path | None) -> dict[str, Any]:
    if not path_value:
        return {}
    path = Path(path_value).expanduser()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}



def xhs_repair_pagination_evidence(
    records: list[dict[str, Any]],
    *,
    target_count: int,
) -> dict[str, Any]:
    reports = [
        record.get("repair_report")
        for record in records
        if isinstance(record, dict)
        and record.get("platform") == "xhs"
        and isinstance(record.get("repair_report"), dict)
    ]
    batches: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    successful_ids: set[str] = set()
    for report in reports:
        batches.extend(
            value for value in report.get("batches") or [] if isinstance(value, dict)
        )
        failures.extend(
            value
            for value in report.get("candidate_failures") or []
            if isinstance(value, dict)
        )
        successful_ids.update(
            str(value) for value in report.get("successful_ids") or [] if str(value)
        )
    return {
        "available": True,
        "stopped": True,
        "stop_reason": "repair_targets_processed",
        "stop_detail": "specified_detail_targets",
        "candidate_count": target_count,
        "batches": batches,
        "successful_candidate_count": len(successful_ids),
        "skipped_candidate_count": len(failures),
        "skipped_candidate_failures": failures,
    }



_PAGE_KINDS = frozenset({"detail", "login", "error_page", "search", "other", "closed"})


def _clean_evidence(raw: Any) -> dict[str, Any] | None:
    """只保留详情证据的登记键。地址、正文和其他字段丢掉。"""

    if not isinstance(raw, dict):
        return None
    unmatched: list[dict[str, Any]] = []
    items = raw.get("show_responses_unmatched")
    if isinstance(items, list):
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            ok = item.get("ok")
            body_error = item.get("body_error")
            unmatched.append({
                "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
                "ok": ok if isinstance(ok, bool) or (isinstance(ok, int) and not isinstance(ok, bool)) else None,
                "id_matched": bool(item.get("id_matched")),
                "body_error": body_error if isinstance(body_error, str) and body_error.isidentifier() else "",
            })
    kind = raw.get("final_page_kind")
    nav = raw.get("navigation_status")
    waited = raw.get("waited_seconds")
    failures = raw.get("show_request_failures")
    pending = raw.get("show_response_pending")
    waited_value = None
    if isinstance(waited, (int, float)) and not isinstance(waited, bool):
        waited_value = float(round(float(waited), 1))
    if not any(key in raw for key in (
        "navigation_status",
        "final_page_kind",
        "show_responses_unmatched",
        "show_request_failures",
        "show_response_pending",
        "waited_seconds",
    )):
        return None
    return {
        "navigation_status": nav if isinstance(nav, int) and not isinstance(nav, bool) else None,
        "final_page_kind": kind if isinstance(kind, str) and kind in _PAGE_KINDS else None,
        "show_responses_unmatched": unmatched,
        "show_request_failures": (
            failures if isinstance(failures, int) and not isinstance(failures, bool) and failures >= 0 else 0
        ),
        "show_response_pending": pending if isinstance(pending, bool) else None,
        "waited_seconds": waited_value,
    }


def clean_repair_outcome(item: Any) -> dict[str, Any] | None:
    """子进程记录缺字段、不是布尔可重试标记或夹带 URL 时丢弃，调用方退回旧码。"""

    if not isinstance(item, dict):
        return None
    post_id = item.get("platform_post_id")
    error_code = item.get("error_code")
    evidence_source = item.get("evidence_source")
    attempts = item.get("attempts")
    retryable = item.get("retryable")
    if not all(isinstance(value, str) and value.strip() and "://" not in value for value in (
        post_id, error_code, evidence_source,
    )):
        return None
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        return None
    if not isinstance(retryable, bool):
        return None
    cleaned: dict[str, Any] = {
        "platform_post_id": str(post_id).strip(),
        "error_code": str(error_code).strip(),
        "evidence_source": str(evidence_source).strip(),
        "attempts": attempts,
        "retryable": retryable,
    }
    if "evidence" in item:
        evidence = _clean_evidence(item.get("evidence"))
        if evidence is None:
            return None
        cleaned["evidence"] = evidence
    return cleaned


def load_weibo_repair_outcomes(path_value: str | Path | None) -> list[dict[str, Any]]:
    """文件缺失、损坏或某条记录不合格时，该条不采用。不补造原因。"""

    if not path_value:
        return []
    path = Path(path_value).expanduser()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    raw_items = value.get("outcomes") if isinstance(value, dict) else None
    if not isinstance(raw_items, list):
        return []
    cleaned = []
    for item in raw_items:
        outcome = clean_repair_outcome(item)
        if outcome is not None:
            cleaned.append(outcome)
    return cleaned


def _posted_repair_outcomes(
    records: list[dict[str, Any]],
    platform: str,
) -> list[dict[str, Any]] | None:
    found: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or record.get("platform") != platform:
            continue
        raw = record.get("repair_outcomes")
        if not isinstance(raw, list):
            continue
        for item in raw:
            outcome = clean_repair_outcome(item)
            if outcome is not None:
                found.append(outcome)
    return found or None


def weibo_posted_repair_outcomes(
    records: list[dict[str, Any]],
    platform: str,
) -> list[dict[str, Any]] | None:
    """只读取微博子进程写回的记录。其他平台保持原有差额口径。"""

    if platform != "weibo":
        return None
    return _posted_repair_outcomes(records, "weibo")


def zhihu_posted_repair_outcomes(
    records: list[dict[str, Any]],
    platform: str,
) -> list[dict[str, Any]] | None:
    """只读取知乎 admit 子进程写回的不可得记录。没有记录时保持原差额口径。"""

    if platform != "zhihu":
        return None
    return _posted_repair_outcomes(records, "zhihu")


def post_repair_successful_identities(
    validation: dict[str, Any],
    *,
    admit: bool = False,
) -> set[str]:
    """修复成功集。admit 才把本轮新行算进成功，其他模式仍只认已在库中的行。"""

    found = {str(item) for item in (validation.get("existing_identities") or []) if str(item)}
    if admit:
        found.update(str(item) for item in (validation.get("new_identities") or []) if str(item))
    return found


def post_repair_pagination_evidence(
    targets: list[dict[str, Any]],
    *,
    platform: str,
    successful_identities: set[str],
    materialization_failures: list[dict[str, Any]] | None = None,
    target_outcomes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    target_ids = {
        str(target.get("platform_post_id") or "")
        for target in targets
        if str(target.get("platform_post_id") or "")
    }
    successful_ids = {
        identity.rsplit(":id:", 1)[-1]
        for identity in successful_identities
        if identity.startswith(f"{platform}:id:")
    } & target_ids
    failure_by_id: dict[str, dict[str, Any]] = {}
    for failure in materialization_failures or []:
        identity = str(failure.get("identity") or "")
        post_id = identity.rsplit(":id:", 1)[-1] if ":id:" in identity else identity
        if post_id:
            failure_by_id[post_id] = failure
    outcome_by_id: dict[str, dict[str, Any]] = {}
    for item in target_outcomes or []:
        outcome = clean_repair_outcome(item)
        if outcome is None:
            continue
        outcome_by_id.setdefault(outcome["platform_post_id"], outcome)

    failures: list[dict[str, Any]] = []
    unavailable_ids: list[str] = []
    for target in targets:
        post_id = str(target.get("platform_post_id") or "")
        if not post_id or post_id in successful_ids:
            continue
        materialization_failure = failure_by_id.get(post_id) or {}
        # 物化失败仍优先。没有物化失败时，才用子进程写出的细记录填充；没有记录则保持旧码。
        outcome = {} if materialization_failure else outcome_by_id.get(post_id) or {}
        error_code = str(
            materialization_failure.get("error_code")
            or materialization_failure.get("code")
            or outcome.get("error_code")
            or "repair_target_no_valid_output"
        )
        detail = str(
            materialization_failure.get("detail")
            or materialization_failure.get("message")
            or "explicit repair target produced no formally valid persisted detail record"
        )
        failure_scope = str(
            materialization_failure.get("failure_scope")
            or ("image" if materialization_failure or outcome.get("evidence_source") == "weibo_image" else "post")
        )
        if materialization_failure:
            evidence_source = "image_materialization"
            retryable = bool(materialization_failure.get("retryable", False))
            attempts = max(1, int(materialization_failure.get("attempts") or 1))
        elif outcome:
            evidence_source = str(outcome["evidence_source"])
            retryable = bool(outcome["retryable"])
            attempts = max(1, int(outcome["attempts"]))
        else:
            evidence_source = "repair_target_output_difference"
            retryable = False
            attempts = 1
        failure = {
            "platform": platform,
            "identity": f"{platform}:id:{post_id}",
            "platform_post_id": post_id,
            "failure_scope": failure_scope,
            "detail": detail,
            "error_code": error_code,
            "retryable": retryable,
            "attempts": attempts,
            "source_index": materialization_failure.get("source_index"),
            "terminal_for_run": True,
            "evidence_source": evidence_source,
        }
        if outcome.get("evidence"):
            failure["evidence"] = outcome["evidence"]
        if not materialization_failure and error_code in ADMIT_UNAVAILABLE_CODES:
            unavailable_ids.append(post_id)
            continue
        failures.append(failure)
    evidence = {
        "available": True,
        "stopped": True,
        "stop_reason": "repair_targets_processed",
        "stop_detail": "specified_detail_targets",
        "candidate_count": len(targets),
        "batches": [],
        "successful_candidate_count": len(successful_ids),
        "skipped_candidate_count": len(failures),
        "skipped_candidate_failures": failures,
    }
    if unavailable_ids:
        evidence["unavailable_candidate_ids"] = unavailable_ids
        evidence["unavailable_candidate_count"] = len(unavailable_ids)
    return evidence



def repair_partial_child_execution_allowed(
    *,
    repair_mode: bool,
    child_execution_ok: bool,
    runtime_blocked: bool = False,
    validation: dict[str, Any],
    image_materialization: dict[str, Any],
    behavior_validation: dict[str, Any],
) -> bool:
    if runtime_blocked:
        return False
    if child_execution_ok:
        return True
    return bool(
        repair_mode
        and int(validation.get("valid_total_count") or 0) > 0
        and bool(image_materialization.get("complete"))
        and bool(behavior_validation.get("ok"))
    )



def repair_candidate_execution_completed(
    records: list[dict[str, Any]],
    platforms: list[str],
) -> bool:
    repair_records = [
        record
        for record in records
        if isinstance(record, dict) and record.get("platform") in platforms
    ]

    def clean_process(record: dict[str, Any]) -> bool:
        run = record.get("run")
        return bool(
            isinstance(run, dict)
            and run.get("returncode") == 0
            and not bool(run.get("timed_out"))
            and str(
                (record.get("failure_classification") or {}).get("failure_type")
                or ""
            )
            == "success"
        )

    return bool(
        len(repair_records) == len(platforms)
        and all(clean_process(record) for record in repair_records)
    )



def repair_runtime_stop_reason(
    records: list[dict[str, Any]],
    platforms: list[str],
    *,
    latest_runtime_blocker,
) -> str:
    """Keep the repair API while using current-attempt blocker semantics."""

    return str(latest_runtime_blocker(records, platforms).get("failure_type") or "")
