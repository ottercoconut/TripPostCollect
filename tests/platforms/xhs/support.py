"""旧 fork 用例的局部配置与装配接缝；不导入 fork，不读取真实配置。"""

from contextlib import contextmanager
from dataclasses import fields, is_dataclass
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
import uuid

from trippostcollect.platforms.entry import xhs_dependencies
from trippostcollect.platforms.xhs.capture import XHS_RULES, XHS_SENTINEL, XHS_VERIFY_HEADERS
from trippostcollect.platforms.xhs.core import XiaoHongShuCrawler as RootXiaoHongShuCrawler
from trippostcollect.platforms.xhs.page_scripts import STATE_SCRIPTS
from trippostcollect.records.sanitization import author_avatar_log_marker
from trippostcollect.runtime.page_capture import PageCaptureChannel
from trippostcollect.runtime.visit_ledger import VisitLedger


# 与原 fork 用例运行时的 base_config/xhs_config 默认值一致，用例再按需 patch。
config = SimpleNamespace(
    PLATFORM="xhs", LOGIN_TYPE="qrcode", COOKIES="", CRAWLER_TYPE="search",
    ENABLE_CDP_MODE=False, CDP_HEADLESS=False, HEADLESS=False, KEYWORDS="编程副业,编程兼职",
    ENABLE_GET_MEIDAS=False,
    SAVE_DATA_OPTION="jsonl", SAVE_DATA_PATH="",
    XHS_SPECIFIED_NOTE_URL_LIST=[
        "https://www.xiaohongshu.com/explore/64b95d01000000000c034587"
        "?xsec_token=AB0EFqJvINCkj6xOCKCQgfNNh8GdnBC_6XecG4QOddo3Q=&xsec_source=pc_cfeed",
    ],
    CDP_CONNECT_EXISTING=False, CDP_DEBUG_PORT=9222, BROWSER_LAUNCH_TIMEOUT=60, CUSTOM_BROWSER_PATH="",
    SAVE_LOGIN_STATE=True, AUTO_CLOSE_BROWSER=True, DISABLE_SSL_VERIFY=False,
    CRAWLER_MAX_NOTES_COUNT=15,
)


def mutable(value):
    """把冻结端口展开为可 patch 的命名空间；嵌套端口同样展开。"""
    if not is_dataclass(value):
        return value
    return SimpleNamespace(**{field.name: mutable(getattr(value, field.name)) for field in fields(value)})


def dependencies():
    resolved = xhs_dependencies(config, repair=False)
    resolved["ports"] = mutable(resolved["ports"])
    return resolved


class XiaoHongShuCrawler(RootXiaoHongShuCrawler):
    """旧用例无参构造；构造后仍可 patch 测试配置与注入端口。"""

    def __init__(self):
        super().__init__(**dependencies())
        self.settings = config


def make_capture(directory: Path, *, context=None, mode: str = "discovery") -> PageCaptureChannel:
    """与会话相同规则的页面取数通道；账本写在用例临时目录，名称唯一。"""
    ledger = VisitLedger.create(
        Path(directory) / f"behavior_evidence.{uuid.uuid4().hex[:8]}.content_visits.jsonl",
        platform="xhs",
        mode=mode,
    )
    return PageCaptureChannel(
        context if context is not None else SimpleNamespace(pages=[], on=lambda *args: None),
        platform="xhs",
        rules=XHS_RULES,
        ledger=ledger,
        sentinel=XHS_SENTINEL,
        verify_headers=XHS_VERIFY_HEADERS,
        state_scripts=STATE_SCRIPTS,
    )


@contextmanager
def crawler_log_lines() -> Iterator[list[str]]:
    """收集期间 MediaCrawler logger 的消息与异常文本（按行），且不传到根 logger：失败报告不会带出原文。"""
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.extend(record.getMessage().splitlines())
            if record.exc_info:
                lines.extend(logging.Formatter().formatException(record.exc_info).splitlines())

    logger = logging.getLogger("MediaCrawler")
    handler = Collect(logging.DEBUG)
    saved_level, saved_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def assert_no_avatar_log_lines(lines: list[str]) -> None:
    """日志不命中头像正则；失败时只报审计标记（命中行数与模块标签），不报原文。"""
    marker = author_avatar_log_marker({"log": "\n".join(lines)})
    assert marker is None, marker
