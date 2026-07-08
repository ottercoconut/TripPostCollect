"""Small response envelope helpers."""

from __future__ import annotations

from typing import Any


def ok(data: Any, meta: dict[str, Any] | None = None, errors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "data": data,
        "meta": meta or {},
        "errors": errors or [],
    }


def error_response(
    code: str,
    message: str,
    *,
    field: str | None = None,
    status_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if field:
        error["field"] = field
    return ok(None, status_meta, [error])
