"""Independent runner gates for formal local-image artifacts and persistence."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import sqlite3
from typing import Any

from trippostcollect.artifacts.image_materialization import (
    ImageMaterializationError,
    validate_image_file,
)
from trippostcollect.core.paths import LOCAL_MEDIA_ROOT, PROJECT_ROOT


def _controlled_relative_file(path_value: Any, project_root: Path) -> Path:
    text = str(path_value or "")
    relative = PurePosixPath(text)
    if (
        not text
        or relative.is_absolute()
        or "\\" in text
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("artifact path must be a safe project-relative POSIX path")
    path = project_root.joinpath(*relative.parts).resolve(strict=True)
    if path != project_root and project_root not in path.parents:
        raise ValueError("artifact path escapes project root")
    if not path.is_file():
        raise ValueError("artifact path is not a file")
    return path


def verify_image_artifacts(
    summary: dict[str, Any],
    *,
    project_root: str | Path = PROJECT_ROOT,
    expect_promotion: bool,
) -> dict[str, Any]:
    """Re-hash manifest evidence and enforce the formal image completion contract."""

    root = Path(project_root).expanduser().resolve(strict=True)
    image = summary.get("image_materialization")
    evidence: dict[str, Any] = {
        "ok": False,
        "required": False,
        "complete": False,
        "promotion_required": expect_promotion,
        "candidate_posts": 0,
        "expected_images": 0,
        "validated_images": 0,
        "verified_manifests": 0,
        "reason": "image_materialization_missing",
    }
    if not isinstance(image, dict):
        return evidence
    try:
        required = image.get("required") is True
        complete = image.get("complete") is True
        promotion_required = image.get("promotion_required") is True
        candidate_posts = int(image.get("candidate_posts") or 0)
        complete_posts = int(image.get("complete_posts") or 0)
        expected_images = int(image.get("expected_images") or 0)
        downloaded_images = int(image.get("downloaded_images") or 0)
        validated_images = int(image.get("validated_images") or 0)
        promoted_images = int(image.get("promoted_images") or 0)
        reused_images = int(image.get("reused_images") or 0)
        retryable_failures = int(image.get("retryable_failures") or 0)
        terminal_failures = int(image.get("terminal_failures") or 0)
        manifest_items = image.get("manifest_evidence")
        manifest_paths = image.get("manifest_paths")
        if not isinstance(manifest_items, list) or not isinstance(manifest_paths, list):
            raise ValueError("manifest evidence must be lists")
        if any(not isinstance(item, dict) for item in manifest_items):
            raise ValueError("manifest evidence row must be an object")

        verified_items: list[dict[str, str]] = []
        for item in manifest_items:
            path_value = str(item.get("path") or "")
            expected_sha256 = str(item.get("sha256") or "")
            path = _controlled_relative_file(path_value, root)
            actual_sha256 = sha256(path.read_bytes()).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError(f"manifest SHA-256 mismatch: {path_value}")
            verified_items.append({"path": path_value, "sha256": actual_sha256})
        verified_items.sort(key=lambda item: item["path"])
        if manifest_paths != [item["path"] for item in verified_items]:
            raise ValueError("manifest path list does not match hashed evidence")
        aggregate_sha256 = sha256(
            json.dumps(
                verified_items,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if aggregate_sha256 != str(image.get("manifest_sha256") or ""):
            raise ValueError("aggregate manifest SHA-256 mismatch")

        if not required:
            raise ValueError("local image storage is not marked required")
        if not complete:
            raise ValueError("local image materialization is incomplete")
        if promotion_required != expect_promotion:
            raise ValueError("image promotion mode does not match runner mode")
        if candidate_posts != complete_posts:
            raise ValueError("complete post count does not match candidates")
        if expected_images != downloaded_images or expected_images != validated_images:
            raise ValueError("downloaded/validated image count does not match candidates")
        if retryable_failures or terminal_failures or image.get("failures"):
            raise ValueError("image materialization contains failures")
        if expected_images > 0 and not verified_items:
            raise ValueError("non-empty image set has no manifest evidence")
        if expect_promotion and promoted_images + reused_images != expected_images:
            raise ValueError("promoted/reused image count does not match candidates")
    except (OSError, TypeError, ValueError) as exc:
        evidence["reason"] = str(exc)
        return evidence

    evidence.update(
        {
            "ok": True,
            "required": True,
            "complete": True,
            "promotion_required": promotion_required,
            "candidate_posts": candidate_posts,
            "expected_images": expected_images,
            "validated_images": validated_images,
            "verified_manifests": len(verified_items),
            "manifest_sha256": aggregate_sha256,
            "reason": "",
        }
    )
    return evidence


def _identity_query(identity: str) -> tuple[str, tuple[str, str]]:
    if ":id:" in identity:
        platform_key, platform_post_id = identity.split(":id:", 1)
        return (
            "SELECT id, post_images_count FROM web_posts "
            "WHERE platform_key=? AND platform_post_id=?",
            (platform_key, platform_post_id),
        )
    if ":url:" in identity:
        platform_key, canonical_url = identity.split(":url:", 1)
        return (
            "SELECT id, post_images_count FROM web_posts "
            "WHERE platform_key=? AND canonical_url=?",
            (platform_key, canonical_url),
        )
    raise ValueError(f"unsupported formal identity: {identity}")


def verify_image_persistence(
    summary: dict[str, Any],
    db_path: str | Path,
    *,
    project_root: str | Path = PROJECT_ROOT,
    media_root: str | Path = LOCAL_MEDIA_ROOT,
) -> dict[str, Any]:
    """Verify every formally selected post/image relation against immutable bytes."""

    root = Path(project_root).expanduser().resolve(strict=True)
    media = Path(media_root).expanduser().resolve()
    evidence: dict[str, Any] = {
        "ok": False,
        "checked_posts": 0,
        "checked_images": 0,
        "reason": "formal_image_persistence_incomplete",
    }
    if media != root and root not in media.parents:
        evidence["reason"] = "media root escapes project root"
        return evidence
    artifact_evidence = verify_image_artifacts(
        summary,
        project_root=root,
        expect_promotion=True,
    )
    if not artifact_evidence["ok"]:
        evidence["reason"] = str(artifact_evidence["reason"])
        return evidence
    if int(artifact_evidence["expected_images"]) > 0 and not media.is_dir():
        evidence["reason"] = "formal media root is missing"
        return evidence
    if summary.get("import_completion_met") is not True:
        evidence["reason"] = "formal import completion is false"
        return evidence
    validation = summary.get("formal_validation")
    if not isinstance(validation, dict):
        evidence["reason"] = "formal validation is missing"
        return evidence
    identities = list(validation.get("new_identities") or []) + list(
        validation.get("existing_identities") or []
    )
    if len(identities) != len(set(identities)):
        evidence["reason"] = "formal validation contains duplicate identities"
        return evidence
    if int(validation.get("valid_total_count") or 0) != len(identities):
        evidence["reason"] = "formal identity count does not match valid records"
        return evidence

    checked_images = 0
    try:
        with sqlite3.connect(Path(db_path).expanduser()) as conn:
            for identity in identities:
                query, params = _identity_query(str(identity))
                rows = conn.execute(query, params).fetchall()
                if len(rows) != 1:
                    raise ValueError(f"formal post identity is absent or ambiguous: {identity}")
                post_id, post_images_count = int(rows[0][0]), int(rows[0][1] or 0)
                images = conn.execute(
                    """
                    SELECT image_index, local_path, width, height, mime_type, sha256
                    FROM web_post_images
                    WHERE web_post_id=? AND image_role='content'
                    ORDER BY image_index
                    """,
                    (post_id,),
                ).fetchall()
                if post_images_count != len(images):
                    raise ValueError(f"post image count mismatch: {identity}")
                if [int(row[0]) for row in images] != list(range(len(images))):
                    raise ValueError(f"post image indices are not continuous: {identity}")
                for _, local_path, width, height, mime_type, expected_sha256 in images:
                    path_value = str(local_path or "")
                    if Path(path_value).is_absolute():
                        raise ValueError(f"formal local path must be project-relative: {identity}")
                    image_path = root / path_value
                    verified = validate_image_file(
                        image_path,
                        allowed_root=media,
                        expected_sha256=str(expected_sha256 or ""),
                        require_suffix_match=True,
                    )
                    if (
                        verified.width != int(width or 0)
                        or verified.height != int(height or 0)
                        or verified.mime_type != str(mime_type or "")
                    ):
                        raise ValueError(f"database image metadata mismatch: {identity}")
                    checked_images += 1
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite quick_check failed")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("SQLite foreign_key_check failed")
    except (ImageMaterializationError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        evidence["reason"] = str(exc)
        return evidence

    if checked_images != int(artifact_evidence["expected_images"]):
        evidence["reason"] = "persisted image count does not match materialization"
        return evidence
    evidence.update(
        {
            "ok": True,
            "checked_posts": len(identities),
            "checked_images": checked_images,
            "expected_images": int(artifact_evidence["expected_images"]),
            "quick_check": "ok",
            "foreign_key_check": "ok",
            "reason": "",
        }
    )
    return evidence
