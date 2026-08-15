"""Shared validation and SQLite persistence for post image relationships."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from trippostcollect.artifacts.image_candidates import (
    normalize_image_url,
    source_asset_key_for_image,
)
from trippostcollect.artifacts.image_materialization import validate_image_file


class ImagePersistenceError(ValueError):
    """Raised before a partial post/image relationship can be committed."""


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def normalize_persistence_items(image_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate role/index/URL identities before preparing SQLite rows."""

    role_counters: Counter[str] = Counter()
    identities: set[tuple[str, int]] = set()
    normalized: list[dict[str, Any]] = []
    for raw_item in image_items:
        item = dict(raw_item)
        role = str(item.get("role") or "")
        if role not in {"content", "page"}:
            raise ImagePersistenceError(f"invalid image role: {role!r}")
        source_index = item.get("source_index")
        if source_index is None:
            source_index = role_counters[role]
        if isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 0:
            raise ImagePersistenceError("image source_index must be a non-negative integer")
        identity = (role, source_index)
        if identity in identities:
            raise ImagePersistenceError(f"duplicate image identity: {identity}")
        if not normalize_image_url(item.get("url")):
            raise ImagePersistenceError(f"invalid image URL at {identity}")
        item["role"] = role
        item["source_index"] = source_index
        identities.add(identity)
        role_counters[role] += 1
        normalized.append(item)
    content_indices = sorted(index for role, index in identities if role == "content")
    if content_indices != list(range(len(content_indices))):
        raise ImagePersistenceError("content image indices must be continuous from zero")
    return normalized


def _url_asset_identity(platform_key: str, image_url: str) -> str:
    try:
        return source_asset_key_for_image(platform_key, image_url)
    except ValueError:
        return str(normalize_image_url(image_url) or image_url)


def _local_file_path(local_path: str, project_root: Path) -> Path:
    raw = Path(local_path).expanduser()
    return raw if raw.is_absolute() else project_root / raw


def _verified_local_metadata(
    item: dict[str, Any],
    *,
    project_root: Path,
    allowed_root: Path,
    require_suffix_match: bool,
) -> dict[str, Any]:
    fields = ("local_path", "width", "height", "mime_type", "sha256")
    if not any(item.get(field) not in (None, "") for field in fields):
        return {}
    if any(item.get(field) in (None, "") for field in fields):
        raise ImagePersistenceError("partial local image metadata is not allowed")
    local_path = str(item["local_path"])
    try:
        verified = validate_image_file(
            _local_file_path(local_path, project_root),
            allowed_root=allowed_root,
            expected_sha256=str(item["sha256"]),
            require_suffix_match=require_suffix_match,
        )
    except (OSError, ValueError) as exc:
        raise ImagePersistenceError(f"local image verification failed: {exc}") from exc
    if (
        int(item["width"]) != verified.width
        or int(item["height"]) != verified.height
        or str(item["mime_type"]) != verified.mime_type
    ):
        raise ImagePersistenceError("local image metadata does not match file bytes")
    local_file = item.get("local_file")
    if not isinstance(local_file, dict):
        local_file = {
            "source": "verified_legacy_local_relation",
            "source_url": str(item.get("url") or ""),
            "size_bytes": verified.size_bytes,
        }
    return {
        "local_path": local_path,
        "width": verified.width,
        "height": verified.height,
        "mime_type": verified.mime_type,
        "sha256": verified.sha256,
        "local_file": local_file,
    }


def existing_image_records(conn: sqlite3.Connection, post_id: int) -> list[dict[str, Any]]:
    """Return persisted image rows in the item shape used by the shared matcher."""

    records: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT id, image_index, image_url, image_role, local_path,
               width, height, mime_type, sha256, raw_image_json
        FROM web_post_images
        WHERE web_post_id=?
        ORDER BY image_role, image_index
        """,
        (post_id,),
    ):
        raw = _json_object(row[9])
        records.append(
            {
                "id": int(row[0]),
                "source_index": int(row[1]),
                "url": str(row[2]),
                "role": str(row[3]),
                "local_path": row[4],
                "width": row[5],
                "height": row[6],
                "mime_type": row[7],
                "sha256": row[8],
                "source_asset_key": str(raw.get("source_asset_key") or ""),
                "local_file": raw.get("local_file"),
            }
        )
    return records


def _match_existing_image(
    platform_key: str,
    item: dict[str, Any],
    existing: list[dict[str, Any]],
    used_ids: set[int],
) -> dict[str, Any] | None:
    available = [row for row in existing if row["id"] not in used_ids and row["role"] == item["role"]]
    new_asset_key = str(item.get("source_asset_key") or "")
    if new_asset_key:
        exact = [row for row in available if row["source_asset_key"] == new_asset_key]
        if len(exact) == 1:
            return exact[0]
        available = [
            row
            for row in available
            if not row["source_asset_key"] or row["source_asset_key"] == new_asset_key
        ]

    url_identity = _url_asset_identity(platform_key, str(item["url"]))
    same_url = [
        row
        for row in available
        if _url_asset_identity(platform_key, row["url"]) == url_identity
    ]
    if len(same_url) == 1:
        return same_url[0]

    same_index = [
        row
        for row in available
        if not row["source_asset_key"]
        and row["source_index"] == item["source_index"]
        and _url_asset_identity(platform_key, row["url"]) == url_identity
    ]
    return same_index[0] if len(same_index) == 1 else None


def prepare_image_rows(
    platform_key: str,
    image_items: list[dict[str, Any]],
    existing_images: list[dict[str, Any]],
    *,
    project_root: Path,
    media_root: Path,
    require_local_images: bool,
) -> list[dict[str, Any]]:
    """Attach only verified local metadata and serialize rows for SQLite."""

    prepared: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    for item in image_items:
        local_metadata: dict[str, Any] = {}
        if any(
            item.get(field) not in (None, "")
            for field in ("local_path", "width", "height", "mime_type", "sha256", "local_file")
        ):
            local_metadata = _verified_local_metadata(
                item,
                project_root=project_root,
                allowed_root=media_root,
                require_suffix_match=True,
            )
        else:
            matched = _match_existing_image(platform_key, item, existing_images, used_ids)
            if matched is not None:
                used_ids.add(int(matched["id"]))
                try:
                    local_metadata = _verified_local_metadata(
                        matched,
                        project_root=project_root,
                        allowed_root=project_root,
                        require_suffix_match=False,
                    )
                except ImagePersistenceError:
                    local_metadata = {}
        if item["role"] == "content" and require_local_images and not local_metadata:
            raise ImagePersistenceError(
                f"content image lacks verified local file: source_index={item['source_index']}"
            )
        raw_image_payload = {
            key: value
            for key, value in item.items()
            if key not in {"local_path", "width", "height", "mime_type", "sha256", "local_file"}
        }
        if local_metadata:
            raw_image_payload["local_file"] = local_metadata["local_file"]
        prepared.append(
            {
                **item,
                **local_metadata,
                "raw_image_json": _json_dump(raw_image_payload),
            }
        )
    return prepared


def replace_image_rows(
    conn: sqlite3.Connection,
    post_id: int,
    prepared_images: Iterable[dict[str, Any]],
    *,
    roles: frozenset[str] | None = None,
) -> int:
    """Replace all or selected image roles using the shared SQLite row contract."""

    rows = list(prepared_images)
    if roles is None:
        conn.execute("DELETE FROM web_post_images WHERE web_post_id=?", (post_id,))
    else:
        if not roles:
            raise ImagePersistenceError("roles must not be empty")
        if any(str(item.get("role") or "") not in roles for item in rows):
            raise ImagePersistenceError("prepared image role is outside replacement scope")
        placeholders = ",".join("?" for _ in roles)
        conn.execute(
            f"DELETE FROM web_post_images WHERE web_post_id=? AND image_role IN ({placeholders})",
            (post_id, *sorted(roles)),
        )
    for item in rows:
        conn.execute(
            """
            INSERT INTO web_post_images (
                web_post_id, image_index, image_url, image_role, local_path,
                width, height, mime_type, sha256, raw_image_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                item["source_index"],
                item["url"],
                item["role"],
                item.get("local_path"),
                item.get("width"),
                item.get("height"),
                item.get("mime_type"),
                item.get("sha256"),
                item["raw_image_json"],
            ),
        )
    return len(rows)
