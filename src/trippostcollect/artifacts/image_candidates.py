"""Explicit platform projections for authoritative post-body images."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit


XHS_STABLE_PATH_MARKERS = ("/notes_pre_post/", "/notes_post/", "/notes/")
ZHIMG_TRANSFORM_SUFFIX_RE = re.compile(
    r"_(?:[1-9]\d{1,4}w|b|r|qhd|hd|xs|s|m|l|xl|xxl|original|watermark)"
    r"\.(?:avif|gif|jpe?g|png|webp)$",
    re.IGNORECASE,
)
RASTER_SUFFIX_RE = re.compile(r"\.(?:avif|gif|jpe?g|png|webp)$", re.IGNORECASE)
BILIBILI_TRANSFORM_RE = re.compile(r"@.*$")


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
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
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


def _zhihu_asset_path(source_url: str) -> str | None:
    parsed = urlsplit(source_url)
    hostname = (parsed.hostname or "").lower()
    if hostname != "zhimg.com" and not hostname.endswith(".zhimg.com"):
        return None
    logical_path = ZHIMG_TRANSFORM_SUFFIX_RE.sub("", unquote(parsed.path))
    return RASTER_SUFFIX_RE.sub("", logical_path)


def _dedupe_identity(platform_key: str, source_url: str) -> str:
    if platform_key == "xhs":
        return _xhs_path_identity(source_url)
    if platform_key == "zhihu":
        logical_path = _zhihu_asset_path(source_url)
        if logical_path is not None:
            return f"zhihu:path:{logical_path}"
    return source_url


def _fallback_key(platform_key: str, identity: str) -> str:
    digest = sha256(identity.encode("utf-8")).hexdigest()
    return f"{platform_key}:urlsha256:{digest}"


def _asset_id(*items: Any, keys: tuple[str, ...]) -> str | None:
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return str(value).strip()
    return None


def source_asset_key_for_image(
    platform_key: str,
    source_url: str,
    *,
    source_item: Any = None,
    asset_metadata: Any = None,
) -> str:
    """Return the stable, non-random platform identity for one source image."""

    normalized = normalize_image_url(source_url)
    if not normalized:
        raise ValueError("source image URL must be HTTP(S)")
    parsed = urlsplit(normalized)
    path = unquote(parsed.path)

    if platform_key == "bilibili":
        logical_path = BILIBILI_TRANSFORM_RE.sub("", path)
        if "/bfs/" in logical_path:
            bfs_asset = logical_path.split("/bfs/", 1)[1].lstrip("/")
            bfs_asset = RASTER_SUFFIX_RE.sub("", bfs_asset)
            return f"bilibili:bfs:{bfs_asset}"
        return _fallback_key(platform_key, f"{parsed.hostname.lower()}{logical_path}")

    if platform_key == "weibo":
        pid = _asset_id(source_item, asset_metadata, keys=("pid", "picture_id"))
        if pid:
            return f"weibo:pid:{pid}"
        return _fallback_key(platform_key, f"{parsed.hostname.lower()}{path}")

    if platform_key == "xhs":
        return f"xhs:path:{_xhs_path_identity(normalized)}"

    if platform_key == "douyin":
        uri = _asset_id(source_item, asset_metadata, keys=("uri", "image_uri"))
        if uri:
            return f"douyin:uri:{uri}"
        return _fallback_key(platform_key, path)

    if platform_key == "zhihu":
        logical_path = _zhihu_asset_path(normalized)
        if logical_path is not None:
            return _fallback_key(platform_key, logical_path)
        return _fallback_key(platform_key, f"{parsed.hostname.lower()}{path}")

    raise ValueError(f"unsupported image candidate platform: {platform_key}")


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


def _asset_metadata_for(
    record: dict[str, Any],
    source_position: int,
    source_url: str,
) -> dict[str, Any] | None:
    assets = _sequence(record.get("image_assets"))
    if source_position < len(assets) and isinstance(assets[source_position], dict):
        return assets[source_position]
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_url = _mapping_url(asset, ("url", "source_url", "url_default", "url_pre"))
        if asset_url == source_url:
            return asset
    return None


def _is_zhihu_formula(source_url: str) -> bool:
    path = urlsplit(source_url).path.lower().rstrip("/")
    return path.endswith("/equation") or "/equation/" in f"{path}/"


def content_image_candidates(platform_key: str, record: dict[str, Any]) -> list[ImageCandidate]:
    """Project only authoritative post-body images for one supported platform."""

    source_key, raw_values = _authoritative_values(platform_key, record)
    platform_post_id = _platform_post_id(platform_key, record)
    seen: set[str] = set()
    source_items: list[tuple[str, Any, dict[str, Any] | None]] = []
    for source_position, item in enumerate(raw_values):
        source_url = _item_url(platform_key, item)
        if not source_url:
            continue
        if platform_key == "zhihu" and _is_zhihu_formula(source_url):
            continue
        identity = _dedupe_identity(platform_key, source_url)
        if identity in seen:
            continue
        seen.add(identity)
        source_items.append(
            (
                source_url,
                item,
                _asset_metadata_for(record, source_position, source_url),
            )
        )

    return [
        ImageCandidate(
            platform_key=platform_key,
            platform_post_id=platform_post_id,
            image_role="content",
            source_index=index,
            source_url=source_url,
            source_key=source_key,
            source_asset_key=source_asset_key_for_image(
                platform_key,
                source_url,
                source_item=source_item,
                asset_metadata=asset_metadata,
            ),
        )
        for index, (source_url, source_item, asset_metadata) in enumerate(source_items)
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
