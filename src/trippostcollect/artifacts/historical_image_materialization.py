"""Plan and apply recoverable historical post-image relationship rebuilds."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Collection, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from trippostcollect.artifacts.image_candidates import (
    ImageCandidate,
    content_image_candidates,
    source_asset_key_for_image,
)
from trippostcollect.artifacts.image_materialization import (
    DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
    promote_validated_image,
    safe_platform_post_id,
    validate_image_file,
    write_staging_image,
)
from trippostcollect.artifacts.image_manifest import (
    ImageManifestEntry,
    ImageManifestError,
    manifest_sha256,
    parse_manifest,
    validate_post_manifest,
)
from trippostcollect.artifacts.image_persistence import (
    existing_image_records,
    normalize_persistence_items,
    prepare_image_rows,
    replace_image_rows,
)


HISTORICAL_PLATFORM_ORDER = ("xhs", "bilibili", "weibo", "zhihu", "douyin")
DISCOVERY_TABLES = (
    "crawl_discovery_checkpoints",
    "crawl_discovery_seen_candidates",
    "xhs_discovery_checkpoints",
    "xhs_discovery_seen_candidates",
)
HISTORICAL_IMAGE_EXCLUSION_TABLE = "historical_image_exclusions"


@dataclass(frozen=True, slots=True)
class HistoricalPostPlan:
    """One atomic post relationship replacement."""

    web_post_id: int
    platform_key: str
    platform_post_id: str
    current_content_rows: int
    authoritative_images: int
    misclassified_rows: int
    duplicate_variant_rows: int
    missing_authoritative_relations: int
    url_normalizations: int
    existing_local_rows: int
    preserved_local_rows: int
    prepared_images: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class HistoricalRelationshipPlan:
    """A deterministic, bounded set of post relationship replacements."""

    posts: tuple[HistoricalPostPlan, ...]
    source_digest: str
    plan_digest: str
    remaining_posts_by_platform: dict[str, int]
    skipped_incomplete_by_platform: dict[str, int]


def sha256_file(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _digest_rows(rows: Iterable[Sequence[Any]]) -> str:
    digest = sha256()
    for row in rows:
        digest.update(
            json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def table_digest(
    conn: sqlite3.Connection,
    table: str,
    *,
    excluded_columns: frozenset[str] = frozenset(),
) -> str:
    """Hash a SQLite table in physical row order using the H-00 row format."""

    if table not in {"web_posts", "web_post_images", *DISCOVERY_TABLES}:
        raise ValueError(f"table is outside historical invariant scope: {table}")
    columns = [
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})")
        if str(row[1]) not in excluded_columns
    ]
    if not columns:
        raise ValueError(f"table has no hashable columns: {table}")
    query = f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid"
    return _digest_rows(conn.execute(query))


def protected_database_digests(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return fields that historical image work must never change."""

    return {
        "web_posts_non_image_sha256": table_digest(
            conn,
            "web_posts",
            excluded_columns=frozenset({"post_images_count", "updated_at"}),
        ),
        "discovery_table_sha256": {
            table: table_digest(conn, table)
            for table in DISCOVERY_TABLES
        },
    }


def database_integrity(conn: sqlite3.Connection) -> dict[str, Any]:
    quick_check = conn.execute("PRAGMA quick_check").fetchone()
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    return {
        "quick_check": quick_check[0] if quick_check else None,
        "foreign_key_violations": len(violations),
    }


def sqlite_backup(source_path: str | Path, destination_path: str | Path) -> dict[str, Any]:
    """Create and independently verify a consistent SQLite backup."""

    source = Path(source_path).expanduser().resolve(strict=True)
    destination = Path(destination_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"backup destination already exists: {destination}")
    with sqlite3.connect(source) as source_conn, sqlite3.connect(destination) as backup_conn:
        source_conn.backup(backup_conn)
    with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as backup_conn:
        integrity = database_integrity(backup_conn)
    if integrity != {"quick_check": "ok", "foreign_key_violations": 0}:
        raise RuntimeError(f"backup integrity check failed: {integrity}")
    return {
        "path": str(destination),
        "sha256": sha256_file(destination),
        **integrity,
    }


def projection_inventory(
    conn: sqlite3.Connection,
    platforms: Sequence[str] = HISTORICAL_PLATFORM_ORDER,
) -> dict[str, dict[str, int]]:
    """Recompute current and authoritative content-image counts without writes."""

    result: dict[str, dict[str, int]] = {}
    exclusions = approved_historical_image_exclusions(conn)
    for platform_key in platforms:
        posts = authoritative = current = local = 0
        for web_post_id, platform_post_id, raw_sample_json in conn.execute(
            """
            SELECT id, platform_post_id, raw_sample_json
            FROM web_posts
            WHERE platform_key=?
            ORDER BY id
            """,
            (platform_key,),
        ):
            posts += 1
            excluded_keys = exclusions.get((platform_key, str(platform_post_id or "")), set())
            authoritative += sum(
                candidate.source_asset_key not in excluded_keys
                for candidate in content_image_candidates(
                    platform_key, _json_object(raw_sample_json)
                )
            )
            current_row = conn.execute(
                """
                SELECT
                  COUNT(*),
                  SUM(CASE WHEN local_path IS NOT NULL AND local_path<>'' THEN 1 ELSE 0 END)
                FROM web_post_images
                WHERE web_post_id=? AND image_role='content'
                """,
                (web_post_id,),
            ).fetchone()
            current += int(current_row[0] or 0)
            local += int(current_row[1] or 0)
        result[platform_key] = {
            "posts": posts,
            "current_content_rows": current,
            "authoritative_images": authoritative,
            "misclassified_rows": max(0, current - authoritative),
            "existing_local_rows": local,
            "local_gap": authoritative - local,
        }
    return result


def approved_historical_image_exclusions(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], set[str]]:
    """Return operator-approved historical source assets, if the schema exists."""

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (HISTORICAL_IMAGE_EXCLUSION_TABLE,),
    ).fetchone()
    if not exists:
        return {}
    result: dict[tuple[str, str], set[str]] = {}
    for platform_key, platform_post_id, source_asset_key in conn.execute(
        """
        SELECT platform_key, platform_post_id, source_asset_key
        FROM historical_image_exclusions
        ORDER BY platform_key, platform_post_id, source_index, id
        """
    ):
        result.setdefault(
            (str(platform_key), str(platform_post_id)), set()
        ).add(str(source_asset_key))
    return result


def assert_initial_inventory_matches_campaign(
    inventory: Mapping[str, Mapping[str, int]],
    campaign: Mapping[str, Any],
) -> None:
    """Stop before applying if the H-00 source counts no longer match."""

    expected = campaign.get("projection", {}).get("platforms", {})
    for platform_key in HISTORICAL_PLATFORM_ORDER:
        actual_row = inventory.get(platform_key, {})
        expected_row = expected.get(platform_key, {})
        for field in ("posts", "current_content_rows", "authoritative_images", "misclassified_rows"):
            if int(actual_row.get(field, -1)) != int(expected_row.get(field, -2)):
                raise ValueError(
                    f"H-00 inventory mismatch: platform={platform_key} field={field} "
                    f"actual={actual_row.get(field)!r} expected={expected_row.get(field)!r}"
                )


def _relationship_source_rows(
    conn: sqlite3.Connection,
    post_ids: Sequence[int],
) -> Iterable[Sequence[Any]]:
    for post_id in post_ids:
        post = conn.execute(
            """
            SELECT id, platform_key, platform_post_id, raw_sample_json, post_images_count
            FROM web_posts
            WHERE id=?
            """,
            (post_id,),
        ).fetchone()
        if post is None:
            yield (post_id, "missing")
            continue
        yield ("post", *post)
        yield from (
            ("image", *row)
            for row in conn.execute(
                """
                SELECT id, image_index, image_url, image_role, local_path,
                       width, height, mime_type, sha256, raw_image_json
                FROM web_post_images
                WHERE web_post_id=?
                ORDER BY image_role, image_index, id
                """,
                (post_id,),
            )
        )


def relationship_source_digest(conn: sqlite3.Connection, post_ids: Sequence[int]) -> str:
    return _digest_rows(_relationship_source_rows(conn, post_ids))


def _plan_digest(posts: Sequence[HistoricalPostPlan]) -> str:
    rows: list[Sequence[Any]] = []
    for post in posts:
        rows.append(
            (
                post.web_post_id,
                post.platform_key,
                post.platform_post_id,
                post.current_content_rows,
                post.authoritative_images,
                post.misclassified_rows,
                post.duplicate_variant_rows,
                post.missing_authoritative_relations,
                post.url_normalizations,
                post.preserved_local_rows,
            )
        )
        rows.extend(
            (
                post.web_post_id,
                item["source_index"],
                item["source_asset_key"],
                item["url"],
                item.get("local_path"),
                item.get("sha256"),
            )
            for item in post.prepared_images
        )
    return _digest_rows(rows)


def build_relationship_plan(
    conn: sqlite3.Connection,
    *,
    platforms: Sequence[str],
    after_post_ids: Mapping[str, int] | None = None,
    excluded_platform_post_ids: Mapping[str, Collection[str]] | None = None,
    batch_size: int = 10,
    project_root: str | Path,
    media_root: str | Path,
    require_complete_existing_local: bool = False,
    require_missing_local: bool = False,
) -> HistoricalRelationshipPlan:
    """Build a bounded plan using the production candidate and persistence components."""

    if require_complete_existing_local and require_missing_local:
        raise ValueError("existing-complete and missing-local filters are mutually exclusive")
    unknown = set(platforms) - set(HISTORICAL_PLATFORM_ORDER)
    if unknown:
        raise ValueError(f"unsupported historical platforms: {sorted(unknown)}")
    if batch_size < 0:
        raise ValueError("batch_size must be zero or positive")
    cursors = dict(after_post_ids or {})
    exclusions = {
        platform_key: {str(value) for value in values}
        for platform_key, values in (excluded_platform_post_ids or {}).items()
    }
    approved_exclusions = approved_historical_image_exclusions(conn)
    resolved_project_root = Path(project_root).expanduser().resolve(strict=True)
    resolved_media_root = Path(media_root).expanduser().resolve()
    selected: list[tuple[Any, ...]] = []
    remaining_by_platform: dict[str, int] = {}
    skipped_incomplete_by_platform: dict[str, int] = {}
    slots = batch_size
    for platform_key in HISTORICAL_PLATFORM_ORDER:
        if platform_key not in platforms:
            continue
        cursor = int(cursors.get(platform_key, 0))
        available = list(
            conn.execute(
                """
                SELECT id, platform_post_id, raw_sample_json
                FROM web_posts
                WHERE platform_key=? AND id>?
                ORDER BY id
                """,
                (platform_key, cursor),
            )
        )
        platform_exclusions = exclusions.get(platform_key, set())
        if platform_exclusions:
            available = [
                row for row in available if str(row[1] or "") not in platform_exclusions
            ]
        if require_complete_existing_local or require_missing_local:
            eligible: list[tuple[Any, ...]] = []
            skipped = 0
            for row in available:
                web_post_id, _platform_post_id, raw_sample_json = row
                source_exclusions = approved_exclusions.get(
                    (platform_key, str(_platform_post_id or "")), set()
                )
                authoritative_count = sum(
                    candidate.source_asset_key not in source_exclusions
                    for candidate in content_image_candidates(
                        platform_key, _json_object(raw_sample_json)
                    )
                )
                local_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM web_post_images
                        WHERE web_post_id=?
                          AND image_role='content'
                          AND local_path IS NOT NULL
                          AND local_path<>''
                        """,
                        (web_post_id,),
                    ).fetchone()[0]
                )
                complete_existing = authoritative_count > 0 and local_count == authoritative_count
                missing_local = authoritative_count > local_count
                if (
                    require_complete_existing_local
                    and complete_existing
                    or require_missing_local
                    and missing_local
                ):
                    eligible.append(row)
                else:
                    skipped += 1
            available = eligible
            skipped_incomplete_by_platform[platform_key] = skipped
        if batch_size == 0:
            take = len(available)
        else:
            take = min(len(available), max(0, slots))
            slots -= take
        selected.extend((platform_key, *row) for row in available[:take])
        remaining_by_platform[platform_key] = len(available) - take

    posts: list[HistoricalPostPlan] = []
    for platform_key, web_post_id, platform_post_id, raw_sample_json in selected:
        source_exclusions = approved_exclusions.get(
            (platform_key, str(platform_post_id or "")), set()
        )
        candidates = tuple(
            candidate
            for candidate in content_image_candidates(
                platform_key, _json_object(raw_sample_json)
            )
            if candidate.source_asset_key not in source_exclusions
        )
        image_items = normalize_persistence_items(
            [candidate.as_image_item() for candidate in candidates]
        )
        existing = existing_image_records(conn, int(web_post_id))
        current_content = [item for item in existing if item["role"] == "content"]
        authoritative_by_key = {
            candidate.source_asset_key: candidate
            for candidate in candidates
        }
        authoritative_match_keys = dict(authoritative_by_key)
        for candidate in candidates:
            authoritative_match_keys.setdefault(
                source_asset_key_for_image(platform_key, candidate.source_url),
                candidate,
            )
        current_by_key: dict[str, list[dict[str, Any]]] = {}
        for item in current_content:
            persisted_key = str(item.get("source_asset_key") or "")
            candidate = authoritative_match_keys.get(persisted_key)
            if candidate is None:
                try:
                    fallback_key = source_asset_key_for_image(platform_key, str(item["url"]))
                except ValueError:
                    fallback_key = f"invalid:{item['url']}"
                candidate = authoritative_match_keys.get(fallback_key)
            asset_key = candidate.source_asset_key if candidate is not None else fallback_key
            current_by_key.setdefault(asset_key, []).append(item)
        misclassified = sum(
            len(items)
            for asset_key, items in current_by_key.items()
            if asset_key not in authoritative_by_key
        )
        duplicate_variants = sum(
            max(0, len(items) - 1)
            for asset_key, items in current_by_key.items()
            if asset_key in authoritative_by_key
        )
        missing_authoritative = sum(
            asset_key not in current_by_key
            for asset_key in authoritative_by_key
        )
        url_normalizations = sum(
            bool(current_by_key.get(asset_key))
            and str(current_by_key[asset_key][0]["url"]) != candidate.source_url
            for asset_key, candidate in authoritative_by_key.items()
        )
        existing_local = sum(bool(item.get("local_path")) for item in current_content)
        prepared = prepare_image_rows(
            platform_key,
            image_items,
            existing,
            project_root=resolved_project_root,
            media_root=resolved_media_root,
            require_local_images=False,
        )
        preserved_local = sum(bool(item.get("local_path")) for item in prepared)
        if preserved_local < existing_local:
            raise ValueError(
                f"verified local relationship would be lost: platform={platform_key} "
                f"post_id={platform_post_id} existing={existing_local} preserved={preserved_local}"
            )
        posts.append(
            HistoricalPostPlan(
                web_post_id=int(web_post_id),
                platform_key=platform_key,
                platform_post_id=str(platform_post_id or ""),
                current_content_rows=len(current_content),
                authoritative_images=len(candidates),
                misclassified_rows=misclassified,
                duplicate_variant_rows=duplicate_variants,
                missing_authoritative_relations=missing_authoritative,
                url_normalizations=url_normalizations,
                existing_local_rows=existing_local,
                preserved_local_rows=preserved_local,
                prepared_images=tuple(prepared),
            )
        )
    post_ids = [post.web_post_id for post in posts]
    return HistoricalRelationshipPlan(
        posts=tuple(posts),
        source_digest=relationship_source_digest(conn, post_ids),
        plan_digest=_plan_digest(posts),
        remaining_posts_by_platform=remaining_by_platform,
        skipped_incomplete_by_platform=skipped_incomplete_by_platform,
    )


def promote_existing_plan(
    plan: HistoricalRelationshipPlan,
    *,
    project_root: str | Path,
    media_root: str | Path,
    staging_root: str | Path,
) -> tuple[HistoricalRelationshipPlan, dict[str, int]]:
    """Promote a complete-existing batch through the production validator and promoter."""

    resolved_project_root = Path(project_root).expanduser().resolve(strict=True)
    resolved_media_root = Path(media_root).expanduser().resolve()
    resolved_staging_root = Path(staging_root).expanduser().resolve()
    promoted_posts: list[HistoricalPostPlan] = []
    promoted_images = reused_images = promoted_bytes = 0
    for post in plan.posts:
        if post.authoritative_images <= 0 or post.preserved_local_rows != post.authoritative_images:
            raise ValueError(
                f"existing promotion requires a complete post: {post.platform_key}:{post.platform_post_id}"
            )
        image_items: list[dict[str, Any]] = []
        for item in post.prepared_images:
            local_path = str(item.get("local_path") or "")
            local_file = Path(local_path).expanduser()
            if not local_file.is_absolute():
                local_file = resolved_project_root / local_file
            validated = validate_image_file(
                local_file,
                allowed_root=resolved_project_root,
                expected_sha256=str(item.get("sha256") or ""),
                require_suffix_match=False,
            )
            with validated.path.open("rb") as source_handle:
                staged = write_staging_image(
                    iter(lambda: source_handle.read(1024 * 1024), b""),
                    staging_root=resolved_staging_root,
                    relative_stem=(
                        f"{post.platform_key}/{safe_platform_post_id(post.platform_post_id)}/"
                        f"{int(item['source_index']):03d}-{validated.sha256[:16]}"
                    ),
                    content_type=validated.mime_type,
                    content_length=validated.size_bytes,
                    source_url=str(item["url"]),
                )
            candidate = ImageCandidate(
                platform_key=post.platform_key,
                platform_post_id=post.platform_post_id,
                image_role=str(item["role"]),
                source_index=int(item["source_index"]),
                source_url=str(item["url"]),
                source_key=str(item["source_key"]),
                source_asset_key=str(item["source_asset_key"]),
            )
            promoted = promote_validated_image(
                staged,
                candidate,
                staging_root=resolved_staging_root,
                media_root=resolved_media_root,
                project_root=resolved_project_root,
            )
            promoted_images += int(not promoted.reused)
            reused_images += int(promoted.reused)
            promoted_bytes += promoted.size_bytes
            image_items.append(
                {
                    **item,
                    "local_path": promoted.local_path,
                    "width": promoted.width,
                    "height": promoted.height,
                    "mime_type": promoted.mime_type,
                    "sha256": promoted.sha256,
                    "local_file": {
                        "source": "historical_existing_promotion_v1",
                        "source_path": local_path,
                        "source_url": promoted.source_url,
                        "size_bytes": promoted.size_bytes,
                    },
                }
            )
        prepared = prepare_image_rows(
            post.platform_key,
            normalize_persistence_items(image_items),
            [],
            project_root=resolved_project_root,
            media_root=resolved_media_root,
            require_local_images=True,
        )
        promoted_posts.append(
            HistoricalPostPlan(
                web_post_id=post.web_post_id,
                platform_key=post.platform_key,
                platform_post_id=post.platform_post_id,
                current_content_rows=post.current_content_rows,
                authoritative_images=post.authoritative_images,
                misclassified_rows=post.misclassified_rows,
                duplicate_variant_rows=post.duplicate_variant_rows,
                missing_authoritative_relations=post.missing_authoritative_relations,
                url_normalizations=post.url_normalizations,
                existing_local_rows=post.existing_local_rows,
                preserved_local_rows=len(prepared),
                prepared_images=tuple(prepared),
            )
        )
    promoted_plan = HistoricalRelationshipPlan(
        posts=tuple(promoted_posts),
        source_digest=plan.source_digest,
        plan_digest=_plan_digest(promoted_posts),
        remaining_posts_by_platform=plan.remaining_posts_by_platform,
        skipped_incomplete_by_platform=plan.skipped_incomplete_by_platform,
    )
    return promoted_plan, {
        "promoted_images": promoted_images,
        "reused_images": reused_images,
        "promoted_bytes": promoted_bytes,
    }


def _manifest_staging_root(manifest_path: Path, entry: ImageManifestEntry) -> Path:
    staging_path = Path(str(entry.staging_path))
    if staging_path.parts and staging_path.parts[0] == manifest_path.parent.name:
        return manifest_path.parent.parent
    return manifest_path.parent


def _validate_xhs_detail_index_manifest(
    entries: Sequence[ImageManifestEntry],
    candidates: Sequence[ImageCandidate],
) -> tuple[ImageManifestEntry, ...]:
    """Allow current signed-detail CDN variants only at the same post/index."""

    if len(entries) != len(candidates):
        raise ImageManifestError(
            "image_manifest_count_mismatch",
            f"manifest rows={len(entries)} expected={len(candidates)}",
        )
    indexed = {(entry.image_role, entry.source_index): entry for entry in entries}
    if len(indexed) != len(entries):
        raise ImageManifestError(
            "image_manifest_identity_mismatch", "duplicate XHS detail image index"
        )
    ordered: list[ImageManifestEntry] = []
    for candidate in candidates:
        entry = indexed.get((candidate.image_role, candidate.source_index))
        if entry is None or any(
            (
                candidate.platform_key != "xhs",
                entry.platform_key != candidate.platform_key,
                entry.platform_post_id != candidate.platform_post_id,
                entry.source_key != candidate.source_key,
                entry.fetch_status != "downloaded",
            )
        ):
            raise ImageManifestError(
                "image_manifest_identity_mismatch",
                f"XHS detail manifest does not match post/index {candidate.source_index}",
            )
        source_hosts = {
            str(urlsplit(value).hostname or "").lower()
            for value in (candidate.source_url, entry.source_url)
        }
        if not source_hosts or any(
            host != "xhscdn.com" and not host.endswith(".xhscdn.com")
            for host in source_hosts
        ):
            raise ImageManifestError(
                "image_manifest_identity_mismatch",
                "XHS detail index fallback requires trusted XHS CDN URLs",
            )
        ordered.append(entry)
    return tuple(ordered)


def promote_downloaded_plan(
    plan: HistoricalRelationshipPlan,
    *,
    manifest_paths: Sequence[str | Path],
    project_root: str | Path,
    media_root: str | Path,
    allow_xhs_detail_index_match: bool = False,
) -> tuple[HistoricalRelationshipPlan, dict[str, Any]]:
    """Validate an exact crawler manifest batch and promote it into a historical plan."""

    if not plan.posts:
        raise ValueError("download promotion requires at least one planned post")
    resolved_project_root = Path(project_root).expanduser().resolve(strict=True)
    resolved_media_root = Path(media_root).expanduser().resolve()
    entries_by_post: dict[tuple[str, str], list[tuple[ImageManifestEntry, Path]]] = {}
    manifest_evidence: list[dict[str, Any]] = []
    for path_value in dict.fromkeys(str(value) for value in manifest_paths):
        manifest_path = Path(path_value).expanduser().resolve(strict=True)
        if (
            manifest_path != resolved_project_root
            and resolved_project_root not in manifest_path.parents
        ):
            raise ValueError(f"image manifest escapes project root: {manifest_path}")
        entries = parse_manifest(manifest_path.read_bytes())
        manifest_evidence.append(
            {
                "path": manifest_path.relative_to(resolved_project_root).as_posix(),
                "sha256": manifest_sha256(entries),
                "rows": len(entries),
            }
        )
        for entry in entries:
            entries_by_post.setdefault(
                (entry.platform_key, entry.platform_post_id), []
            ).append((entry, manifest_path))

    expected_post_keys = {
        (post.platform_key, post.platform_post_id) for post in plan.posts
    }
    if set(entries_by_post) != expected_post_keys:
        raise ValueError(
            "image manifest post identities do not exactly match the historical plan"
        )

    promoted_posts: list[HistoricalPostPlan] = []
    promoted_images = reused_images = promoted_bytes = 0
    strict_identity_images = detail_index_identity_images = 0
    stable_asset_key_matches = source_url_matches = 0
    for post in plan.posts:
        candidates = [
            ImageCandidate(
                platform_key=post.platform_key,
                platform_post_id=post.platform_post_id,
                image_role=str(item["role"]),
                source_index=int(item["source_index"]),
                source_url=str(item["url"]),
                source_key=str(item["source_key"]),
                source_asset_key=str(item["source_asset_key"]),
            )
            for item in post.prepared_images
        ]
        rows = entries_by_post[(post.platform_key, post.platform_post_id)]
        manifest_entries = [entry for entry, _path in rows]
        identity_match_mode = "strict_manifest_v1"
        try:
            ordered_entries = validate_post_manifest(
                manifest_entries,
                candidates,
                require_downloaded=True,
            )
            strict_identity_images += len(ordered_entries)
        except ImageManifestError:
            if not allow_xhs_detail_index_match or post.platform_key != "xhs":
                raise
            ordered_entries = _validate_xhs_detail_index_manifest(
                manifest_entries,
                candidates,
            )
            identity_match_mode = "historical_xhs_detail_post_index_v1"
            detail_index_identity_images += len(ordered_entries)
        manifest_path_by_index = {
            entry.source_index: manifest_path for entry, manifest_path in rows
        }
        promoted_items: list[dict[str, Any]] = []
        for item, candidate, entry in zip(
            post.prepared_images,
            candidates,
            ordered_entries,
            strict=True,
        ):
            stable_asset_key_matches += int(
                entry.source_asset_key == candidate.source_asset_key
            )
            source_url_matches += int(entry.source_url == candidate.source_url)
            manifest_path = manifest_path_by_index[entry.source_index]
            staging_root = _manifest_staging_root(manifest_path, entry)
            staged = validate_image_file(
                staging_root / str(entry.staging_path),
                allowed_root=staging_root,
                expected_sha256=str(entry.sha256),
                max_bytes=DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
                require_suffix_match=True,
            )
            if any(
                (
                    staged.size_bytes != entry.size_bytes,
                    staged.mime_type != entry.mime_type,
                    staged.width != entry.width,
                    staged.height != entry.height,
                )
            ):
                raise ValueError(
                    f"staging metadata differs from manifest: {post.platform_key}:{post.platform_post_id}"
                )
            promoted = promote_validated_image(
                staged,
                candidate,
                staging_root=staging_root,
                media_root=resolved_media_root,
                project_root=resolved_project_root,
            )
            promoted_images += int(not promoted.reused)
            reused_images += int(promoted.reused)
            promoted_bytes += promoted.size_bytes
            promoted_items.append(
                {
                    **item,
                    "local_path": promoted.local_path,
                    "width": promoted.width,
                    "height": promoted.height,
                    "mime_type": promoted.mime_type,
                    "sha256": promoted.sha256,
                    "local_file": {
                        "source": "historical_crawler_download_v1",
                        "source_url": promoted.source_url,
                        "download_source_url": entry.source_url,
                        "download_source_asset_key": entry.source_asset_key,
                        "identity_match_mode": identity_match_mode,
                        "size_bytes": promoted.size_bytes,
                        "manifest_path": manifest_path.relative_to(
                            resolved_project_root
                        ).as_posix(),
                    },
                }
            )
        prepared = prepare_image_rows(
            post.platform_key,
            normalize_persistence_items(promoted_items),
            [],
            project_root=resolved_project_root,
            media_root=resolved_media_root,
            require_local_images=True,
        )
        promoted_posts.append(
            HistoricalPostPlan(
                web_post_id=post.web_post_id,
                platform_key=post.platform_key,
                platform_post_id=post.platform_post_id,
                current_content_rows=post.current_content_rows,
                authoritative_images=post.authoritative_images,
                misclassified_rows=post.misclassified_rows,
                duplicate_variant_rows=post.duplicate_variant_rows,
                missing_authoritative_relations=post.missing_authoritative_relations,
                url_normalizations=post.url_normalizations,
                existing_local_rows=post.existing_local_rows,
                preserved_local_rows=len(prepared),
                prepared_images=tuple(prepared),
            )
        )
    promoted_plan = HistoricalRelationshipPlan(
        posts=tuple(promoted_posts),
        source_digest=plan.source_digest,
        plan_digest=_plan_digest(promoted_posts),
        remaining_posts_by_platform=plan.remaining_posts_by_platform,
        skipped_incomplete_by_platform=plan.skipped_incomplete_by_platform,
    )
    return promoted_plan, {
        "promoted_images": promoted_images,
        "reused_images": reused_images,
        "promoted_bytes": promoted_bytes,
        "strict_identity_images": strict_identity_images,
        "detail_index_identity_images": detail_index_identity_images,
        "stable_asset_key_matches": stable_asset_key_matches,
        "source_url_matches": source_url_matches,
        "manifest_evidence": manifest_evidence,
    }


def apply_relationship_plan(
    conn: sqlite3.Connection,
    plan: HistoricalRelationshipPlan,
) -> dict[str, int]:
    """Atomically replace content roles while leaving all non-image post fields intact."""

    post_ids = [post.web_post_id for post in plan.posts]
    if relationship_source_digest(conn, post_ids) != plan.source_digest:
        raise RuntimeError("historical relationship source changed after planning")
    replaced = removed = local_preserved = post_counts_updated = 0
    for post in plan.posts:
        replaced += replace_image_rows(
            conn,
            post.web_post_id,
            post.prepared_images,
            roles=frozenset({"content"}),
        )
        removed += post.misclassified_rows + post.duplicate_variant_rows
        local_preserved += post.preserved_local_rows
        cursor = conn.execute(
            """
            UPDATE web_posts
            SET post_images_count=?
            WHERE id=? AND post_images_count<>?
            """,
            (post.authoritative_images, post.web_post_id, post.authoritative_images),
        )
        post_counts_updated += int(cursor.rowcount or 0)
    return {
        "processed_posts": len(plan.posts),
        "replaced_content_rows": replaced,
        "removed_misclassified_rows": removed,
        "preserved_local_rows": local_preserved,
        "post_images_count_updates": post_counts_updated,
    }


def summarize_plan(plan: HistoricalRelationshipPlan) -> dict[str, Any]:
    platforms: dict[str, dict[str, int]] = {}
    for post in plan.posts:
        row = platforms.setdefault(
            post.platform_key,
            {
                "posts": 0,
                "current_content_rows": 0,
                "authoritative_images": 0,
                "misclassified_rows": 0,
                "duplicate_variant_rows": 0,
                "missing_authoritative_relations": 0,
                "url_normalizations": 0,
                "preserved_local_rows": 0,
            },
        )
        row["posts"] += 1
        row["current_content_rows"] += post.current_content_rows
        row["authoritative_images"] += post.authoritative_images
        row["misclassified_rows"] += post.misclassified_rows
        row["duplicate_variant_rows"] += post.duplicate_variant_rows
        row["missing_authoritative_relations"] += post.missing_authoritative_relations
        row["url_normalizations"] += post.url_normalizations
        row["preserved_local_rows"] += post.preserved_local_rows
    return {
        "planned_posts": len(plan.posts),
        "planned_images": sum(post.authoritative_images for post in plan.posts),
        "preserved_local_rows": sum(post.preserved_local_rows for post in plan.posts),
        "misclassified_rows_to_remove": sum(
            post.misclassified_rows
            for post in plan.posts
        ),
        "duplicate_variant_rows_to_merge": sum(
            post.duplicate_variant_rows for post in plan.posts
        ),
        "missing_authoritative_relations_to_add": sum(
            post.missing_authoritative_relations for post in plan.posts
        ),
        "url_normalizations": sum(post.url_normalizations for post in plan.posts),
        "source_digest": plan.source_digest,
        "plan_digest": plan.plan_digest,
        "remaining_posts_by_platform": plan.remaining_posts_by_platform,
        "skipped_incomplete_by_platform": plan.skipped_incomplete_by_platform,
        "platforms": platforms,
    }
