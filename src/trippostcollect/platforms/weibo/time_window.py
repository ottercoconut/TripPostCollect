"""#217 微博显式时间窗：解析 job 参数，并给出地址与账本使用的小时形式。

区间按北京时间理解，左闭右开。日期形式等于当日 0 点。规范化结果不补零。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any
from zoneinfo import ZoneInfo


TIME_WINDOW_INVALID = "time_window_invalid"
TIME_WINDOW_UNSUPPORTED = "time_window_unsupported"
TIME_WINDOW_ARGS_INCOMPLETE = "time_window_args_incomplete"

ENV_TIME_WINDOW = "TRIPPOSTCOLLECT_TIME_WINDOW"
ENV_TIME_WINDOW_JOB_KEY = "TRIPPOSTCOLLECT_TIME_WINDOW_JOB_KEY"

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_BOUND_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:-(\d{1,2}))?$")


class TimeWindowError(ValueError):
    """配置或 child 参数不符合时间窗格式。``code`` 是稳定错误码，正文带 job_key。"""

    def __init__(self, code: str, job_key: str, detail: str) -> None:
        self.code = code
        self.job_key = job_key
        super().__init__(f"{code}: job={job_key}: {detail}")


@dataclass(frozen=True)
class TimeWindow:
    """原始起止与规范化小时形式。``start_at`` 含、``end_at`` 不含。"""

    start_raw: str
    end_raw: str
    start: str
    end: str
    start_at: datetime
    end_at: datetime

    @property
    def timescope(self) -> str:
        return f"custom:{self.start}:{self.end}"

    def audit(self) -> dict[str, str]:
        return {
            "start": self.start_raw,
            "end": self.end_raw,
            "start_normalized": self.start,
            "end_normalized": self.end,
            "timescope": self.timescope,
        }


def _bound(value: Any, *, job_key: str, field: str) -> tuple[str, datetime]:
    if not isinstance(value, str):
        raise TimeWindowError(TIME_WINDOW_INVALID, job_key, f"{field} must be a string")
    match = _BOUND_RE.fullmatch(value)
    if match is None:
        raise TimeWindowError(
            TIME_WINDOW_INVALID,
            job_key,
            f"{field} must be YYYY-MM-DD or YYYY-MM-DD-H",
        )
    year_text, month_text, day_text, hour_text = match.groups()
    hour = 0 if hour_text is None else int(hour_text)
    if hour > 23:
        raise TimeWindowError(TIME_WINDOW_INVALID, job_key, f"{field} hour must be 0-23")
    year, month, day = int(year_text), int(month_text), int(day_text)
    try:
        date(year, month, day)
    except ValueError as exc:
        raise TimeWindowError(TIME_WINDOW_INVALID, job_key, f"{field} is not a real date") from exc
    moment = datetime(year, month, day, hour, tzinfo=_SHANGHAI)
    return f"{year:04d}-{month:02d}-{day:02d}-{hour}", moment


def parse_time_window(value: Any, *, job_key: str) -> TimeWindow:
    """解析 ``time_window``。只接受 ``start``、``end`` 两个字符串键。"""

    if not isinstance(value, Mapping) or isinstance(value, (str, bytes)):
        raise TimeWindowError(TIME_WINDOW_INVALID, job_key, "time_window must be an object")
    keys = set(value)
    missing = sorted({"start", "end"} - keys)
    if missing:
        raise TimeWindowError(
            TIME_WINDOW_INVALID,
            job_key,
            f"time_window missing {', '.join(missing)}",
        )
    extra = sorted(keys - {"start", "end"})
    if extra:
        raise TimeWindowError(
            TIME_WINDOW_INVALID,
            job_key,
            f"time_window unexpected {', '.join(extra)}",
        )
    start, start_at = _bound(value["start"], job_key=job_key, field="start")
    end, end_at = _bound(value["end"], job_key=job_key, field="end")
    if start_at >= end_at:
        raise TimeWindowError(TIME_WINDOW_INVALID, job_key, "start must be earlier than end")
    return TimeWindow(
        start_raw=str(value["start"]),
        end_raw=str(value["end"]),
        start=start,
        end=end,
        start_at=start_at,
        end_at=end_at,
    )


def search_filter_signature(window: TimeWindow | None) -> str:
    """访问账本的过滤签名。默认模式只有 ``nodup=1``。"""

    if window is None:
        return "nodup=1"
    return f"nodup=1;timescope={window.timescope}"


def worker_environment(audit: Mapping[str, str] | None, *, job_key: str) -> dict[str, str]:
    """交给微博 worker 的原始起止。没有时间窗时不写环境变量。"""

    if not audit:
        return {}
    return {
        ENV_TIME_WINDOW: json.dumps(
            {"start": audit["start"], "end": audit["end"]},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        ENV_TIME_WINDOW_JOB_KEY: job_key,
    }


def load_configured_time_window(environ: Mapping[str, str] | None = None) -> TimeWindow | None:
    """读取 child 写入的原始起止。未设置表示默认模式。"""

    source = os.environ if environ is None else environ
    raw = str(source.get(ENV_TIME_WINDOW, "") or "").strip()
    if not raw:
        return None
    job_key = str(source.get(ENV_TIME_WINDOW_JOB_KEY, "") or "").strip() or "child"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TimeWindowError(TIME_WINDOW_INVALID, job_key, "time_window env is not json") from exc
    return parse_time_window(payload, job_key=job_key)
