"""JSON parsing helpers for raw database fields."""

from __future__ import annotations

import json
from typing import Any


def parse_json_text(value: str | None, *, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        return {
            "value": value,
            "parse_error": f"{exc.msg} at line {exc.lineno} column {exc.colno}",
        }
