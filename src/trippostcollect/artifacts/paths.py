"""Safe artifact path resolution."""

from __future__ import annotations

from pathlib import Path

from trippostcollect.core.paths import PROJECT_ROOT


class UnsafeArtifactPath(ValueError):
    """Raised when a stored artifact path escapes the project boundary."""


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
