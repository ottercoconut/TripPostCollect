"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
import sqlite3

from fastapi import Depends

from apps.admin_api.app.settings import AdminSettings, load_settings
from trippostcollect.db.connection import connect_readonly_db


@lru_cache(maxsize=1)
def get_settings() -> AdminSettings:
    return load_settings()


def get_db(settings: AdminSettings = Depends(get_settings)) -> Iterator[sqlite3.Connection]:
    with connect_readonly_db(settings.db_path) as conn:
        yield conn


def reset_settings_cache() -> None:
    get_settings.cache_clear()
