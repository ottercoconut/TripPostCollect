"""Health and metadata routes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from apps.admin_api.app.deps import get_db, get_settings
from apps.admin_api.app.response import ok
from apps.admin_api.app.settings import AdminSettings
from trippostcollect import __version__
from trippostcollect.db.status import count_table_rows, schema_status


router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health(settings: AdminSettings = Depends(get_settings)) -> dict:
    return ok(
        {
            "status": "ok",
            "readonly": settings.db_readonly,
            "commands_enabled": settings.allow_commands,
        }
    )


@router.get("/meta")
def meta(
    settings: AdminSettings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    tables = [
        "source_platforms",
        "web_posts",
        "web_post_images",
        "ctf_captures",
        "ctf_capture_images",
        "crawl_jobs",
        "crawl_run_reports",
    ]
    return ok(
        {
            "version": __version__,
            "db_path": str(settings.db_path),
            "config_path": str(settings.config_path),
            "readonly": settings.db_readonly,
            "commands_enabled": settings.allow_commands,
            "refresh_seconds": settings.refresh_seconds,
            "schema": schema_status(conn),
            "table_counts": {table: count_table_rows(conn, table) for table in tables},
        }
    )
