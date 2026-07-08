"""Evidence artifact lookup by capture and fixed kind."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trippostcollect.artifacts.paths import require_existing_project_file


ARTIFACT_KINDS = {
    "screenshot": "screenshot_path",
    "visible_text": "visible_text_path",
    "rendered_html": "rendered_html_path",
}


@dataclass(frozen=True)
class EvidenceArtifact:
    kind: str
    path: Path
    media_type: str
    as_attachment: bool = False


def artifact_for_capture(capture: dict[str, Any], kind: str) -> EvidenceArtifact:
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"unsupported artifact kind: {kind}")
    column = ARTIFACT_KINDS[kind]
    path = require_existing_project_file(capture.get(column))
    if kind == "screenshot":
        return EvidenceArtifact(kind=kind, path=path, media_type="image/png")
    return EvidenceArtifact(kind=kind, path=path, media_type="text/plain; charset=utf-8")
