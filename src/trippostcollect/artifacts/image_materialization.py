"""Validate, stage, and promote immutable local post-body images."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import secrets
from typing import Iterable
import warnings

from PIL import Image, UnidentifiedImageError

from trippostcollect.artifacts.image_candidates import ImageCandidate
from trippostcollect.artifacts.image_proxy import (
    RemoteImageFetchError,
    validate_remote_image_response,
)
from trippostcollect.core.paths import LOCAL_MEDIA_ROOT, PROJECT_ROOT


DEFAULT_ARCHIVE_IMAGE_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_ARCHIVE_IMAGE_MAX_PIXELS = 100_000_000
SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}
)
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
PIL_FORMAT_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "AVIF": "image/avif",
}
SUFFIX_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
}
SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9_-]+")


class ImageMaterializationError(ValueError):
    """Image validation or promotion error with a stable report code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    path: Path
    size_bytes: int
    mime_type: str
    extension: str
    width: int
    height: int
    sha256: str


@dataclass(frozen=True, slots=True)
class MaterializedImage:
    platform_key: str
    platform_post_id: str
    image_role: str
    source_index: int
    source_key: str
    source_asset_key: str
    source_url: str
    local_path: str
    size_bytes: int
    mime_type: str
    width: int
    height: int
    sha256: str
    reused: bool


def _magic_mime(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if len(header) >= 16 and header[4:8] == b"ftyp" and any(
        brand in header[8:64] for brand in (b"avif", b"avis")
    ):
        return "image/avif"
    return None


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_within(path: Path, root: Path, *, code: str = "image_path_escape") -> Path:
    resolved_root = root.expanduser().resolve(strict=True)
    resolved = path.expanduser().resolve(strict=True)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ImageMaterializationError(code, f"image path escapes controlled root: {resolved}")
    return resolved


def _decode_dimensions(path: Path, magic_mime: str, max_pixels: int) -> tuple[int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = map(int, image.size)
                decoded_mime = PIL_FORMAT_MIME.get(str(image.format or "").upper())
                if decoded_mime != magic_mime:
                    raise ImageMaterializationError(
                        "image_decode_failed",
                        f"decoder format {decoded_mime!r} does not match magic {magic_mime}",
                    )
                if width <= 0 or height <= 0:
                    raise ImageMaterializationError("image_decode_failed", "decoded image dimensions are invalid")
                if width * height > max_pixels:
                    raise ImageMaterializationError(
                        "image_too_large",
                        f"decoded image exceeds {max_pixels} pixels",
                    )
                image.verify()
                return width, height
    except ImageMaterializationError:
        raise
    except Image.DecompressionBombError as exc:
        raise ImageMaterializationError("image_too_large", f"image decoder rejected pixel count: {exc}") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageMaterializationError("image_decode_failed", f"image decoder rejected file: {exc}") from exc


def validate_image_file(
    path: str | Path,
    *,
    allowed_root: str | Path | None = None,
    expected_sha256: str | None = None,
    max_bytes: int = DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
    max_pixels: int = DEFAULT_ARCHIVE_IMAGE_MAX_PIXELS,
) -> ValidatedImage:
    """Verify bounds, raster magic, decoder format, dimensions, and SHA-256."""

    candidate = Path(path).expanduser()
    if not candidate.exists() or not candidate.is_file():
        raise ImageMaterializationError("image_file_missing", f"image file does not exist: {candidate}")
    resolved = (
        _require_within(candidate, Path(allowed_root))
        if allowed_root is not None
        else candidate.resolve(strict=True)
    )
    size_bytes = resolved.stat().st_size
    if size_bytes <= 0:
        raise ImageMaterializationError("image_decode_failed", "image file is empty")
    if size_bytes > max_bytes:
        raise ImageMaterializationError("image_too_large", f"image exceeds {max_bytes} bytes")

    with resolved.open("rb") as file_handle:
        header = file_handle.read(64)
    magic_mime = _magic_mime(header)
    if magic_mime not in SUPPORTED_IMAGE_MIME_TYPES:
        raise ImageMaterializationError("image_non_raster_response", "file is not an allowed raster image")
    suffix_mime = SUFFIX_MIME.get(resolved.suffix.lower())
    if suffix_mime is not None and suffix_mime != magic_mime:
        raise ImageMaterializationError("image_decode_failed", "file suffix does not match raster bytes")

    width, height = _decode_dimensions(resolved, magic_mime, max_pixels)
    actual_sha256 = _sha256_file(resolved)
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise ImageMaterializationError("image_hash_mismatch", "image SHA-256 does not match manifest")
    return ValidatedImage(
        path=resolved,
        size_bytes=size_bytes,
        mime_type=magic_mime,
        extension=MIME_EXTENSIONS[magic_mime],
        width=width,
        height=height,
        sha256=actual_sha256,
    )


def _safe_relative_stem(value: str | PurePosixPath) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) in {"", ".", ".."}
        or "\\" in str(value)
        or any(part in {"", ".", ".."} for part in path.parts)
        or re.match(r"^[A-Za-z]:", str(value))
    ):
        raise ImageMaterializationError("image_path_escape", "staging stem must be a safe relative path")
    return path


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_staging_image(
    chunks: Iterable[bytes],
    *,
    staging_root: str | Path,
    relative_stem: str | PurePosixPath,
    content_type: str | None = None,
    content_length: str | int | None = None,
    source_url: str = "https://example.invalid/image",
    max_bytes: int = DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
    max_pixels: int = DEFAULT_ARCHIVE_IMAGE_MAX_PIXELS,
) -> ValidatedImage:
    """Stream one bounded image to ``.part`` and atomically publish it in staging."""

    if content_type is not None or content_length is not None:
        try:
            validate_remote_image_response(
                content_type=content_type,
                content_length=content_length,
                url=source_url,
                max_bytes=max_bytes,
                allowed_media_types=SUPPORTED_IMAGE_MIME_TYPES,
            )
        except RemoteImageFetchError as exc:
            code = "image_too_large" if "exceeds" in str(exc) else "image_non_raster_response"
            raise ImageMaterializationError(code, str(exc)) from exc

    root = Path(staging_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    stem = _safe_relative_stem(relative_stem)
    destination_parent = root.joinpath(*stem.parts).parent
    destination_parent.mkdir(parents=True, exist_ok=True)
    if root != destination_parent and root not in destination_parent.resolve().parents:
        raise ImageMaterializationError("image_path_escape", "staging destination escapes root")
    part_path = destination_parent / f".{stem.name}.{secrets.token_hex(8)}.part"

    size_bytes = 0
    with part_path.open("xb") as file_handle:
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise ImageMaterializationError("image_decode_failed", "image stream yielded non-bytes")
            size_bytes += len(chunk)
            if size_bytes > max_bytes:
                raise ImageMaterializationError("image_too_large", f"image exceeds {max_bytes} bytes")
            file_handle.write(chunk)
        file_handle.flush()
        os.fsync(file_handle.fileno())

    validated_part = validate_image_file(
        part_path,
        allowed_root=root,
        max_bytes=max_bytes,
        max_pixels=max_pixels,
    )
    final_path = destination_parent / f"{stem.name}{validated_part.extension}"
    if final_path.exists():
        existing = validate_image_file(final_path, allowed_root=root, max_bytes=max_bytes, max_pixels=max_pixels)
        if existing.sha256 != validated_part.sha256:
            raise ImageMaterializationError("image_promotion_conflict", "staging destination has different bytes")
        part_path.unlink()
        return existing
    os.replace(part_path, final_path)
    _fsync_directory(destination_parent)
    return validate_image_file(
        final_path,
        allowed_root=root,
        expected_sha256=validated_part.sha256,
        max_bytes=max_bytes,
        max_pixels=max_pixels,
    )


def safe_platform_post_id(platform_post_id: str) -> str:
    value = str(platform_post_id or "")
    if value and SAFE_COMPONENT_RE.fullmatch(value):
        return value
    return f"id-{sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _project_relative(path: Path, project_root: Path) -> str:
    resolved_root = project_root.expanduser().resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ImageMaterializationError("image_path_escape", "long-term image escapes project root")
    return resolved.relative_to(resolved_root).as_posix()


def promote_validated_image(
    validated: ValidatedImage,
    candidate: ImageCandidate,
    *,
    staging_root: str | Path,
    media_root: str | Path = LOCAL_MEDIA_ROOT,
    project_root: str | Path = PROJECT_ROOT,
    max_bytes: int = DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
    max_pixels: int = DEFAULT_ARCHIVE_IMAGE_MAX_PIXELS,
) -> MaterializedImage:
    """Copy a verified staging file to its immutable content-hash path."""

    current = validate_image_file(
        validated.path,
        allowed_root=staging_root,
        expected_sha256=validated.sha256,
        max_bytes=max_bytes,
        max_pixels=max_pixels,
    )
    if candidate.platform_key not in {"bilibili", "weibo", "xhs", "douyin", "zhihu"}:
        raise ImageMaterializationError("image_manifest_identity_mismatch", "unsupported platform")
    root = Path(project_root).expanduser().resolve(strict=True)
    media = Path(media_root).expanduser().resolve()
    if media != root and root not in media.parents:
        raise ImageMaterializationError("image_path_escape", "media root escapes project root")
    media.mkdir(parents=True, exist_ok=True)

    platform_dir = media / candidate.platform_key
    platform_dir.mkdir(parents=True, exist_ok=True)
    resolved_platform_dir = platform_dir.resolve(strict=True)
    if resolved_platform_dir != media and media not in resolved_platform_dir.parents:
        raise ImageMaterializationError("image_path_escape", "long-term platform directory escapes media root")
    target_dir = resolved_platform_dir / safe_platform_post_id(candidate.platform_post_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_target_dir = target_dir.resolve(strict=True)
    if resolved_target_dir != media and media not in resolved_target_dir.parents:
        raise ImageMaterializationError("image_path_escape", "long-term post directory escapes media root")
    target_dir = resolved_target_dir
    target = target_dir / f"{candidate.source_index:03d}-{current.sha256[:16]}{current.extension}"
    reused = False
    if target.exists():
        try:
            existing = validate_image_file(
                target,
                allowed_root=media,
                expected_sha256=current.sha256,
                max_bytes=max_bytes,
                max_pixels=max_pixels,
            )
        except ImageMaterializationError as exc:
            raise ImageMaterializationError(
                "image_promotion_conflict",
                f"long-term target conflicts with staged image: {exc}",
            ) from exc
        reused = True
        promoted = existing
    else:
        part_path = target_dir / f".{target.name}.{secrets.token_hex(8)}.part"
        with current.path.open("rb") as source, part_path.open("xb") as destination:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        copied = validate_image_file(
            part_path,
            allowed_root=media,
            expected_sha256=current.sha256,
            max_bytes=max_bytes,
            max_pixels=max_pixels,
        )
        os.replace(part_path, target)
        _fsync_directory(target_dir)
        promoted = validate_image_file(
            target,
            allowed_root=media,
            expected_sha256=copied.sha256,
            max_bytes=max_bytes,
            max_pixels=max_pixels,
        )

    return MaterializedImage(
        platform_key=candidate.platform_key,
        platform_post_id=candidate.platform_post_id,
        image_role=candidate.image_role,
        source_index=candidate.source_index,
        source_key=candidate.source_key,
        source_asset_key=candidate.source_asset_key,
        source_url=candidate.source_url,
        local_path=_project_relative(promoted.path, root),
        size_bytes=promoted.size_bytes,
        mime_type=promoted.mime_type,
        width=promoted.width,
        height=promoted.height,
        sha256=promoted.sha256,
        reused=reused,
    )
