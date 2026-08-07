"""Schema v1 model and deterministic validation for image download manifests."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from hashlib import sha256
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import secrets
from typing import Any, Iterable

from trippostcollect.artifacts.image_candidates import ImageCandidate, normalize_image_url


IMAGE_MANIFEST_SCHEMA_VERSION = 1
SUPPORTED_PLATFORMS = frozenset({"bilibili", "weibo", "xhs", "douyin", "zhihu"})
SUPPORTED_FETCH_STATUSES = frozenset({"downloaded", "failed"})
SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}
)
SOURCE_KEYS = {
    "bilibili": "image_urls",
    "weibo": "image_list",
    "xhs": "image_list",
    "douyin": "note_download_url",
    "zhihu": "image_list",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{1,127}")


class ImageManifestError(ValueError):
    """Manifest validation error carrying a stable report code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ImageManifestEntry:
    schema_version: int
    platform_key: str
    platform_post_id: str
    image_role: str
    source_index: int
    source_key: str
    source_asset_key: str
    source_url: str
    fetch_status: str
    attempts: int
    http_status: int | None
    staging_path: str | None
    size_bytes: int | None
    mime_type: str | None
    width: int | None
    height: int | None
    sha256: str | None
    error_code: str | None

    def __post_init__(self) -> None:
        _validate_entry(self)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ImageManifestEntry:
        if not isinstance(payload, dict):
            raise ImageManifestError("missing_image_manifest", "manifest row must be an object")
        expected = {field.name for field in fields(cls)}
        actual = set(payload)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ImageManifestError(
                "missing_image_manifest",
                f"manifest fields do not match schema v1: missing={missing}, extra={extra}",
            )
        return cls(**payload)


def _require_int(value: Any, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ImageManifestError(
            "missing_image_manifest",
            f"{field_name} must be an integer >= {minimum}",
        )
    return value


def _validate_staging_path(value: str | None) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "\\" in value
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise ImageManifestError("image_path_escape", "staging_path must be a POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ImageManifestError("image_path_escape", "staging_path escapes the platform data root")


def _validate_entry(entry: ImageManifestEntry) -> None:
    if entry.schema_version != IMAGE_MANIFEST_SCHEMA_VERSION:
        raise ImageManifestError("missing_image_manifest", "unsupported image manifest schema")
    if entry.platform_key not in SUPPORTED_PLATFORMS:
        raise ImageManifestError("image_manifest_identity_mismatch", "unsupported platform_key")
    if not isinstance(entry.platform_post_id, str) or not entry.platform_post_id.strip():
        raise ImageManifestError("image_manifest_identity_mismatch", "platform_post_id is required")
    if entry.image_role != "content":
        raise ImageManifestError("image_manifest_identity_mismatch", "manifest image_role must be content")
    _require_int(entry.source_index, "source_index")
    if entry.source_key != SOURCE_KEYS[entry.platform_key]:
        raise ImageManifestError("image_manifest_identity_mismatch", "source_key is not authoritative")
    if (
        not isinstance(entry.source_asset_key, str)
        or not entry.source_asset_key.startswith(f"{entry.platform_key}:")
        or len(entry.source_asset_key) > 512
        or any(marker in entry.source_asset_key for marker in ("\r", "\n", "?", "://"))
    ):
        raise ImageManifestError("image_manifest_identity_mismatch", "invalid source_asset_key")
    if normalize_image_url(entry.source_url) != entry.source_url:
        raise ImageManifestError("image_manifest_identity_mismatch", "source_url is not normalized HTTP(S)")
    if entry.fetch_status not in SUPPORTED_FETCH_STATUSES:
        raise ImageManifestError("missing_image_manifest", "unsupported fetch_status")
    _require_int(entry.attempts, "attempts", minimum=1)
    if entry.http_status is not None:
        _require_int(entry.http_status, "http_status", minimum=100)
        if entry.http_status > 599:
            raise ImageManifestError("missing_image_manifest", "http_status must be <= 599")

    success_values = (
        entry.staging_path,
        entry.size_bytes,
        entry.mime_type,
        entry.width,
        entry.height,
        entry.sha256,
    )
    if entry.fetch_status == "downloaded":
        _validate_staging_path(entry.staging_path)
        _require_int(entry.size_bytes, "size_bytes", minimum=1)
        _require_int(entry.width, "width", minimum=1)
        _require_int(entry.height, "height", minimum=1)
        if entry.mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
            raise ImageManifestError("image_non_raster_response", "unsupported downloaded image MIME")
        if not isinstance(entry.sha256, str) or not SHA256_RE.fullmatch(entry.sha256):
            raise ImageManifestError("image_hash_mismatch", "downloaded image SHA-256 is invalid")
        if entry.http_status is None or not 200 <= entry.http_status < 300:
            raise ImageManifestError("missing_image_manifest", "downloaded row requires a 2xx status")
        if entry.error_code is not None:
            raise ImageManifestError("missing_image_manifest", "downloaded row cannot have error_code")
    else:
        if any(value is not None for value in success_values):
            raise ImageManifestError("missing_image_manifest", "failed row cannot contain success metadata")
        if not isinstance(entry.error_code, str) or not ERROR_CODE_RE.fullmatch(entry.error_code):
            raise ImageManifestError("missing_image_manifest", "failed row requires a stable error_code")


def serialize_manifest(entries: Iterable[ImageManifestEntry]) -> bytes:
    ordered = sorted(
        entries,
        key=lambda item: (
            item.platform_key,
            item.platform_post_id,
            item.image_role,
            item.source_index,
        ),
    )
    lines = [
        json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for entry in ordered
    ]
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def manifest_sha256(entries: Iterable[ImageManifestEntry]) -> str:
    return sha256(serialize_manifest(entries)).hexdigest()


def write_manifest_atomic(
    path: str | Path,
    entries: Iterable[ImageManifestEntry],
) -> tuple[Path, str]:
    """Validate and atomically replace one deterministic UTF-8 JSONL manifest."""

    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = serialize_manifest(entries)
    parse_manifest(payload)
    part = target.parent / f".{target.name}.{secrets.token_hex(8)}.part"
    with part.open("xb") as file_handle:
        file_handle.write(payload)
        file_handle.flush()
        os.fsync(file_handle.fileno())
    os.replace(part, target)
    descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return target, sha256(payload).hexdigest()


def parse_manifest(payload: str | bytes) -> tuple[ImageManifestEntry, ...]:
    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    entries: list[ImageManifestEntry] = []
    seen: set[tuple[str, str, str, int]] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ImageManifestError(
                "missing_image_manifest",
                f"manifest line {line_number} is invalid JSON",
            ) from exc
        entry = ImageManifestEntry.from_dict(raw)
        identity = (
            entry.platform_key,
            entry.platform_post_id,
            entry.image_role,
            entry.source_index,
        )
        if identity in seen:
            raise ImageManifestError(
                "image_manifest_identity_mismatch",
                f"duplicate manifest identity at line {line_number}",
            )
        seen.add(identity)
        entries.append(entry)
    return tuple(entries)


def validate_post_manifest(
    entries: Iterable[ImageManifestEntry],
    candidates: Iterable[ImageCandidate],
    *,
    require_downloaded: bool = True,
) -> tuple[ImageManifestEntry, ...]:
    manifest_entries = tuple(entries)
    expected = tuple(candidates)
    if len(manifest_entries) != len(expected):
        raise ImageManifestError(
            "image_manifest_count_mismatch",
            f"manifest rows={len(manifest_entries)} expected={len(expected)}",
        )
    indexed: dict[tuple[str, int], ImageManifestEntry] = {}
    for entry in manifest_entries:
        identity = (entry.image_role, entry.source_index)
        if identity in indexed:
            raise ImageManifestError(
                "image_manifest_identity_mismatch",
                f"duplicate role/source_index: {identity}",
            )
        indexed[identity] = entry

    ordered: list[ImageManifestEntry] = []
    for candidate in expected:
        entry = indexed.get((candidate.image_role, candidate.source_index))
        if entry is None or any(
            (
                entry.platform_key != candidate.platform_key,
                entry.platform_post_id != candidate.platform_post_id,
                entry.source_key != candidate.source_key,
                entry.source_asset_key != candidate.source_asset_key,
                entry.source_url != candidate.source_url,
            )
        ):
            raise ImageManifestError(
                "image_manifest_identity_mismatch",
                f"manifest does not match candidate index {candidate.source_index}",
            )
        if require_downloaded and entry.fetch_status != "downloaded":
            raise ImageManifestError(
                entry.error_code or "image_download_retryable",
                f"candidate index {candidate.source_index} was not downloaded",
            )
        ordered.append(entry)
    return tuple(ordered)
