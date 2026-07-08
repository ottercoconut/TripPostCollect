"""Read-only maintenance inspection routes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from apps.admin_api.app.deps import get_db
from apps.admin_api.app.response import ok
from trippostcollect.db.status import schema_status


router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


@router.get("/schema-status")
def get_schema_status(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return ok(schema_status(conn))
