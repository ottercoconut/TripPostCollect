"""运行时间戳与监督进度回调。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f%z")


def _runtime_progress(callback: Callable[[], object] | None) -> None:
    if callback is not None:
        callback()


def _runtime_progress_if_due(
    callback: Callable[[], object] | None,
    last_checkpoint_at: float,
    *,
    interval_seconds: float = 5.0,
) -> float:
    if callback is None:
        return last_checkpoint_at
    now = time.monotonic()
    if now - last_checkpoint_at < interval_seconds:
        return last_checkpoint_at
    callback()
    return time.monotonic()
