"""Record workbench routes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from apps.admin_api.app.deps import get_db
from apps.admin_api.app.response import ok
from trippostcollect.records.repository import RecordFilters, RecordRepository
from trippostcollect.records.service import RecordService


router = APIRouter(prefix="/api/records", tags=["records"])


def record_service(conn: sqlite3.Connection = Depends(get_db)) -> RecordService:
    return RecordService(RecordRepository(conn))


@router.get("")
def list_records(
    platform_key: str | None = None,
    city_name: str | None = None,
    source_type: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    published_from: str | None = None,
    published_to: str | None = None,
    captured_from: str | None = None,
    captured_to: str | None = None,
    has_images: bool | None = None,
    missing_published_at: bool | None = None,
    missing_author_followers: bool | None = None,
    missing_field: str | None = Query(default=None, pattern="^(images|published_at|author_followers)$"),
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort: str = "-captured_at",
    service: RecordService = Depends(record_service),
) -> dict:
    filters = RecordFilters(
        platform_key=platform_key,
        city_name=city_name,
        source_type=source_type,
        status=status,
        keyword=keyword,
        published_from=published_from,
        published_to=published_to,
        captured_from=captured_from,
        captured_to=captured_to,
        has_images=has_images,
        missing_published_at=missing_published_at,
        missing_author_followers=missing_author_followers,
        missing_field=missing_field,
        q=q,
    )
    result = service.list_records(filters, page=page, page_size=page_size, sort=sort)
    return ok(result.items, result.meta)


@router.get("/{record_id}")
def get_record(record_id: int, service: RecordService = Depends(record_service)) -> dict:
    record = service.get_record_detail(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="record not found")
    return ok(record)


@router.get("/{record_id}/context")
def get_record_context(record_id: int, service: RecordService = Depends(record_service)) -> dict:
    context = service.get_record_context(record_id)
    if context is None:
        raise HTTPException(status_code=404, detail="record not found")
    return ok(context)


@router.get("/{record_id}/raw")
def get_record_raw(record_id: int, service: RecordService = Depends(record_service)) -> dict:
    raw = service.get_record_raw(record_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="record not found")
    return ok(raw)
