"""正文图片 URL 的纯规范化。"""

from typing import Any
from urllib.parse import urlsplit, urlunsplit


def normalize_image_url(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().rstrip("\t\r\n ).];,，")
    if text.startswith("//"):
        text = f"https:{text}"
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    netloc = parsed.netloc.lower()
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, ""))

