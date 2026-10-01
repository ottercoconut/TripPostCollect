"""正式正文图片清单复验、晋升与回滚。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from trippostcollect.artifacts.image_candidates import content_image_candidates
from trippostcollect.artifacts.image_candidates import source_asset_key_for_image
from trippostcollect.artifacts.image_manifest import ImageManifestError
from trippostcollect.artifacts.image_manifest import parse_manifest
from trippostcollect.artifacts.image_manifest import validate_post_manifest
from trippostcollect.artifacts.image_materialization import DEFAULT_ARCHIVE_IMAGE_MAX_BYTES
from trippostcollect.artifacts.image_materialization import ImageMaterializationError
from trippostcollect.artifacts.image_materialization import Sha256DuplicateSource
from trippostcollect.artifacts.image_materialization import promote_validated_image
from trippostcollect.artifacts.image_materialization import validate_image_file
from trippostcollect.application.contracts import ImagePersistenceError
from trippostcollect.core.paths import FORMAL_MEDIA_PERSISTENCE_LOCK
from trippostcollect.core.paths import LOCAL_MEDIA_ROOT
from trippostcollect.core.paths import PROJECT_ROOT
from trippostcollect.core.paths import ensure_parent
from trippostcollect.records.formal import post_id_for_record as post_id_for_record
from trippostcollect.runtime.helpers import _runtime_progress as _runtime_progress
from trippostcollect.runtime.process import PROCESS_PROGRESS_POLL_SECONDS as PROCESS_PROGRESS_POLL_SECONDS
from typing import Any
from typing import Callable
from typing import Iterator
from typing import TYPE_CHECKING
from urllib.parse import urlparse
import fcntl
import json
import re
import sqlite3
import sys
import time

if TYPE_CHECKING:
    from trippostcollect.artifacts.image_candidates import ImageCandidate
    from trippostcollect.artifacts.image_manifest import ImageManifestEntry
    from trippostcollect.artifacts.image_materialization import MaterializedImage
    from trippostcollect.artifacts.image_materialization import ValidatedImage
LEGACY_ZHIHU_TRANSFORM_SUFFIX_RE = re.compile(
    r"_(?:b|r|qhd|hd|xs|s|m|l|xl|xxl|original|watermark)"
    r"\.(?:avif|gif|jpe?g|png|webp)$",
    re.IGNORECASE,
)


RASTER_IMAGE_SUFFIX_RE = re.compile(
    r"\.(?:avif|gif|jpe?g|png|webp)$",
    re.IGNORECASE,
)


def _project_relative_evidence_path(path: Path, project_root: Path) -> str:
    resolved = path.expanduser().resolve(strict=True)
    if resolved != project_root and project_root not in resolved.parents:
        raise ImageMaterializationError(
            "image_path_escape",
            f"image evidence escapes project root: {resolved}",
        )
    return resolved.relative_to(project_root).as_posix()


def _load_manifest_with_evidence(
    path_value: str | Path,
    *,
    project_root: Path,
) -> tuple[Path, tuple[ImageManifestEntry, ...], dict[tuple[str, str, int], int]]:
    path = Path(path_value).expanduser().resolve(strict=True)
    _project_relative_evidence_path(path, project_root)
    payload = path.read_bytes()
    entries = parse_manifest(payload)
    line_numbers: dict[tuple[str, str, int], int] = {}
    entry_offset = 0
    for line_number, line in enumerate(payload.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        entry = entries[entry_offset]
        entry_offset += 1
        line_numbers[(entry.platform_key, entry.platform_post_id, entry.source_index)] = line_number
    return path, entries, line_numbers


def _staging_root_for_manifest_entry(
    manifest_path: Path,
    entry: ImageManifestEntry,
) -> Path:
    staging_path = Path(str(entry.staging_path))
    if staging_path.parts and staging_path.parts[0] == manifest_path.parent.name:
        return manifest_path.parent.parent
    return manifest_path.parent


def rollback_newly_promoted_images(
    materialized_by_identity: dict[str, list[MaterializedImage]],
    *,
    project_root: str | Path = PROJECT_ROOT,
    media_root: str | Path = LOCAL_MEDIA_ROOT,
    db_path: str | Path | None = None,
    progress_callback: Callable[[], object] | None = None,
) -> int:
    _runtime_progress(progress_callback)
    root = Path(project_root).expanduser().resolve(strict=True)
    media = Path(media_root).expanduser().resolve()
    if media != root and root not in media.parents:
        raise ImagePersistenceError("media root escapes project root")
    referenced_paths: set[str] = set()
    if db_path is not None:
        resolved_db = Path(db_path).expanduser().resolve()
        if resolved_db.is_file():
            try:
                with sqlite3.connect(f"file:{resolved_db}?mode=ro", uri=True) as conn:
                    referenced_paths = {
                        str(row[0])
                        for row in conn.execute(
                            """
                            SELECT DISTINCT local_path
                            FROM web_post_images
                            WHERE local_path IS NOT NULL AND local_path != ''
                            """
                        )
                    }
            except sqlite3.Error as exc:
                print(
                    "[image_promotion_rollback_skipped] "
                    f"reason=sqlite_reference_check_failed error={type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return 0
    removed = 0
    candidate_dirs: set[Path] = set()
    for images in materialized_by_identity.values():
        _runtime_progress(progress_callback)
        for item in images:
            if item.reused:
                continue
            if item.local_path in referenced_paths:
                continue
            path = (root / item.local_path).resolve()
            if path != media and media not in path.parents:
                raise ImagePersistenceError("promoted image escapes media root")
            candidate_dirs.add(path.parent)
            if path.is_file():
                path.unlink()
                removed += 1
    for directory in sorted(candidate_dirs, key=lambda value: len(value.parts), reverse=True):
        _runtime_progress(progress_callback)
        current = directory
        while current != media and media in current.parents:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
    return removed


@contextmanager
def formal_media_persistence_lock(
    *,
    enabled: bool,
    lock_path: str | Path = FORMAL_MEDIA_PERSISTENCE_LOCK,
    progress_callback: Callable[[], object] | None = None,
) -> Iterator[None]:
    if not enabled:
        yield
        return
    resolved_lock = ensure_parent(lock_path)
    with resolved_lock.open("a+b") as handle:
        if progress_callback is None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        else:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    _runtime_progress(progress_callback)
                    time.sleep(min(1.0, PROCESS_PROGRESS_POLL_SECONDS))
            _runtime_progress(progress_callback)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validated_manifest_rows_for_post(
    post_manifest_rows: list[tuple[ImageManifestEntry, Path, int]],
    candidates: list[ImageCandidate],
    *,
    project_root: Path,
) -> tuple[
    list[tuple[ImageCandidate, ImageManifestEntry, Path, int]],
    list[dict[str, Any]],
    list[tuple[ImageManifestEntry, Path, int]],
]:
    entries = [row[0] for row in post_manifest_rows]
    try:
        ordered_entries = validate_post_manifest(
            entries,
            candidates,
            require_downloaded=True,
        )
    except ImageManifestError as original_error:
        if (
            not candidates
            or any(candidate.platform_key != "zhihu" for candidate in candidates)
            or original_error.code != "image_manifest_count_mismatch"
        ):
            raise

        if len({manifest_path.resolve() for _, manifest_path, _ in post_manifest_rows}) != 1:
            raise original_error

        def is_real_zhimg_url(source_url: str) -> bool:
            hostname = (urlparse(source_url).hostname or "").lower()
            return hostname == "zhimg.com" or hostname.endswith(".zhimg.com")

        def legacy_zhihu_asset_key(source_url: str) -> str:
            path = urlparse(source_url).path
            logical_path = LEGACY_ZHIHU_TRANSFORM_SUFFIX_RE.sub("", path)
            legacy_identity = RASTER_IMAGE_SUFFIX_RE.sub("", logical_path)
            digest = sha256(legacy_identity.encode("utf-8")).hexdigest()
            return f"zhihu:urlsha256:{digest}"

        if any(not is_real_zhimg_url(candidate.source_url) for candidate in candidates):
            raise original_error

        evidence_by_entry_id = {
            id(entry): (manifest_path, line_number)
            for entry, manifest_path, line_number in post_manifest_rows
        }
        expected_by_asset = {
            candidate.source_asset_key: candidate for candidate in candidates
        }
        if len(expected_by_asset) != len(candidates):
            raise original_error

        entries_by_asset: dict[str, list[ImageManifestEntry]] = {}
        for entry in entries:
            if (
                entry.platform_key != "zhihu"
                or entry.image_role != "content"
                or entry.fetch_status != "downloaded"
                or entry.sha256 is None
                or not is_real_zhimg_url(entry.source_url)
            ):
                raise original_error
            canonical_asset_key = source_asset_key_for_image(
                "zhihu",
                entry.source_url,
            )
            if entry.source_asset_key not in {
                canonical_asset_key,
                legacy_zhihu_asset_key(entry.source_url),
            }:
                raise original_error
            entries_by_asset.setdefault(canonical_asset_key, []).append(entry)
        if set(entries_by_asset) != set(expected_by_asset):
            raise original_error

        matched_rows: list[tuple[ImageCandidate, ImageManifestEntry, Path, int]] = []
        reconciliations: list[dict[str, Any]] = []
        reconciliation_rows: list[tuple[ImageManifestEntry, Path, int]] = []
        for candidate in candidates:
            grouped_entries = entries_by_asset[candidate.source_asset_key]
            if any(
                entry.platform_post_id != candidate.platform_post_id
                or entry.source_key != candidate.source_key
                or source_asset_key_for_image("zhihu", entry.source_url)
                != candidate.source_asset_key
                for entry in grouped_entries
            ):
                raise original_error
            sha256_values = {entry.sha256 for entry in grouped_entries}
            if len(sha256_values) != 1 or len(
                {entry.source_url for entry in grouped_entries}
            ) != len(grouped_entries):
                raise original_error
            retained_entry = min(
                grouped_entries,
                key=lambda entry: (
                    entry.source_url != candidate.source_url,
                    entry.source_index,
                ),
            )
            manifest_path, line_number = evidence_by_entry_id[id(retained_entry)]
            matched_rows.append(
                (candidate, retained_entry, manifest_path, line_number)
            )
            for duplicate_entry in grouped_entries:
                if duplicate_entry is retained_entry:
                    continue
                duplicate_path, duplicate_line = evidence_by_entry_id[id(duplicate_entry)]
                reconciliation_rows.append(
                    (duplicate_entry, duplicate_path, duplicate_line)
                )
                reconciliations.append(
                    {
                        "identity": (
                            f"{candidate.platform_key}:id:{candidate.platform_post_id}"
                        ),
                        "sha256": str(retained_entry.sha256),
                        "retained_source_index": retained_entry.source_index,
                        "retained_source_url": retained_entry.source_url,
                        "retained_manifest_path": _project_relative_evidence_path(
                            manifest_path,
                            project_root,
                        ),
                        "retained_manifest_line": line_number,
                        "duplicate_source_index": duplicate_entry.source_index,
                        "duplicate_source_url": duplicate_entry.source_url,
                        "duplicate_manifest_path": _project_relative_evidence_path(
                            duplicate_path,
                            project_root,
                        ),
                        "duplicate_manifest_line": duplicate_line,
                    }
                )
        return matched_rows, reconciliations, reconciliation_rows

    evidence_by_entry_id = {
        id(entry): (manifest_path, line_number)
        for entry, manifest_path, line_number in post_manifest_rows
    }
    matched_rows = []
    for candidate, entry in zip(candidates, ordered_entries, strict=True):
        manifest_path, line_number = evidence_by_entry_id[id(entry)]
        matched_rows.append((candidate, entry, manifest_path, line_number))
    return matched_rows, [], []


def materialize_formal_record_images(
    selected: list[dict[str, Any]],
    *,
    project_root: str | Path = PROJECT_ROOT,
    media_root: str | Path = LOCAL_MEDIA_ROOT,
    promote: bool,
    progress_callback: Callable[[], object] | None = None,
) -> tuple[dict[str, Any], dict[str, list[MaterializedImage]], set[str]]:
    _runtime_progress(progress_callback)
    root = Path(project_root).expanduser().resolve(strict=True)
    resolved_media_root = Path(media_root).expanduser().resolve()
    if resolved_media_root != root and root not in resolved_media_root.parents:
        raise ImagePersistenceError("media root escapes project root")

    cache: dict[
        Path,
        tuple[
            tuple[ImageManifestEntry, ...],
            dict[tuple[str, str, int], int],
            str,
        ],
    ] = {}
    materialized_by_identity: dict[str, list[MaterializedImage]] = {}
    complete_identities: set[str] = set()
    manifest_evidence: dict[str, str] = {}
    failures: list[dict[str, Any]] = []
    expected_images = 0
    downloaded_images = 0
    validated_images = 0
    unique_images = 0
    sha256_duplicate_images = 0
    promoted_images = 0
    reused_images = 0
    retryable_failures = 0
    terminal_failures = 0
    sha256_duplicates: list[dict[str, Any]] = []
    legacy_manifest_reconciliations: list[dict[str, Any]] = []
    rolled_back_images = 0

    for item in selected:
        _runtime_progress(progress_callback)
        identity = str(item.get("identity") or "")
        platform_key = str(item.get("platform") or "")
        record = item.get("record") if isinstance(item.get("record"), dict) else {}
        candidates = content_image_candidates(platform_key, record)
        expected_images += len(candidates)
        post_id = post_id_for_record(platform_key, record)
        post_manifest_rows: list[
            tuple[ImageManifestEntry, Path, int]
        ] = []
        try:
            manifest_values: list[Path] = []
            seen_manifest_paths: set[Path] = set()
            for path_value in item.get("manifest_paths") or []:
                unresolved_path = Path(path_value).expanduser()
                resolved_path = (
                    unresolved_path.resolve()
                    if unresolved_path.is_absolute()
                    else (root / unresolved_path).resolve()
                )
                if resolved_path in seen_manifest_paths:
                    continue
                seen_manifest_paths.add(resolved_path)
                manifest_values.append(resolved_path)
            if not manifest_values:
                raise ImageManifestError(
                    "missing_image_manifest",
                    f"no image manifest is associated with {identity}",
                )
            for path_value in manifest_values:
                unresolved_path = Path(path_value).expanduser()
                cache_key = unresolved_path.resolve()
                cached = cache.get(cache_key)
                if cached is None:
                    path, entries, line_numbers = _load_manifest_with_evidence(
                        unresolved_path,
                        project_root=root,
                    )
                    payload_sha256 = sha256(path.read_bytes()).hexdigest()
                    cached = (entries, line_numbers, payload_sha256)
                    cache[path] = cached
                    manifest_evidence[_project_relative_evidence_path(path, root)] = payload_sha256
                else:
                    path = cache_key
                entries, line_numbers, _ = cached
                for entry in entries:
                    if entry.platform_key == platform_key and entry.platform_post_id == post_id:
                        post_manifest_rows.append(
                            (
                                entry,
                                path,
                                line_numbers[
                                    (entry.platform_key, entry.platform_post_id, entry.source_index)
                                ],
                            )
                        )

            (
                matched_manifest_rows,
                post_reconciliations,
                reconciliation_rows,
            ) = _validated_manifest_rows_for_post(
                post_manifest_rows,
                candidates,
                project_root=root,
            )
            downloaded_images += sum(
                entry.fetch_status == "downloaded"
                for _, entry, _, _ in matched_manifest_rows
            )
            validated_rows: list[
                tuple[ImageCandidate, ValidatedImage, Path, int, Path]
            ] = []
            for candidate, entry, manifest_path, manifest_line in matched_manifest_rows:
                _runtime_progress(progress_callback)
                staging_root = _staging_root_for_manifest_entry(manifest_path, entry)
                staged_path = staging_root / str(entry.staging_path)
                validated = validate_image_file(
                    staged_path,
                    allowed_root=staging_root,
                    expected_sha256=entry.sha256,
                    max_bytes=DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
                    require_suffix_match=True,
                )
                if any(
                    (
                        validated.size_bytes != entry.size_bytes,
                        validated.mime_type != entry.mime_type,
                        validated.width != entry.width,
                        validated.height != entry.height,
                    )
                ):
                    raise ImageMaterializationError(
                        "image_manifest_metadata_mismatch",
                        f"manifest byte metadata does not match staging file for {identity}",
                    )
                validated_images += 1
                validated_rows.append(
                    (candidate, validated, manifest_path, manifest_line, staging_root)
                )

            for entry, manifest_path, _ in reconciliation_rows:
                _runtime_progress(progress_callback)
                staging_root = _staging_root_for_manifest_entry(manifest_path, entry)
                staged_path = staging_root / str(entry.staging_path)
                validated = validate_image_file(
                    staged_path,
                    allowed_root=staging_root,
                    expected_sha256=entry.sha256,
                    max_bytes=DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
                    require_suffix_match=True,
                )
                if any(
                    (
                        validated.size_bytes != entry.size_bytes,
                        validated.mime_type != entry.mime_type,
                        validated.width != entry.width,
                        validated.height != entry.height,
                    )
                ):
                    raise ImageMaterializationError(
                        "image_manifest_metadata_mismatch",
                        "legacy manifest byte metadata does not match staging "
                        f"file for {identity}",
                    )
            legacy_manifest_reconciliations.extend(post_reconciliations)

            retained_rows: list[
                tuple[ImageCandidate, ValidatedImage, Path, int, Path]
            ] = []
            retained_by_sha256: dict[str, int] = {}
            duplicate_sources_by_retained: dict[int, list[Sha256DuplicateSource]] = {}
            for (
                candidate,
                validated,
                manifest_path,
                manifest_line,
                staging_root,
            ) in validated_rows:
                retained_position = retained_by_sha256.get(validated.sha256)
                if retained_position is None:
                    retained_by_sha256[validated.sha256] = len(retained_rows)
                    retained_rows.append(
                        (
                            candidate,
                            validated,
                            manifest_path,
                            manifest_line,
                            staging_root,
                        )
                    )
                    continue
                (
                    retained_candidate,
                    _,
                    retained_manifest_path,
                    retained_manifest_line,
                    _,
                ) = retained_rows[retained_position]
                duplicate = Sha256DuplicateSource(
                    source_index=candidate.source_index,
                    source_key=candidate.source_key,
                    source_asset_key=candidate.source_asset_key,
                    source_url=candidate.source_url,
                    manifest_path=_project_relative_evidence_path(manifest_path, root),
                    manifest_line=manifest_line,
                )
                duplicate_sources_by_retained.setdefault(retained_position, []).append(
                    duplicate
                )
                sha256_duplicate_images += 1
                sha256_duplicates.append(
                    {
                        "identity": identity,
                        "sha256": validated.sha256,
                        "retained_source_index": retained_candidate.source_index,
                        "retained_source_url": retained_candidate.source_url,
                        "retained_manifest_path": _project_relative_evidence_path(
                            retained_manifest_path,
                            root,
                        ),
                        "retained_manifest_line": retained_manifest_line,
                        "duplicate_source_index": candidate.source_index,
                        "duplicate_source_url": candidate.source_url,
                        "duplicate_manifest_path": duplicate.manifest_path,
                        "duplicate_manifest_line": duplicate.manifest_line,
                    }
                )
            unique_images += len(retained_rows)

            post_materialized: list[MaterializedImage] = []
            if not promote:
                complete_identities.add(identity)
                continue
            materialized_by_identity[identity] = post_materialized
            for retained_index, (
                candidate,
                validated,
                manifest_path,
                manifest_line,
                staging_root,
            ) in enumerate(retained_rows):
                _runtime_progress(progress_callback)
                persistence_candidate = replace(candidate, source_index=retained_index)
                promoted = promote_validated_image(
                    validated,
                    persistence_candidate,
                    staging_root=staging_root,
                    media_root=resolved_media_root,
                    project_root=root,
                )
                promoted = replace(
                    promoted,
                    manifest_source_index=candidate.source_index,
                    manifest_path=_project_relative_evidence_path(manifest_path, root),
                    manifest_line=manifest_line,
                    sha256_duplicate_sources=tuple(
                        duplicate_sources_by_retained.get(retained_index, [])
                    ),
                )
                post_materialized.append(promoted)
                reused_images += int(promoted.reused)
                promoted_images += int(not promoted.reused)
            if len(post_materialized) != len(retained_rows):
                raise ImageMaterializationError(
                    "image_manifest_count_mismatch",
                    f"promoted image count does not match SHA-256 unique images for {identity}",
                )
            complete_identities.add(identity)
        except BaseException as exc:
            expected_failure = isinstance(
                exc,
                (ImageManifestError, ImageMaterializationError, OSError, UnicodeError),
            )
            if not expected_failure:
                if promote:
                    rolled_back_images += rollback_newly_promoted_images(
                        materialized_by_identity,
                        project_root=root,
                        media_root=resolved_media_root,
                        progress_callback=progress_callback,
                    )
                raise
            code = getattr(exc, "code", "missing_image_manifest")
            failed_rows = [row for row in post_manifest_rows if row[0].fetch_status == "failed"]
            retryable_count = sum(
                row[0].error_code == "image_download_retryable" for row in failed_rows
            )
            failure_count = max(1, len(failed_rows), len(candidates) - len(post_manifest_rows))
            if code == "image_download_retryable":
                retryable_count = max(retryable_count, failure_count)
            retryable_failures += retryable_count
            terminal_failures += max(0, failure_count - retryable_count)
            failures.append(
                {
                    "identity": identity,
                    "code": str(code),
                    "message": str(exc),
                    "source_path": str(item.get("source_path") or ""),
                    "line_number": item.get("line_number"),
                }
            )

    manifest_items = [
        {"path": path, "sha256": digest}
        for path, digest in sorted(manifest_evidence.items())
    ]
    aggregate_manifest_sha256 = sha256(
        json.dumps(manifest_items, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    complete = (
        len(complete_identities) == len(selected)
        and validated_images == expected_images
        and not failures
    )
    if promote and not complete:
        rolled_back_images = rollback_newly_promoted_images(
            materialized_by_identity,
            project_root=root,
            media_root=resolved_media_root,
            progress_callback=progress_callback,
        )
        materialized_by_identity = {}
        complete_identities = set()
        promoted_images = 0
        reused_images = 0
    report = {
        "required": True,
        "promotion_required": promote,
        "candidate_posts": len(selected),
        "complete_posts": len(complete_identities),
        "expected_images": expected_images,
        "downloaded_images": downloaded_images,
        "validated_images": validated_images,
        "unique_images": unique_images,
        "sha256_duplicate_images": sha256_duplicate_images,
        "sha256_duplicates": sha256_duplicates,
        "legacy_manifest_reconciled_images": len(legacy_manifest_reconciliations),
        "legacy_manifest_reconciliations": legacy_manifest_reconciliations,
        "reused_images": reused_images,
        "promoted_images": promoted_images,
        "rolled_back_images": rolled_back_images,
        "retryable_failures": retryable_failures,
        "terminal_failures": terminal_failures,
        "complete": complete,
        "manifest_paths": [item["path"] for item in manifest_items],
        "manifest_sha256": aggregate_manifest_sha256,
        "manifest_evidence": manifest_items,
        "failures": failures,
    }
    _runtime_progress(progress_callback)
    return report, materialized_by_identity, complete_identities
