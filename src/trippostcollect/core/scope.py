"""Project-wide geographic scope validation."""

from __future__ import annotations


QINGDAO_TOPIC_MARKERS = ("青岛", "崂山")


def is_qingdao_topic_keyword(value: object) -> bool:
    """Return whether a declared query keyword is inside the Qingdao topic scope."""
    keyword = str(value or "").strip()
    return keyword.startswith(QINGDAO_TOPIC_MARKERS)


def require_qingdao_topic_keyword(value: object, *, field_name: str = "keyword") -> str:
    """Return a normalized keyword or reject collection outside the project scope."""
    keyword = str(value or "").strip()
    if not is_qingdao_topic_keyword(keyword):
        markers = "、".join(QINGDAO_TOPIC_MARKERS)
        raise ValueError(f"{field_name} must start with one of: {markers}")
    return keyword
