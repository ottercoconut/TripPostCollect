"""Read-only scheduler routes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from apps.admin_api.app.deps import get_db, get_settings
from apps.admin_api.app.response import ok
from apps.admin_api.app.settings import AdminSettings
from trippostcollect.scheduler.config import read_scheduler_config
from trippostcollect.scheduler.repository import list_jobs, list_reports


router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/config")
def scheduler_config(settings: AdminSettings = Depends(get_settings)) -> dict:
    return ok(read_scheduler_config(settings.config_path))


@router.get("/jobs")
def scheduler_jobs(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return ok(list_jobs(conn))


@router.get("/reports")
def scheduler_reports(limit: int = Query(default=20, ge=1, le=100), conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return ok(list_reports(conn, limit=limit))
