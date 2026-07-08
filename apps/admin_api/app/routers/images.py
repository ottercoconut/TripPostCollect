"""Record image routes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from apps.admin_api.app.deps import get_db
from apps.admin_api.app.response import ok
from trippostcollect.artifacts.image_proxy import (
    UnsafeImageUrl,
    content_type_for_path,
    local_image_file,
    validate_remote_image_url,
)
from trippostcollect.artifacts.paths import UnsafeArtifactPath
from trippostcollect.records.repository import RecordRepository


router = APIRouter(prefix="/api", tags=["images"])


@router.get("/records/{record_id}/images")
def list_record_images(record_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    repo = RecordRepository(conn)
    if repo.get_record(record_id) is None:
        raise HTTPException(status_code=404, detail="record not found")
    return ok(repo.list_record_images(record_id))


@router.get("/images/{image_id}/preview")
def preview_record_image(image_id: int, conn: sqlite3.Connection = Depends(get_db)):
    repo = RecordRepository(conn)
    image = repo.get_record_image(image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="image not found")
    local_path = image.get("local_path")
    if local_path:
        try:
            path = local_image_file(local_path)
        except UnsafeArtifactPath as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError:
            path = None
        if path is not None:
            media_type = image.get("mime_type") or content_type_for_path(path)
            return FileResponse(path, media_type=media_type)
    try:
        remote_url = validate_remote_image_url(image.get("image_url"))
    except UnsafeImageUrl as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return RedirectResponse(remote_url, status_code=307)
