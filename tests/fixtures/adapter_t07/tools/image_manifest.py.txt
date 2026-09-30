"""图片 staging 旧导入出口；站点稳定键留待各站 parser 迁移。"""

from hashlib import sha256
import re
from urllib.parse import unquote, urlsplit

from trippostcollect.runtime.helpers import normalize_image_url as normalize_image_url
from trippostcollect.artifacts.image_staging import (
    ImageStagingError as ImageStagingError,
    ImageAsset as ImageAsset,
    InspectedImage as InspectedImage,
    looks_like_supported_raster as looks_like_supported_raster,
    inspect_image_bytes as inspect_image_bytes,
    _safe_component as _safe_component,
    _fsync_directory as _fsync_directory,
    _manifest_payload as _manifest_payload,
    upsert_manifest_rows_atomic as upsert_manifest_rows_atomic,
    failed_manifest_row as failed_manifest_row,
    stage_post_images as stage_post_images,
    MAX_IMAGE_BYTES as MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS as MAX_IMAGE_PIXELS,
    FORMAT_METADATA as FORMAT_METADATA,
)

XHS_STABLE_PATH_MARKERS = ("/notes_pre_post/", "/notes_post/", "/notes/")

ZHIMG_TRANSFORM_SUFFIX_RE = re.compile(
    r"_(?:[1-9]\d{1,4}w|b|r|qhd|hd|xs|s|m|l|xl|xxl|original|watermark)"
    r"\.(?:avif|gif|jpe?g|png|webp)$",
    re.IGNORECASE,
)

RASTER_SUFFIX_RE = re.compile(r"\.(?:avif|gif|jpe?g|png|webp)$", re.IGNORECASE)


def weibo_source_asset_key(pid: str | None, source_url: str) -> str:
    if pid not in (None, ""):
        return f"weibo:pid:{str(pid).strip()}"
    normalized = normalize_image_url(source_url)
    parsed = urlsplit(normalized)
    digest = sha256(f"{parsed.hostname.lower()}{parsed.path}".encode("utf-8")).hexdigest()
    return f"weibo:urlsha256:{digest}"


def xhs_source_asset_key(source_url: str) -> str:
    normalized = normalize_image_url(source_url)
    parsed = urlsplit(normalized)
    identity = f"{parsed.netloc.lower()}{parsed.path}"
    for marker in XHS_STABLE_PATH_MARKERS:
        if marker in parsed.path:
            identity = f"{marker}{parsed.path.split(marker, 1)[1]}"
            break
    return f"xhs:path:{identity}"


def zhihu_source_asset_key(source_url: str) -> str:
    normalized = normalize_image_url(source_url)
    parsed = urlsplit(normalized)
    hostname = parsed.hostname.lower()
    path = unquote(parsed.path)
    is_zhimg = hostname == "zhimg.com" or hostname.endswith(".zhimg.com")
    if is_zhimg:
        logical_path = ZHIMG_TRANSFORM_SUFFIX_RE.sub("", path)
        identity = RASTER_SUFFIX_RE.sub("", logical_path)
    else:
        identity = f"{hostname}{path}"
    return f"zhihu:urlsha256:{sha256(identity.encode('utf-8')).hexdigest()}"


def douyin_source_asset_key(uri: str | None, source_url: str) -> str:
    if uri not in (None, ""):
        return f"douyin:uri:{str(uri).strip()}"
    normalized = normalize_image_url(source_url)
    digest = sha256(urlsplit(normalized).path.encode("utf-8")).hexdigest()
    return f"douyin:urlsha256:{digest}"
