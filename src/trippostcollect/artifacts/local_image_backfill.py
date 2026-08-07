"""Recover auditable local-image relationships from MediaCrawler XHS artifacts."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable
from urllib.parse import urlparse

from trippostcollect.core.paths import PROJECT_ROOT


IMAGE_FILE_RE = re.compile(r"^(?P<index>\d+)\.(?:avif|gif|jpe?g|png|webp)$", re.IGNORECASE)
XHS_STABLE_PATH_MARKERS = ("/notes_pre_post/", "/notes_post/", "/notes/")


@dataclass(frozen=True)
class SourceImageSequence:
    jsonl_path: Path
    line_number: int
    urls: tuple[str, ...]


@dataclass(frozen=True)
class LocalImageMapping:
    image_row_id: int
    web_post_id: int
    platform_post_id: str
    image_index: int
    image_url: str
    source_image_url: str
    source_image_index: int
    source_jsonl_path: Path
    source_jsonl_local_path: str
    source_jsonl_line: int
    file_path: Path
    local_path: str
    sha256: str
    mime_type: str
    width: int | None
    height: int | None


@dataclass(frozen=True)
class BackfillPlan:
    mappings: tuple[LocalImageMapping, ...]
    reason_counts: dict[str, int]
    scanned_image_roots: int
    scanned_note_directories: int
    source_records: int
    candidate_posts: int
    database_posts: int
    database_raw_image_urls: int


def json_load_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def xhs_image_urls(value: Any) -> tuple[str, ...]:
    values: list[Any]
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        values = value
    else:
        return ()

    urls: list[str] = []
    for item in values:
        if isinstance(item, dict):
            raw = next(
                (
                    item.get(key)
                    for key in ("url_default", "url", "url_pre", "image_url")
                    if item.get(key)
                ),
                None,
            )
        else:
            raw = item
        url = str(raw or "").strip()
        if url.startswith("//"):
            url = f"https:{url}"
        if url.startswith(("http://", "https://")):
            urls.append(url)
    return tuple(dict.fromkeys(urls))


def xhs_image_identity(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path
    for marker in XHS_STABLE_PATH_MARKERS:
        if marker in path:
            return f"xhs:{marker.lstrip('/')}{path.split(marker, 1)[1]}"
    return f"url:{parsed.netloc.lower()}{path}"


def indexed_image_files(image_dir: Path) -> tuple[Path, ...] | None:
    indexed: dict[int, Path] = {}
    for path in image_dir.iterdir():
        if not path.is_file():
            continue
        match = IMAGE_FILE_RE.fullmatch(path.name)
        if not match:
            continue
        index = int(match.group("index"))
        if index in indexed:
            return None
        indexed[index] = path
    if not indexed or sorted(indexed) != list(range(len(indexed))):
        return None
    return tuple(indexed[index] for index in range(len(indexed)))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_metadata(path: Path) -> tuple[str, int | None, int | None]:
    header = path.read_bytes()[:32]
    mime_type = "application/octet-stream"
    if header.startswith(b"\xff\xd8\xff"):
        mime_type = "image/jpeg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    elif header.startswith((b"GIF87a", b"GIF89a")):
        mime_type = "image/gif"
    elif len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        mime_type = "image/webp"
    elif len(header) >= 12 and header[4:8] == b"ftyp" and b"avif" in header[8:16]:
        mime_type = "image/avif"

    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
            detected = Image.MIME.get(image.format or "")
            if detected:
                mime_type = detected
            return mime_type, int(width), int(height)
    except (ImportError, OSError, ValueError):
        return mime_type, None, None


def project_relative_path(path: Path, *, project_root: Path = PROJECT_ROOT) -> str:
    resolved = path.resolve(strict=True)
    root = project_root.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"local image escapes project root: {resolved}")
    return resolved.relative_to(root).as_posix()


def discover_image_roots(runs_root: Path) -> tuple[Path, ...]:
    if not runs_root.is_dir():
        return ()
    return tuple(sorted(path for path in runs_root.glob("*/*/xhs/data/xhs/images") if path.is_dir()))


def _source_sequences(jsonl_dir: Path, note_ids: set[str]) -> tuple[dict[str, list[SourceImageSequence]], int]:
    sequences: dict[str, list[SourceImageSequence]] = defaultdict(list)
    source_records = 0
    if not jsonl_dir.is_dir():
        return sequences, source_records

    for jsonl_path in sorted(jsonl_dir.glob("*.jsonl")):
        with jsonl_path.open(encoding="utf-8") as file_handle:
            for line_number, line in enumerate(file_handle, start=1):
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(row, dict):
                    continue
                note_id = str(row.get("note_id") or "").strip()
                if note_id not in note_ids:
                    continue
                urls = xhs_image_urls(row.get("image_list"))
                if not urls:
                    continue
                sequences[note_id].append(
                    SourceImageSequence(jsonl_path=jsonl_path, line_number=line_number, urls=urls)
                )
                source_records += 1
    return sequences, source_records


def _database_posts(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    posts: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        """
        SELECT
          p.id AS web_post_id,
          p.platform_post_id,
          p.raw_sample_json,
          i.id AS image_row_id,
          i.image_index,
          i.image_url,
          i.image_role,
          i.local_path
        FROM web_posts p
        LEFT JOIN web_post_images i ON i.web_post_id=p.id
        WHERE p.platform_key='xhs' AND p.platform_post_id IS NOT NULL
        ORDER BY p.id, i.image_index
        """
    )
    for row in rows:
        note_id = str(row["platform_post_id"])
        post = posts.setdefault(
            note_id,
            {
                "web_post_id": int(row["web_post_id"]),
                "raw_sample_json": row["raw_sample_json"],
                "images": [],
            },
        )
        if row["image_row_id"] is not None:
            post["images"].append(dict(row))
    return posts


def _unique_database_image_rows(
    post: dict[str, Any],
    current_urls: tuple[str, ...],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    rows_by_identity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in post["images"]:
        rows_by_identity[xhs_image_identity(str(row["image_url"]))].append(row)

    selected: dict[str, dict[str, Any]] = {}
    for url in current_urls:
        identity = xhs_image_identity(url)
        matches = rows_by_identity.get(identity, [])
        if len(matches) != 1:
            return {}, "database_image_not_unique"
        if str(matches[0]["image_role"]) != "content":
            return {}, "database_image_not_content"
        selected[identity] = matches[0]
    if len(selected) != len(current_urls):
        return {}, "database_image_not_unique"
    return selected, None


def build_backfill_plan(
    conn: sqlite3.Connection,
    runs_root: Path,
    *,
    project_root: Path = PROJECT_ROOT,
) -> BackfillPlan:
    conn.row_factory = sqlite3.Row
    posts = _database_posts(conn)
    image_roots = discover_image_roots(runs_root)
    reasons: Counter[str] = Counter()
    candidates: dict[str, list[tuple[bool, Path, SourceImageSequence, tuple[Path, ...]]]] = defaultdict(list)
    scanned_note_directories = 0
    source_records = 0

    for image_root in image_roots:
        note_dirs = tuple(sorted(path for path in image_root.iterdir() if path.is_dir()))
        scanned_note_directories += len(note_dirs)
        sequences, root_source_records = _source_sequences(
            image_root.parent / "jsonl",
            {path.name for path in note_dirs},
        )
        source_records += root_source_records
        for note_dir in note_dirs:
            note_id = note_dir.name
            post = posts.get(note_id)
            if post is None:
                reasons["post_not_in_database"] += 1
                continue
            files = indexed_image_files(note_dir)
            if files is None:
                reasons["local_files_not_contiguous"] += 1
                continue
            current_raw = json_load_object(post["raw_sample_json"])
            current_urls = xhs_image_urls(current_raw.get("image_list"))
            if not current_urls:
                reasons["database_raw_images_missing"] += 1
                continue
            current_identities = tuple(xhs_image_identity(url) for url in current_urls)
            matching_sources = []
            for source in sequences.get(note_id, []):
                if len(source.urls) != len(files):
                    continue
                source_identities = tuple(xhs_image_identity(url) for url in source.urls)
                if source_identities == current_identities:
                    matching_sources.append(source)
            if not matching_sources:
                reasons["source_urls_or_count_mismatch"] += 1
                continue
            unique_sequences = {
                tuple(xhs_image_identity(url) for url in source.urls)
                for source in matching_sources
            }
            if len(unique_sequences) != 1:
                reasons["ambiguous_source_sequences"] += 1
                continue
            source = max(matching_sources, key=lambda item: (item.jsonl_path.as_posix(), item.line_number))
            candidates[note_id].append((source.urls == current_urls, note_dir, source, files))

    mappings: list[LocalImageMapping] = []
    for note_id, note_candidates in sorted(candidates.items()):
        post = posts[note_id]
        current_raw = json_load_object(post["raw_sample_json"])
        current_urls = xhs_image_urls(current_raw.get("image_list"))
        selected_rows, error = _unique_database_image_rows(post, current_urls)
        if error:
            reasons[error] += 1
            continue
        exact_urls, note_dir, source, files = max(
            note_candidates,
            key=lambda item: (item[0], item[1].as_posix()),
        )
        del exact_urls, note_dir
        for source_index, (source_url, current_url, file_path) in enumerate(
            zip(source.urls, current_urls, files, strict=True)
        ):
            row = selected_rows[xhs_image_identity(current_url)]
            mime_type, width, height = image_metadata(file_path)
            mappings.append(
                LocalImageMapping(
                    image_row_id=int(row["image_row_id"]),
                    web_post_id=int(post["web_post_id"]),
                    platform_post_id=note_id,
                    image_index=int(row["image_index"]),
                    image_url=str(row["image_url"]),
                    source_image_url=source_url,
                    source_image_index=source_index,
                    source_jsonl_path=source.jsonl_path,
                    source_jsonl_local_path=project_relative_path(
                        source.jsonl_path,
                        project_root=project_root,
                    ),
                    source_jsonl_line=source.line_number,
                    file_path=file_path,
                    local_path=project_relative_path(file_path, project_root=project_root),
                    sha256=sha256_file(file_path),
                    mime_type=mime_type,
                    width=width,
                    height=height,
                )
            )

    return BackfillPlan(
        mappings=tuple(mappings),
        reason_counts=dict(sorted(reasons.items())),
        scanned_image_roots=len(image_roots),
        scanned_note_directories=scanned_note_directories,
        source_records=source_records,
        candidate_posts=len(candidates),
        database_posts=len(posts),
        database_raw_image_urls=sum(
            len(xhs_image_urls(json_load_object(post["raw_sample_json"]).get("image_list")))
            for post in posts.values()
        ),
    )


def mapping_digest(mappings: Iterable[LocalImageMapping]) -> str:
    digest = hashlib.sha256()
    for mapping in sorted(mappings, key=lambda item: item.image_row_id):
        digest.update(
            json.dumps(
                {
                    "image_row_id": mapping.image_row_id,
                    "local_path": mapping.local_path,
                    "sha256": mapping.sha256,
                    "source_image_index": mapping.source_image_index,
                    "source_image_url": mapping.source_image_url,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _raw_image_json_with_local_file(value: str | None, mapping: LocalImageMapping) -> str:
    payload = json_load_object(value)
    payload["local_file"] = {
        "source": "mediacrawler_xhs_media",
        "source_image_index": mapping.source_image_index,
        "source_image_url": mapping.source_image_url,
        "source_jsonl_path": mapping.source_jsonl_local_path,
        "source_jsonl_line": mapping.source_jsonl_line,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def apply_backfill_plan(conn: sqlite3.Connection, plan: BackfillPlan) -> dict[str, int]:
    updated = unchanged = conflicts = 0
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    try:
        for mapping in plan.mappings:
            row = conn.execute(
                """
                SELECT local_path, sha256, raw_image_json
                FROM web_post_images
                WHERE id=? AND web_post_id=? AND image_url=?
                """,
                (mapping.image_row_id, mapping.web_post_id, mapping.image_url),
            ).fetchone()
            if row is None:
                conflicts += 1
                continue
            existing_path = str(row["local_path"] or "")
            if existing_path and existing_path != mapping.local_path:
                conflicts += 1
                continue
            if existing_path == mapping.local_path and str(row["sha256"] or "") == mapping.sha256:
                unchanged += 1
                continue
            if not mapping.file_path.is_file() or sha256_file(mapping.file_path) != mapping.sha256:
                conflicts += 1
                continue
            conn.execute(
                """
                UPDATE web_post_images
                SET local_path=?, width=?, height=?, mime_type=?, sha256=?, raw_image_json=?
                WHERE id=?
                """,
                (
                    mapping.local_path,
                    mapping.width,
                    mapping.height,
                    mapping.mime_type,
                    mapping.sha256,
                    _raw_image_json_with_local_file(row["raw_image_json"], mapping),
                    mapping.image_row_id,
                ),
            )
            updated += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"updated": updated, "unchanged": unchanged, "conflicts": conflicts}
