"""Safe artifact path resolution."""

from __future__ import annotations

from trippostcollect.application.contracts import ImagePersistenceError
from trippostcollect.core.paths import LOCAL_MEDIA_ROOT

from pathlib import Path

from trippostcollect.core.paths import PROJECT_ROOT


class UnsafeArtifactPath(ValueError):
    pass


def resolve_project_path(value: str | Path | None) -> Path:
    if value is None or str(value).strip() == "":
        raise UnsafeArtifactPath("empty artifact path")
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else PROJECT_ROOT / raw
    resolved = candidate.resolve(strict=False)
    root = PROJECT_ROOT.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise UnsafeArtifactPath("artifact path escapes project root")
    return resolved


def require_existing_project_file(value: str | Path | None) -> Path:
    resolved = resolve_project_path(value)
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(str(resolved))
    return resolved


def resolve_media_root(
    value: str | Path,
    *,
    project_root: str | Path = PROJECT_ROOT,
    default_media_root: str | Path = LOCAL_MEDIA_ROOT,
) -> Path:
    root = Path(project_root).expanduser().resolve(strict=True)
    media_root = Path(value).expanduser().resolve()
    formal_root = Path(default_media_root).expanduser().resolve()
    temp_root = (root / "temp").resolve()
    if media_root != formal_root and media_root != temp_root and temp_root not in media_root.parents:
        raise ImagePersistenceError(
            "--media-root must be LOCAL_MEDIA_ROOT or a directory below project temp/"
        )
    if media_root != root and root not in media_root.parents:
        raise ImagePersistenceError("media root escapes project root")
    return media_root
