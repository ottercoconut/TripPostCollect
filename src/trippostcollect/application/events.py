"""宽容的旧事件写出，以及先 append 再 publish 的显式批次出口。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_execution_event(event_type: str, details: dict[str, Any]) -> None:
    state_value = os.environ.get("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", "").strip()
    if not state_value:
        return
    path = Path(state_value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.setdefault("events", []).append({"at": _utc_iso(), "type": event_type, "details": details})
        payload["updated_at"] = _utc_iso()
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except (OSError, json.JSONDecodeError, TypeError):
        return


_batch_publisher: Callable[[dict[str, Any]], None] | None = None


def configure_batch_checkpoint(publisher: Callable[[dict[str, Any]], None] | None) -> None:
    """在原 worker 安装时点绑定发布能力，不提前读取 execution state。"""
    global _batch_publisher
    _batch_publisher = publisher


def append_and_publish(
    event_type: str, details: dict[str, Any], *,
    original: Callable[[str, dict[str, Any]], None],
    publish_batch: Callable[[dict[str, Any]], None] | None,
) -> None:
    original(event_type, details)
    if event_type == "adaptive_batch_completed" and publish_batch is not None:
        try:
            publish_batch(details)
        except Exception as exc:
            detail = str(exc)
            if not detail.startswith("xhs_batch_checkpoint_"):
                detail = f"xhs_batch_checkpoint_{type(exc).__name__.lower()}"
            original("xhs_runtime_terminal", {
                "phase": "batch_checkpoint",
                "failure_type": "runtime_failed",
                "stop_reason": "runtime_failed",
                "stop_detail": detail,
                "retryable": False,
            })
            raise RuntimeError(detail) from exc


def append_worker_execution_event(event_type: str, details: dict[str, Any]) -> None:
    """fork 保留同名可 patch 出口；持久化失败仍沿 legacy 吞错边界。"""
    append_and_publish(
        event_type, details, original=append_execution_event, publish_batch=_batch_publisher,
    )


def install_batch_checkpoint_hook(adaptive: Any, *, publish_batch: Callable) -> None:
    """仅供在途旧桥安装原包装；正式 worker 使用显式发布出口。"""
    if getattr(adaptive, "_trippostcollect_batch_checkpoint", False):
        return
    original = adaptive.append_execution_event

    def append_and_checkpoint(event_type: str, details: dict[str, Any]) -> None:
        append_and_publish(event_type, details, original=original, publish_batch=publish_batch)

    adaptive.append_execution_event = append_and_checkpoint
    adaptive._trippostcollect_batch_checkpoint = True
