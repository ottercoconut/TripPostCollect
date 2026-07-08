"""Overview routes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from apps.admin_api.app.deps import get_db
from apps.admin_api.app.response import ok
from trippostcollect.overview.repository import counts, field_gaps, recent_runs


router = APIRouter(prefix="/api/overview", tags=["overview"])


@router.get("/counts")
def get_counts(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return ok(counts(conn))


@router.get("/field-gaps")
def get_field_gaps(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return ok(field_gaps(conn))


@router.get("/recent-runs")
def get_recent_runs(limit: int = Query(default=10, ge=1, le=100), conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return ok(recent_runs(conn, limit=limit))
