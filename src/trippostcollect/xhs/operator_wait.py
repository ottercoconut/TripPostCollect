"""小红书人工等待信号：child 写“人工等待中”诊断，父层据此冻结无持久进展看门狗计时。

与网络暂停共用同一套做法：诊断文件位于 behavior evidence 同目录，父层只信任新鲜、结构有效且
来自已核验 exporter 的观察；等待结束时写明确的 ``ended``，陈旧或格式错误的诊断不获得暂停时间。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

OPERATOR_WAIT_SCHEMA_VERSION = 1
OPERATOR_WAIT_STATES = frozenset({"waiting", "ended"})
# 等待期间的刷新间隔，必须小于父层 90 秒诊断新鲜度。
OPERATOR_WAIT_REFRESH_SECONDS = 30.0
BEHAVIOR_EVIDENCE_ENV = "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE"

logger = logging.getLogger("MediaCrawler")


def operator_wait_diagnostics_path(behavior_evidence_path: str | Path) -> Path:
    path = Path(behavior_evidence_path).expanduser()
    return path.with_name(f"{path.stem}.operator_wait.json")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OperatorWaitSignal:
    """按嵌套深度合并所有人工等待；只在首个进入与最后一个退出时改变对外状态。"""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._depth = 0
        self._stage = ""
        self._started_at = ""
        self._lock = threading.Lock()
        self._refresh_stop: threading.Event | None = None

    @property
    def active(self) -> bool:
        return self._depth > 0

    def enter(self, stage: str) -> None:
        with self._lock:
            self._depth += 1
            if self._depth != 1:
                return
            self._stage = stage
            self._started_at = _utc_now()
            self._write("waiting")
            if self.path is None:
                return
            # 刷新用独立守护线程，不占用 worker 的事件循环，也不改变其调度。
            stop = threading.Event()
            self._refresh_stop = stop
            threading.Thread(
                target=self._refresh,
                args=(stop,),
                name="xhs-operator-wait-refresh",
                daemon=True,
            ).start()

    def exit(self) -> None:
        with self._lock:
            if self._depth == 0:
                return
            self._depth -= 1
            if self._depth:
                return
            stop, self._refresh_stop = self._refresh_stop, None
            if stop is not None:
                stop.set()
            self._write("ended")

    @contextmanager
    def waiting(self, stage: str) -> Iterator[None]:
        self.enter(stage)
        try:
            yield
        finally:
            self.exit()

    @contextmanager
    def episode(self, stage: str) -> Iterator[OperatorWaitEpisode]:
        """函数内只有部分区间是人工等待时使用：``begin``/``end`` 幂等，离开上下文时必然结束。"""

        episode = OperatorWaitEpisode(self, stage)
        try:
            yield episode
        finally:
            episode.end()

    def _refresh(self, stop: threading.Event) -> None:
        while not stop.wait(OPERATOR_WAIT_REFRESH_SECONDS):
            with self._lock:
                # 已结束的等待不得被迟到的刷新改回 waiting。
                if stop.is_set():
                    return
                self._write("waiting")

    def _write(self, state: str) -> None:
        if self.path is None:
            return
        now = _utc_now()
        payload = {
            "schema_version": OPERATOR_WAIT_SCHEMA_VERSION,
            "platform": "xhs",
            "updated_at": now,
            "state": state,
            "stage": self._stage,
            "started_at": self._started_at,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            logger.warning(
                "[XiaoHongShuCrawler] Could not persist operator wait diagnostic: "
                f"{type(exc).__name__}: {exc}"
            )


class OperatorWaitEpisode:
    def __init__(self, signal: OperatorWaitSignal, stage: str) -> None:
        self._signal = signal
        self._stage = stage
        self._open = False

    def begin(self) -> None:
        if not self._open:
            self._open = True
            self._signal.enter(self._stage)

    def end(self) -> None:
        if self._open:
            self._open = False
            self._signal.exit()


_SIGNALS: dict[str, OperatorWaitSignal] = {}


def current_operator_wait_signal() -> OperatorWaitSignal:
    """本 worker 的唯一信号；路径由已有的 behavior evidence 位置推导，未配置时只计数不写盘。"""

    evidence_path = os.environ.get(BEHAVIOR_EVIDENCE_ENV, "").strip()
    signal = _SIGNALS.get(evidence_path)
    if signal is None:
        signal = OperatorWaitSignal(
            operator_wait_diagnostics_path(evidence_path) if evidence_path else None
        )
        _SIGNALS[evidence_path] = signal
    return signal
