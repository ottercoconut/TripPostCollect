"""Atomic, format-verified content-image staging for TripPostCollect runs."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import shutil
from typing import Iterable, Sequence

from PIL import Image, UnidentifiedImageError

from trippostcollect.application.contracts import ImageStagingError as ImageStagingError
from trippostcollect.runtime.helpers import normalize_image_url


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 100_000_000
FORMAT_METADATA = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
    "GIF": ("gif", "image/gif"),
    "AVIF": ("avif", "image/avif"),
}


@dataclass(frozen=True, slots=True)
class ImageAsset:
    source_index: int
    source_asset_key: str
    source_url: str
    content: bytes
    attempts: int = 1
    http_status: int = 200


@dataclass(frozen=True, slots=True)
class InspectedImage:
    extension: str
    mime_type: str
    width: int
    height: int
    size_bytes: int
    sha256: str


def looks_like_supported_raster(content: bytes) -> bool:
    return bool(
        content.startswith(b"\xff\xd8\xff")
        or content.startswith(b"\x89PNG\r\n\x1a\n")
        or content.startswith((b"GIF87a", b"GIF89a"))
        or (content.startswith(b"RIFF") and content[8:12] == b"WEBP")
        or (len(content) >= 12 and content[4:12] in {b"ftypavif", b"ftypavis"})
    )


def inspect_image_bytes(content: bytes) -> InspectedImage:
    if not isinstance(content, bytes) or not content:
        raise ImageStagingError("image_non_raster_response", "image response is empty")
    if len(content) > MAX_IMAGE_BYTES:
        raise ImageStagingError("image_too_large", "image exceeds the byte limit")
    try:
        image = Image.open(BytesIO(content))
    except UnidentifiedImageError as exc:
        if looks_like_supported_raster(content):
            raise ImageStagingError(
                "image_decode_failed", "recognized raster image could not be decoded"
            ) from exc
        raise ImageStagingError(
            "image_non_raster_response", "response is not a recognized raster image"
        ) from exc
    except (OSError, SyntaxError, ValueError) as exc:
        raise ImageStagingError(
            "image_decode_failed", "raster image header could not be decoded"
        ) from exc
    try:
        with image:
            image_format = str(image.format or "").upper()
            width, height = image.size
            if image_format not in FORMAT_METADATA:
                raise ImageStagingError(
                    "image_non_raster_response", "unsupported raster image format"
            )
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ImageStagingError("image_too_large", "image exceeds the pixel limit")
            image.verify()
    except ImageStagingError:
        raise
    except (OSError, SyntaxError, ValueError) as exc:
        raise ImageStagingError(
            "image_decode_failed", "raster image payload could not be decoded"
        ) from exc
    extension, mime_type = FORMAT_METADATA[image_format]
    return InspectedImage(
        extension=extension,
        mime_type=mime_type,
        width=width,
        height=height,
        size_bytes=len(content),
        sha256=sha256(content).hexdigest(),
    )


def _safe_component(value: str) -> str:
    text = str(value or "").strip()
    if text and re.fullmatch(r"[A-Za-z0-9._-]{1,160}", text) and text not in {".", ".."}:
        return text
    if not text:
        raise ImageStagingError("image_manifest_identity_mismatch", "post ID is required")
    return f"id-{sha256(text.encode('utf-8')).hexdigest()}"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _manifest_payload(rows: Iterable[dict]) -> bytes:
    ordered = sorted(
        rows,
        key=lambda row: (
            row["platform_key"],
            row["platform_post_id"],
            row["image_role"],
            row["source_index"],
        ),
    )
    lines = [
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in ordered
    ]
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def upsert_manifest_rows_atomic(path: Path, rows: Iterable[dict]) -> None:
    """Upsert complete rows by manifest identity and atomically replace JSONL."""

    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    indexed: dict[tuple[str, str, str, int], dict] = {}
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            identity = (
                row["platform_key"],
                row["platform_post_id"],
                row["image_role"],
                row["source_index"],
            )
            indexed[identity] = row
    for row in rows:
        identity = (
            row["platform_key"],
            row["platform_post_id"],
            row["image_role"],
            row["source_index"],
        )
        indexed[identity] = row
    payload = _manifest_payload(indexed.values())
    part = target.parent / f".{target.name}.{secrets.token_hex(8)}.part"
    try:
        with part.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, target)
        _fsync_directory(target.parent)
    finally:
        if part.exists():
            part.unlink()


def failed_manifest_row(
    *,
    platform_key: str,
    platform_post_id: str,
    source_key: str,
    source_index: int,
    source_asset_key: str,
    source_url: str,
    attempts: int,
    error_code: str,
    http_status: int | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "platform_key": platform_key,
        "platform_post_id": str(platform_post_id),
        "image_role": "content",
        "source_index": source_index,
        "source_key": source_key,
        "source_asset_key": source_asset_key,
        "source_url": normalize_image_url(source_url),
        "fetch_status": "failed",
        "attempts": attempts,
        "http_status": http_status,
        "staging_path": None,
        "size_bytes": None,
        "mime_type": None,
        "width": None,
        "height": None,
        "sha256": None,
        "error_code": error_code,
    }


def stage_post_images(
    *,
    save_data_root: Path,
    platform_storage_key: str,
    platform_key: str,
    platform_post_id: str,
    source_key: str,
    assets: Sequence[ImageAsset],
) -> list[dict]:
    """Validate all post images, then promote one complete post directory atomically."""

    if not assets:
        return []
    inspected: list[tuple[ImageAsset, InspectedImage]] = []
    for asset in assets:
        try:
            normalized_url = normalize_image_url(asset.source_url)
            image = inspect_image_bytes(asset.content)
        except ImageStagingError as exc:
            if exc.source_index is None:
                exc.source_index = asset.source_index
            raise
        inspected.append(
            (
                ImageAsset(
                    source_index=asset.source_index,
                    source_asset_key=asset.source_asset_key,
                    source_url=normalized_url,
                    content=asset.content,
                    attempts=asset.attempts,
                    http_status=asset.http_status,
                ),
                image,
            )
        )

    platform_root = save_data_root.resolve() / platform_storage_key
    images_root = platform_root / "images"
    images_root.mkdir(parents=True, exist_ok=True)
    safe_post_id = _safe_component(platform_post_id)
    final_dir = images_root / safe_post_id
    part_dir = images_root / f".{safe_post_id}.{secrets.token_hex(8)}.part"
    part_dir.mkdir()
    try:
        for asset, image in inspected:
            target = part_dir / f"{asset.source_index:03d}.{image.extension}"
            with target.open("xb") as handle:
                handle.write(asset.content)
                handle.flush()
                os.fsync(handle.fileno())
        _fsync_directory(part_dir)
        if final_dir.exists():
            expected = {item.name: sha256(item.read_bytes()).hexdigest() for item in part_dir.iterdir()}
            actual = {
                item.name: sha256(item.read_bytes()).hexdigest()
                for item in final_dir.iterdir()
                if item.is_file()
            }
            if actual != expected:
                raise ImageStagingError(
                    "image_existing_conflict", "existing post image directory differs"
                )
            shutil.rmtree(part_dir)
        else:
            os.replace(part_dir, final_dir)
            _fsync_directory(images_root)
    finally:
        if part_dir.exists():
            shutil.rmtree(part_dir)

    rows: list[dict] = []
    for asset, image in inspected:
        relative_path = (
            Path(platform_storage_key)
            / "images"
            / safe_post_id
            / f"{asset.source_index:03d}.{image.extension}"
        ).as_posix()
        rows.append(
            {
                "schema_version": 1,
                "platform_key": platform_key,
                "platform_post_id": str(platform_post_id),
                "image_role": "content",
                "source_index": asset.source_index,
                "source_key": source_key,
                "source_asset_key": asset.source_asset_key,
                "source_url": asset.source_url,
                "fetch_status": "downloaded",
                "attempts": asset.attempts,
                "http_status": asset.http_status,
                "staging_path": relative_path,
                "size_bytes": image.size_bytes,
                "mime_type": image.mime_type,
                "width": image.width,
                "height": image.height,
                "sha256": image.sha256,
                "error_code": None,
            }
        )
    upsert_manifest_rows_atomic(platform_root / "image_manifest.jsonl", rows)
    return rows
