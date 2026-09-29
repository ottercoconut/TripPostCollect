"""正式记录的纯字段判定与投影。

图片投影函数与原 ImagePersistenceError 类通过仅限关键字参数注入；各站迁移后由
application 装配层注入，T11 collection 迁移时收口。MaterializedImage 仅用于类型检查。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from trippostcollect.application.contracts import PLATFORMS
from trippostcollect.records.sanitization import sanitize_author_avatar_data
from trippostcollect.records.topic_relevance import (
    CONTENT_BODY_FIELDS, effective_source_keyword, is_topic_relevant,
    web_post_content_text, web_post_title,
)

if TYPE_CHECKING:
    from trippostcollect.artifacts.image_materialization import MaterializedImage


BILIBILI_TRUSTED_DETAIL_SOURCES = frozenset({"article_view_api"})


TRUSTED_CONTENT_DETAIL_SOURCES = {
    "bilibili": BILIBILI_TRUSTED_DETAIL_SOURCES,
    "weibo": frozenset({"search_mblog_complete", "mobile_detail"}),
    "xhs": frozenset({"note_detail"}),
    "douyin": frozenset({"aweme_detail"}),
    "zhihu": frozenset({"search_content", "answer_detail", "article_detail"}),
}


FOLLOWERS_REQUIRED_PLATFORMS = frozenset(PLATFORMS)


REQUIRED_FOLLOWER_SOURCES = {
    "bilibili": frozenset({"relation_stat"}),
    "weibo": frozenset({"search_author"}),
    "xhs": frozenset({"creator_profile"}),
    "douyin": frozenset({"creator_profile"}),
    "zhihu": frozenset({"search_author"}),
}


PLATFORM_REQUIRED_METRICS: dict[str, tuple[tuple[str, ...], ...]] = {
    "bilibili": (("liked_count",), ("comment_count", "comments_count"), ("view_count", "video_play_count")),
    "weibo": (("liked_count",), ("comments_count", "comment_count"), ("shared_count", "reposts_count")),
    "xhs": (("liked_count",), ("collected_count",), ("comment_count",), ("share_count",)),
    "douyin": (("liked_count",), ("collected_count",), ("comment_count",), ("share_count",)),
    "zhihu": (("voteup_count", "liked_count"), ("comment_count", "comments_count")),
}


VIDEO_URL_RE = re.compile(r"(?i)(?:/(?:video|share/video)/\d+|\.(?:mp4|m4v|mov|webm|flv|m3u8|mpd)(?:[?#]|$))")


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
    "publish_time",
    "publish_date",
    "pub_time",
    "pubdate",
    "created_at",
    "created_time",
    "create_date_time",
    "create_time",
    "ctime",
    "timestamp",
    "time",
)


NESTED_PUBLISHED_AT_KEYS = tuple(key for key in PUBLISHED_AT_KEYS if key not in {"time", "timestamp"})


def json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "无"}:
        return None
    unit = 1
    if text.endswith("万"):
        unit = 10_000
        text = text[:-1]
    elif text.endswith("亿"):
        unit = 100_000_000
        text = text[:-1]
    try:
        return int(float(text) * unit)
    except ValueError:
        return None


def first_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
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
    if depth > 4:
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


def published_at_for_record(record: dict[str, Any]) -> str | None:
    for key in PUBLISHED_AT_KEYS:
        value = record.get(key)
        parsed = datetime_value_to_iso(value)
        if parsed:
            return parsed
    for value in iter_nested_values_for_keys(record, NESTED_PUBLISHED_AT_KEYS):
        parsed = datetime_value_to_iso(value)
        if parsed:
            return parsed
    return None


def merge_repair_fallback_metadata(
    platform_key: str,
    record: dict[str, Any],
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    if not metadata:
        return record
    merged = dict(record)

    if not published_at_for_record(merged):
        for key in ("published_at", "created_time"):
            value = metadata.get(key)
            if value not in (None, ""):
                merged["published_at" if key == "published_at" else key] = value
                if published_at_for_record(merged):
                    break

    fallback_observed = metadata.get("followers_observed") is True
    current_observed = merged.get("followers_observed") is True
    if fallback_observed and not current_observed:
        for key in (
            "followers_count",
            "author_followers_count",
            "followers_observed",
            "author_followers_source",
        ):
            if metadata.get(key) not in (None, ""):
                merged[key] = metadata[key]
    else:
        for key in (
            "followers_count",
            "author_followers_count",
            "author_followers_source",
        ):
            if merged.get(key) in (None, "", 0) and metadata.get(key) not in (None, ""):
                merged[key] = metadata[key]

    for key in (
        "creator_hash",
        "creator_url_token",
        "user_nickname",
        "author_profile_url",
        "author_description",
        "author_display_name",
        "author_platform_id",
    ):
        if merged.get(key) in (None, "") and metadata.get(key) not in (None, ""):
            merged[key] = metadata[key]

    if platform_key == "xhs":
        metric_fallbacks: dict[str, dict[str, Any]] = {}
        for key in ("liked_count", "collected_count", "comment_count", "share_count"):
            if first_value(merged, key) is None and metadata.get(key) not in (None, ""):
                merged[key] = metadata[key]
                metric_fallbacks[key] = {
                    "source": "existing_web_posts_metric",
                    "value": metadata[key],
                }
        if metric_fallbacks:
            prior_evidence = merged.get("repair_fallback_evidence")
            evidence = dict(prior_evidence) if isinstance(prior_evidence, dict) else {}
            evidence["metrics"] = metric_fallbacks
            merged["repair_fallback_evidence"] = evidence

    return merged


def is_video_record(platform_key: str, record: dict[str, Any]) -> bool:
    if platform_key == "bilibili" and first_value(record, "video_id", "video_url", "bvid", "aid"):
        return True
    if platform_key == "zhihu":
        content_type = str(first_value(record, "content_type", "type") or "").strip().lower()
        content_url = str(first_value(record, "content_url", "url", "share_url") or "")
        if "zvideo" in content_type or content_type == "video" or "/zvideo/" in content_url:
            return True
    if platform_key == "douyin":
        note_images = first_value(record, "note_download_url", "image_list", "images")
        aweme_type = str(first_value(record, "aweme_type", "video_type", "media_type", "type") or "").strip().lower()
        if note_images or aweme_type in {"68", "note", "image", "images", "image_text", "图文"}:
            return False
        if first_value(record, "video_download_url"):
            return True
    video_type = str(first_value(record, "video_type", "media_type", "type") or "").strip().lower()
    if video_type and video_type not in {"note", "image", "images", "image_text", "图文"}:
        if "video" in video_type or "视频" in video_type:
            return True
    for key, value in record.items():
        key_lower = str(key).lower()
        if "video" not in key_lower and key_lower not in {"bvid", "aid"}:
            continue
        if value not in (None, "") and VIDEO_URL_RE.search(str(value)):
            return True
    for value in (first_value(record, "url", "share_url", "note_url", "aweme_url", "video_url"),):
        if value and VIDEO_URL_RE.search(str(value)):
            return True
    return False


def platform_from_path(path: Path) -> str:
    parts = [part.lower() for part in path.parts]
    if "bili" in parts:
        return "bilibili"
    if "zhihu" in parts:
        return "zhihu"
    if "xhs" in parts:
        return "xhs"
    if "weibo" in parts:
        return "weibo"
    if "douyin" in parts:
        return "douyin"
    for key in PLATFORMS:
        if key in parts:
            return key
    return "unknown"


def post_id_for_record(platform_key: str, record: dict[str, Any]) -> str | None:
    value = first_value(record, "note_id", "aweme_id", "content_id", "video_id", "bvid", "aid", "id")
    return str(value) if value not in (None, "") else None


def canonical_url_for_record(platform_key: str, record: dict[str, Any]) -> str | None:
    value = first_value(record, "note_url", "aweme_url", "content_url", "video_url", "url", "share_url")
    if value:
        return str(value)
    post_id = post_id_for_record(platform_key, record)
    if not post_id:
        return None
    if platform_key == "weibo":
        return f"https://m.weibo.cn/detail/{post_id}"
    if platform_key == "douyin":
        return f"https://www.douyin.com/video/{post_id}"
    if platform_key == "bilibili":
        content_type = str(first_value(record, "content_type", "type") or "").strip().lower()
        if content_type in {"article", "read", "opus", "dynamic"}:
            return f"https://www.bilibili.com/read/cv{post_id}/"
        return f"https://www.bilibili.com/video/av{post_id}"
    if platform_key == "xhs":
        return f"https://www.xiaohongshu.com/explore/{post_id}"
    if platform_key == "zhihu":
        content_type = str(first_value(record, "content_type", "type") or "").strip().lower()
        question_id = first_value(record, "question_id")
        if content_type == "article":
            return f"https://zhuanlan.zhihu.com/p/{post_id}"
        if content_type == "answer" and question_id:
            return f"https://www.zhihu.com/question/{question_id}/answer/{post_id}"
    return None


def content_body_for_record(platform_key: str, record: dict[str, Any]) -> str:
    fields = CONTENT_BODY_FIELDS.get(platform_key, ("content_text", "content"))
    return str(first_value(record, *fields) or "").strip()


def content_text_for_record(platform_key: str, record: dict[str, Any]) -> str:
    return web_post_content_text(platform_key, record)


def inject_materialized_images(
    image_items: list[dict[str, Any]],
    materialized_images: list[MaterializedImage],
    *,
    ImagePersistenceError: type[Exception],
) -> list[dict[str, Any]]:

    def source_identity(
        *,
        platform_key: str,
        platform_post_id: str,
        source_index: int,
        source_key: str,
        source_asset_key: str,
        source_url: str,
    ) -> tuple[str, str, int, str, str, str]:
        return (
            platform_key,
            platform_post_id,
            source_index,
            source_key,
            source_asset_key,
            source_url,
        )

    materialized: dict[tuple[str, str, int, str, str, str], MaterializedImage] = {}
    duplicate_identities: set[tuple[str, str, int, str, str, str]] = set()
    for local in materialized_images:
        if local.manifest_source_index is None:
            raise ImagePersistenceError("materialized image lacks manifest source index")
        identity = source_identity(
            platform_key=local.platform_key,
            platform_post_id=local.platform_post_id,
            source_index=local.manifest_source_index,
            source_key=local.source_key,
            source_asset_key=local.source_asset_key,
            source_url=local.source_url,
        )
        if identity in materialized:
            raise ImagePersistenceError("duplicate materialized image identity")
        materialized[identity] = local
        for duplicate in local.sha256_duplicate_sources:
            duplicate_identity = source_identity(
                platform_key=local.platform_key,
                platform_post_id=local.platform_post_id,
                source_index=duplicate.source_index,
                source_key=duplicate.source_key,
                source_asset_key=duplicate.source_asset_key,
                source_url=duplicate.source_url,
            )
            if duplicate_identity in materialized or duplicate_identity in duplicate_identities:
                raise ImagePersistenceError("duplicate SHA-256 source identity")
            duplicate_identities.add(duplicate_identity)
    content_items = [item for item in image_items if item.get("role") == "content"]
    if len(materialized) + len(duplicate_identities) != len(content_items):
        raise ImagePersistenceError(
            "materialized and SHA-256 duplicate image count does not match source candidates"
        )

    result: list[dict[str, Any]] = []
    for raw_item in image_items:
        item = dict(raw_item)
        if item.get("role") != "content":
            result.append(item)
            continue
        identity = source_identity(
            platform_key=str(item.get("platform_key") or ""),
            platform_post_id=str(item.get("platform_post_id") or ""),
            source_index=int(item["source_index"]),
            source_key=str(item.get("source_key") or ""),
            source_asset_key=str(item.get("source_asset_key") or ""),
            source_url=str(item.get("url") or ""),
        )
        local = materialized.get(identity)
        if local is None:
            if identity in duplicate_identities:
                continue
            raise ImagePersistenceError(f"materialized image identity mismatch: {identity}")
        local_file = {
            "source": "formal_image_materialization_v1",
            "source_url": local.source_url,
            "size_bytes": local.size_bytes,
            "manifest_source_index": local.manifest_source_index,
        }
        if local.manifest_path:
            local_file["manifest_path"] = local.manifest_path
        if local.manifest_line is not None:
            local_file["manifest_line"] = local.manifest_line
        if local.sha256_duplicate_sources:
            local_file["sha256_duplicate_sources"] = [
                {
                    "source_index": duplicate.source_index,
                    "source_key": duplicate.source_key,
                    "source_asset_key": duplicate.source_asset_key,
                    "source_url": duplicate.source_url,
                    "manifest_path": duplicate.manifest_path,
                    "manifest_line": duplicate.manifest_line,
                }
                for duplicate in local.sha256_duplicate_sources
            ]
        item.update(
            {
                "source_index": local.source_index,
                "local_path": local.local_path,
                "width": local.width,
                "height": local.height,
                "mime_type": local.mime_type,
                "sha256": local.sha256,
                "local_file": local_file,
            }
        )
        result.append(item)
    retained_content = [item for item in result if item.get("role") == "content"]
    if len(retained_content) != len(materialized_images):
        raise ImagePersistenceError("retained materialized image count mismatch")
    if [int(item["source_index"]) for item in retained_content] != list(
        range(len(retained_content))
    ):
        raise ImagePersistenceError("retained materialized image indices are not continuous")
    return result


def row_for_record(
    platform_key: str,
    record: dict[str, Any],
    *,
    artifact_dir: str,
    captured_at: str,
    keyword: str,
    materialized_images: list[MaterializedImage] | None = None,
    image_items_for_record: Callable[[str, dict[str, Any]], list[dict[str, Any]]],
    ImagePersistenceError: type[Exception],
) -> dict[str, Any]:
    sanitized_record = sanitize_author_avatar_data(record).value
    if not isinstance(sanitized_record, dict):
        raise ValueError("sanitized record must remain an object")
    record = sanitized_record
    content_text = content_text_for_record(platform_key, record)
    canonical_url = canonical_url_for_record(platform_key, record)
    image_items = image_items_for_record(platform_key, record)
    if materialized_images is not None:
        image_items = inject_materialized_images(
            image_items, materialized_images, ImagePersistenceError=ImagePersistenceError,
        )
    keyword_value = effective_source_keyword(record, keyword)
    title = web_post_title(platform_key, record)
    metrics = {
        "liked_count": parse_int(first_value(record, "liked_count", "voteup_count")),
        "favorites_count": parse_int(first_value(record, "collected_count", "video_favorite_count")),
        "comments_count": parse_int(first_value(record, "comment_count", "comments_count", "video_comment")),
        "shares_count": parse_int(first_value(record, "share_count", "shared_count", "video_share_count")),
        "reposts_count": parse_int(first_value(record, "reposts_count", "repost_count")),
        "views_count": parse_int(first_value(record, "video_play_count", "view_count", "play_count")),
    }
    author = {
        "nickname": first_value(record, "nickname", "user_nickname", "user_name", "author_name"),
        "creator_hash": first_value(record, "user_id", "creator_id", "creator_hash", "author_id"),
        "followers_count": parse_int(
            first_value(
                record,
                "author_followers_count",
                "followers_count",
                "follower_count",
                "fans_count",
                "fans",
                "followers",
            )
        ),
        "following_count": parse_int(first_value(record, "author_following_count", "following_count", "follow_count", "follows")),
        "posts_count": parse_int(first_value(record, "author_posts_count", "aweme_count", "posts_count", "note_count", "notes_count", "video_count")),
        "liked_count": parse_int(first_value(record, "author_liked_count", "total_favorited", "favorited_count")),
        "followers_observed": record.get("followers_observed") is True,
        "followers_source": first_value(record, "author_followers_source", "followers_source"),
    }
    return {
        "platform_key": platform_key,
        "platform_post_id": post_id_for_record(platform_key, record),
        "source_type": "mediacrawler_search",
        "source_url": canonical_url or "",
        "canonical_url": canonical_url,
        "title": title,
        "author_display_name": author["nickname"],
        "author_platform_id": author["creator_hash"],
        "author_profile_url": first_value(record, "author_profile_url", "user_link", "profile_url", "user_url"),
        "author_description": first_value(record, "author_desc", "user_desc"),
        "author_followers_count": author["followers_count"],
        "author_following_count": author["following_count"],
        "author_posts_count": author["posts_count"],
        "author_platform_level": first_value(record, "level", "user_level"),
        "author_verified": None,
        "author_verified_text": first_value(record, "verified_text", "verify_info"),
        "published_at": published_at_for_record(record),
        "captured_at": captured_at,
        "keyword": keyword_value,
        "topic_relevant": int(
            is_topic_relevant(
                title=title,
                content_text=content_text,
                keyword=keyword_value,
            )
        ),
        "content_text": content_text,
        "content_length": len(content_text),
        "post_likes_count": metrics["liked_count"],
        "post_favorites_count": metrics["favorites_count"],
        "post_comments_count": metrics["comments_count"],
        "post_shares_count": metrics["shares_count"],
        "post_reposts_count": metrics["reposts_count"],
        "post_views_count": metrics["views_count"],
        "post_images_count": len([item for item in image_items if item["role"] == "content"]),
        "metrics_json": json_dump(metrics),
        "author_json": json_dump(author),
        "raw_sample_json": json_dump(record),
        "artifact_dir": artifact_dir,
        "capture_method": "import",
        "status": "captured" if content_text else "partial",
        "_image_items": image_items,
    }


def formal_record_identity(platform_key: str, record: dict[str, Any]) -> str:
    post_id = post_id_for_record(platform_key, record)
    if post_id:
        return f"{platform_key}:id:{post_id}"
    canonical_url = canonical_url_for_record(platform_key, record)
    return f"{platform_key}:url:{canonical_url}" if canonical_url else ""


def formal_database_identities(platform_key: str, record: dict[str, Any]) -> set[str]:
    identities: set[str] = set()
    post_id = post_id_for_record(platform_key, record)
    canonical_url = canonical_url_for_record(platform_key, record)
    if post_id:
        identities.add(f"{platform_key}:id:{post_id}")
    if canonical_url:
        identities.add(f"{platform_key}:url:{canonical_url}")
    return identities


def validate_formal_record(
    platform_key: str,
    record: dict[str, Any],
    seen: set[str],
    *,
    allow_xhs_title_image_only: bool = False,
    content_image_candidates: Callable[[str, dict[str, Any]], list[Any]],
) -> dict[str, Any]:
    identity = formal_record_identity(platform_key, record)
    reasons: list[str] = []
    if not identity:
        reasons.append("missing_identity")
    elif identity in seen:
        reasons.append("duplicate_identity")
    if is_video_record(platform_key, record):
        reasons.append("video_record")
    detail_status = str(record.get("content_detail_status") or "")
    detail_source = str(record.get("content_detail_source") or "")
    xhs_title_image_only = bool(
        allow_xhs_title_image_only
        and platform_key == "xhs"
        and str(first_value(record, "title") or "").strip()
        and detail_status == "detail_observed"
        and detail_source == "note_detail"
        and content_image_candidates(platform_key, record)
    )
    if not content_body_for_record(platform_key, record) and not xhs_title_image_only:
        reasons.append("missing_content")
    if detail_status != "detail_observed":
        reasons.append("content_detail_unobserved")
    if detail_source not in TRUSTED_CONTENT_DETAIL_SOURCES.get(platform_key, frozenset()):
        reasons.append("untrusted_content_detail_source")
    if platform_key == "bilibili":
        image_detail_status = str(record.get("content_images_detail_status") or "")
        if image_detail_status != "detail_observed":
            reasons.append("content_images_detail_unobserved")
    if not published_at_for_record(record):
        reasons.append("missing_published_at")
    if not first_value(record, "user_id", "creator_id", "creator_hash", "author_id", "mid"):
        reasons.append("missing_author_id")
    if not first_value(record, "nickname", "user_nickname", "user_name", "author_name", "author"):
        reasons.append("missing_author_name")
    content_images = content_image_candidates(platform_key, record)
    if not content_images:
        detail_status = str(record.get("content_detail_status") or "")
        if (
            platform_key == "zhihu"
            and str(record.get("content_type") or "") in {"answer", "article"}
            and detail_status != "detail_observed"
        ):
            reasons.append("content_detail_unobserved")
        else:
            reasons.append("missing_content_image")

    followers_count = parse_int(
        first_value(
            record,
            "author_followers_count",
            "followers_count",
            "follower_count",
            "fans_count",
            "fans",
            "followers",
        )
    )
    followers_observed = record.get("followers_observed") is True
    followers_source = str(first_value(record, "author_followers_source", "followers_source") or "")
    if platform_key in FOLLOWERS_REQUIRED_PLATFORMS:
        if followers_count is None:
            reasons.append("missing_followers_count")
        if not followers_observed:
            reasons.append("followers_not_observed")
        if not followers_source or followers_source == "missing":
            reasons.append("missing_followers_source")
        elif followers_source not in REQUIRED_FOLLOWER_SOURCES.get(platform_key, frozenset()):
            reasons.append("untrusted_followers_source")

    missing_metric_groups = [
        "/".join(keys)
        for keys in PLATFORM_REQUIRED_METRICS.get(platform_key, ())
        if first_value(record, *keys) is None
    ]
    if missing_metric_groups:
        reasons.append("missing_metrics:" + ",".join(missing_metric_groups))
    return {
        "valid": not reasons,
        "identity": identity,
        "reasons": reasons,
        "followers_count": followers_count,
        "followers_observed": followers_observed,
        "followers_source": followers_source,
        "content_image_count": len(content_images),
    }
