"""Deterministic topic-relevance classification for persisted posts."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping


_WHITESPACE_RE = re.compile(r"\s+")
PRIMARY_TOPIC_LITERAL = "青岛"
CONTENT_BODY_FIELDS = {
    "bilibili": ("content_text", "content"),
    "weibo": ("content_text", "content"),
    "xhs": ("desc",),
    "douyin": ("desc",),
    "zhihu": ("content_text", "content"),
}


def normalize_topic_text(value: Any) -> str:
    """Normalize text without tokenization or geographic inference."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _WHITESPACE_RE.sub(" ", text).strip()


def is_topic_relevant(
    *,
    title: Any,
    content_text: Any,
    keyword: Any,
) -> bool:
    """Classify the final ``web_posts.title`` and ``content_text`` fields."""

    searchable_fields = (
        normalize_topic_text(title),
        normalize_topic_text(content_text),
    )
    primary_topic = normalize_topic_text(PRIMARY_TOPIC_LITERAL)
    if any(primary_topic in field for field in searchable_fields):
        return True
    normalized_keyword = normalize_topic_text(keyword)
    return bool(
        normalized_keyword
        and any(normalized_keyword in field for field in searchable_fields)
    )


def effective_source_keyword(record: Mapping[str, Any], fallback_keyword: Any) -> str:
    """Choose the value that must also be persisted to ``web_posts.keyword``."""

    return str(record.get("source_keyword") or fallback_keyword or "")


def web_post_content_text(platform_key: str, record: Mapping[str, Any]) -> str:
    """Project the exact authoritative text persisted to ``web_posts.content_text``."""

    fields = CONTENT_BODY_FIELDS.get(platform_key, ("content_text", "content"))
    body = next(
        (str(record.get(key)).strip() for key in fields if record.get(key) not in (None, "")),
        "",
    )
    if platform_key in {"xhs", "zhihu"}:
        title = str(record.get("title") or "").strip()
        return "\n".join(dict.fromkeys(part for part in (title, body) if part))
    return body


def web_post_title(platform_key: str, record: Mapping[str, Any]) -> Any:
    """Project the exact value persisted to ``web_posts.title``."""

    title = record.get("title")
    if title not in (None, ""):
        return title
    if platform_key == "douyin":
        return record.get("desc") or ""
    if platform_key == "xhs":
        return str(record.get("desc") or "")[:255]
    return ""


def topic_relevant_for_web_post(
    platform_key: str,
    record: Mapping[str, Any],
    *,
    fallback_keyword: Any,
) -> bool:
    """Classify a crawler record from its final persisted title and body."""

    return is_topic_relevant(
        title=web_post_title(platform_key, record),
        content_text=web_post_content_text(platform_key, record),
        keyword=effective_source_keyword(record, fallback_keyword),
    )
