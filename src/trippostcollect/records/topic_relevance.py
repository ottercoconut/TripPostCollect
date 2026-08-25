"""Deterministic topic-relevance classification for persisted posts."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping


_WHITESPACE_RE = re.compile(r"\s+")
PRIMARY_TOPIC_LITERAL = "青岛"


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
    """Return whether title/body contains Qingdao or the complete search keyword."""

    searchable = normalize_topic_text(f"{title or ''}\n{content_text or ''}")
    if normalize_topic_text(PRIMARY_TOPIC_LITERAL) in searchable:
        return True
    normalized_keyword = normalize_topic_text(keyword)
    return bool(normalized_keyword and normalized_keyword in searchable)


def effective_source_keyword(record: Mapping[str, Any], fallback_keyword: Any) -> str:
    """Choose the value that must also be persisted to ``web_posts.keyword``."""

    return str(record.get("source_keyword") or fallback_keyword or "")


def topic_relevant_for_record(
    record: Mapping[str, Any],
    *,
    content_text: Any,
    fallback_keyword: Any,
    title_keys: tuple[str, ...] = ("title",),
) -> bool:
    """Classify a crawler record using only its title and authoritative body."""

    title = next((record.get(key) for key in title_keys if record.get(key)), "")
    return is_topic_relevant(
        title=title,
        content_text=content_text,
        keyword=effective_source_keyword(record, fallback_keyword),
    )
