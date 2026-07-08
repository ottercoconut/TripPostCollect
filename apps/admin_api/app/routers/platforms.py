"""Platform routes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from apps.admin_api.app.deps import get_db
from apps.admin_api.app.response import ok
from trippostcollect.platforms.repository import list_platforms


router = APIRouter(prefix="/api/platforms", tags=["platforms"])


@router.get("")
def platforms(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return ok(list_platforms(conn))
