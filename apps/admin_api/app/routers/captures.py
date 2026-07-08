"""Capture evidence routes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

from apps.admin_api.app.deps import get_db
from apps.admin_api.app.response import ok
from trippostcollect.artifacts.evidence_reader import artifact_for_capture
from trippostcollect.artifacts.image_proxy import (
    UnsafeImageUrl,
    content_type_for_path,
    local_image_file,
    validate_remote_image_url,
)
from trippostcollect.artifacts.paths import UnsafeArtifactPath
from trippostcollect.artifacts.repository import CaptureFilters, CaptureRepository


router = APIRouter(prefix="/api/captures", tags=["captures"])


def capture_repo(conn: sqlite3.Connection = Depends(get_db)) -> CaptureRepository:
    return CaptureRepository(conn)


@router.get("")
def list_captures(
    site_key: str | None = None,
    capture_kind: str | None = None,
    ok_filter: bool | None = Query(default=None, alias="ok"),
    content_ready: bool | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort: str = "-captured_at",
    repo: CaptureRepository = Depends(capture_repo),
) -> dict:
    rows, meta = repo.list_captures(
        CaptureFilters(
            site_key=site_key,
            capture_kind=capture_kind,
            ok=ok_filter,
            content_ready=content_ready,
            q=q,
        ),
        page=page,
        page_size=page_size,
        sort=sort,
    )
    return ok(rows, meta)


@router.get("/{capture_id}")
def get_capture(capture_id: int, repo: CaptureRepository = Depends(capture_repo)) -> dict:
    capture = repo.get_capture(capture_id)
    if capture is None:
        raise HTTPException(status_code=404, detail="capture not found")
    return ok(capture)


@router.get("/{capture_id}/images")
def list_capture_images(capture_id: int, repo: CaptureRepository = Depends(capture_repo)) -> dict:
    if repo.get_capture(capture_id) is None:
        raise HTTPException(status_code=404, detail="capture not found")
    return ok(repo.list_capture_images(capture_id))


@router.get("/images/{image_id}/preview")
def preview_capture_image(image_id: int, repo: CaptureRepository = Depends(capture_repo)):
    image = repo.get_capture_image(image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="capture image not found")
    saved_path = image.get("saved_path")
    if saved_path:
        try:
            path = local_image_file(saved_path)
        except UnsafeArtifactPath as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError:
            path = None
        if path is not None:
            media_type = image.get("content_type") or content_type_for_path(path)
            return FileResponse(path, media_type=media_type)
    try:
        remote_url = validate_remote_image_url(image.get("image_url"))
    except UnsafeImageUrl as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return RedirectResponse(remote_url, status_code=307)


@router.get("/{capture_id}/artifact")
def get_capture_artifact(
    capture_id: int,
    kind: str = Query(pattern="^(screenshot|visible_text|rendered_html)$"),
    repo: CaptureRepository = Depends(capture_repo),
):
    capture = repo.get_capture(capture_id)
    if capture is None:
        raise HTTPException(status_code=404, detail="capture not found")
    try:
        artifact = artifact_for_capture(capture, kind)
    except UnsafeArtifactPath as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="artifact file not found") from exc
    if kind == "screenshot":
        return FileResponse(artifact.path, media_type=artifact.media_type)
    return PlainTextResponse(artifact.path.read_text(encoding="utf-8", errors="replace"), media_type=artifact.media_type)
