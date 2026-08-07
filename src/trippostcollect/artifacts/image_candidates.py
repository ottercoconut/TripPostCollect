"""Explicit platform projections for authoritative post-body images."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit


XHS_STABLE_PATH_MARKERS = ("/notes_pre_post/", "/notes_post/", "/notes/")


@dataclass(frozen=True, slots=True)
class ImageCandidate:
    """One authoritative body image in source order."""

    platform_key: str
    platform_post_id: str
    image_role: str
    source_index: int
    source_url: str
    source_key: str
    source_asset_key: str

    def as_image_item(self) -> dict[str, Any]:
        """Return the shape persisted in ``web_post_images.raw_image_json``."""

        payload = asdict(self)
        payload["url"] = payload.pop("source_url")
        payload["role"] = payload.pop("image_role")
        return payload


def normalize_image_url(value: Any) -> str | None:
    """Normalize a single HTTP(S) image URL without discarding its query."""

    if value in (None, ""):
        return None
    text = str(value).strip().rstrip("\t\r\n ).];,，")
    if text.startswith("//"):
        text = f"https:{text}"
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    netloc = parsed.netloc.lower()
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, ""))


def _platform_post_id(platform_key: str, record: dict[str, Any]) -> str:
    keys = {
        "bilibili": ("content_id", "id"),
        "weibo": ("note_id", "id"),
        "xhs": ("note_id", "id"),
        "douyin": ("aweme_id", "id"),
        "zhihu": ("content_id", "id"),
    }[platform_key]
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    if text.startswith(("[", "{")):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list):
            return decoded
        if isinstance(decoded, dict):
            return [decoded]
    return [part.strip() for part in text.split(",") if part.strip()]


def _mapping_url(item: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str):
            normalized = normalize_image_url(value)
            if normalized:
                return normalized
        if isinstance(value, dict):
            for nested_key in ("url", "url_default", "url_pre"):
                normalized = normalize_image_url(value.get(nested_key))
                if normalized:
                    return normalized
    return None


def _item_url(platform_key: str, item: Any) -> str | None:
    if not isinstance(item, dict):
        return normalize_image_url(item)
    if platform_key == "xhs":
        return _mapping_url(item, ("url_default", "url", "url_pre"))
    if platform_key == "weibo":
        return _mapping_url(item, ("url", "large", "largest", "geo"))
    return _mapping_url(item, ("url", "image_url"))


def _xhs_path_identity(source_url: str) -> str:
    path = urlsplit(source_url).path
    for marker in XHS_STABLE_PATH_MARKERS:
        if marker in path:
            return f"{marker}{path.split(marker, 1)[1]}"
    parsed = urlsplit(source_url)
    return f"{parsed.netloc.lower()}{parsed.path}"


def _dedupe_identity(platform_key: str, source_url: str) -> str:
    if platform_key == "xhs":
        return _xhs_path_identity(source_url)
    return source_url


def _provisional_asset_key(platform_key: str, source_url: str) -> str:
    """Provide a deterministic pre-manifest key; I-02 adds platform asset IDs."""

    parsed = urlsplit(source_url)
    identity = _xhs_path_identity(source_url) if platform_key == "xhs" else f"{parsed.netloc}{parsed.path}"
    return f"{platform_key}:urlsha256:{sha256(identity.encode('utf-8')).hexdigest()}"


def _authoritative_values(platform_key: str, record: dict[str, Any]) -> tuple[str, list[Any]]:
    if platform_key == "bilibili":
        if str(record.get("content_images_detail_status") or "") != "detail_observed":
            return "image_urls", []
        return "image_urls", _sequence(record.get("image_urls"))
    if platform_key == "weibo":
        provenance = str(record.get("image_list_source") or "")
        if provenance and provenance != "mblog.pics":
            return "image_list", []
        return "image_list", _sequence(record.get("image_list"))
    if platform_key == "xhs":
        return "image_list", _sequence(record.get("image_list"))
    if platform_key == "douyin":
        return "note_download_url", _sequence(record.get("note_download_url"))
    if platform_key == "zhihu":
        return "image_list", _sequence(record.get("image_list"))
    raise ValueError(f"unsupported image candidate platform: {platform_key}")


def _is_zhihu_formula(source_url: str) -> bool:
    path = urlsplit(source_url).path.lower().rstrip("/")
    return path.endswith("/equation") or "/equation/" in f"{path}/"


def content_image_candidates(platform_key: str, record: dict[str, Any]) -> list[ImageCandidate]:
    """Project only authoritative post-body images for one supported platform."""

    source_key, raw_values = _authoritative_values(platform_key, record)
    platform_post_id = _platform_post_id(platform_key, record)
    seen: set[str] = set()
    source_urls: list[str] = []
    for item in raw_values:
        source_url = _item_url(platform_key, item)
        if not source_url:
            continue
        if platform_key == "zhihu" and _is_zhihu_formula(source_url):
            continue
        identity = _dedupe_identity(platform_key, source_url)
        if identity in seen:
            continue
        seen.add(identity)
        source_urls.append(source_url)

    return [
        ImageCandidate(
            platform_key=platform_key,
            platform_post_id=platform_post_id,
            image_role="content",
            source_index=index,
            source_url=source_url,
            source_key=source_key,
            source_asset_key=_provisional_asset_key(platform_key, source_url),
        )
        for index, source_url in enumerate(source_urls)
    ]


def author_avatar_reference(record: dict[str, Any]) -> dict[str, Any] | None:
    """Keep an optional remote avatar relationship outside body-image candidates."""

    for source_key in ("avatar_url", "author_avatar", "author_avatar_url", "avatar", "user_avatar"):
        source_url = normalize_image_url(record.get(source_key))
        if source_url:
            return {
                "url": source_url,
                "role": "author_avatar",
                "source_key": source_key,
                "source_index": 0,
            }
    return None


def image_items_for_record(platform_key: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    """Build explicit persistence items while keeping avatars URL-only."""

    items = [candidate.as_image_item() for candidate in content_image_candidates(platform_key, record)]
    avatar = author_avatar_reference(record)
    if avatar:
        items.append(avatar)
    return items
