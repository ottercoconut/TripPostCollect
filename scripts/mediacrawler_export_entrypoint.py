#!/usr/bin/env python
"""Run MediaCrawler with TripPostCollect's fail-closed export sanitizer."""

from __future__ import annotations

import os
import runpy
import sys
from functools import wraps
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
MEDIACRAWLER_ROOT = ROOT / "tools" / "MediaCrawler"
EXPORT_METHODS = (
    "write_to_csv",
    "write_to_jsonl",
    "write_single_item_to_json",
)


def sanitize_export_item(item: dict[str, Any]) -> dict[str, Any]:
    """Sanitize one item immediately before MediaCrawler serializes it."""
    if os.environ.get("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS") != "1":
        raise RuntimeError("TripPostCollect MediaCrawler export sanitizer is not enabled")
    source_text = str(SOURCE_ROOT)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    from trippostcollect.records.sanitization import sanitize_author_avatar_data

    sanitized = sanitize_author_avatar_data(item).value
    if not isinstance(sanitized, dict):
        raise RuntimeError("sanitized MediaCrawler item must remain an object")
    return sanitized


def install_export_hook() -> None:
    """Wrap every MediaCrawler structured-data writer before loading its main module."""
    media_root_text = str(MEDIACRAWLER_ROOT)
    if media_root_text not in sys.path:
        sys.path.insert(0, media_root_text)
    from tools.async_file_writer import AsyncFileWriter

    if getattr(AsyncFileWriter, "_trippostcollect_avatar_sanitizer", False):
        return
    for method_name in EXPORT_METHODS:
        original = getattr(AsyncFileWriter, method_name)

        @wraps(original)
        async def sanitized_writer(
            self: Any,
            item: dict[str, Any],
            item_type: str,
            *,
            _original: Any = original,
        ) -> Any:
            return await _original(self, sanitize_export_item(item), item_type)

        setattr(AsyncFileWriter, method_name, sanitized_writer)
    AsyncFileWriter._trippostcollect_avatar_sanitizer = True


def main() -> None:
    main_path = MEDIACRAWLER_ROOT / "main.py"
    if not main_path.is_file():
        raise RuntimeError(f"MediaCrawler main module is missing: {main_path}")
    install_export_hook()
    runpy.run_path(str(main_path), run_name="__main__")


if __name__ == "__main__":
    main()
