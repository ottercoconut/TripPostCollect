#!/usr/bin/env python
"""Run MediaCrawler with TripPostCollect's fail-closed export sanitizer."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from typing import Any




ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
# 两站的纯辅助直接重导出；脚本直启时先提供根源码路径。
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from trippostcollect.runtime.helpers import _find_nested_platform_record as _find_nested_platform_record  # noqa: E402
from trippostcollect.platforms.weibo.parser import _find_weibo_detail as _find_weibo_detail  # noqa: E402
from trippostcollect.platforms.weibo.client import _weibo_detail_api_url as _weibo_detail_api_url  # noqa: E402
from trippostcollect.platforms.douyin.parser import (  # noqa: E402
    _douyin_detail_urls as _douyin_detail_urls,
    _find_douyin_detail as _find_douyin_detail,
)

MEDIACRAWLER_ROOT = ROOT / "tools" / "MediaCrawler"
EXPORT_METHODS = (
    "write_to_csv",
    "write_to_jsonl",
    "write_single_item_to_json",
)

def sanitize_export_item(item: dict[str, Any]) -> dict[str, Any]:
    source_text = str(SOURCE_ROOT)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    from trippostcollect.records.sanitization import sanitize_export_item as sanitize

    return sanitize(item)


def install_export_hook() -> None:
    media_root_text = str(MEDIACRAWLER_ROOT)
    if media_root_text not in sys.path:
        sys.path.insert(0, media_root_text)
    from tools.async_file_writer import AsyncFileWriter
    from trippostcollect.records.sanitization import install_export_hook as install_writer_hook

    if install_writer_hook(AsyncFileWriter):
        install_batch_checkpoint_hook()


def install_batch_checkpoint_hook() -> None:
    from trippostcollect.xhs.batch_checkpoint import ENABLED_ENV, publish_batch

    if os.environ.get(ENABLED_ENV) != "1":
        return
    from tools import trippostcollect_adaptive as adaptive
    from trippostcollect.application.events import install_batch_checkpoint_hook as install_legacy_hook

    install_legacy_hook(adaptive, publish_batch=publish_batch)


# T09：小红书修复编排迁入 trippostcollect.platforms.xhs.repair，报告写出迁入 artifacts/evidence；
# 旧名重导出，旧 hook 只在安装时锁存开关，由根 crawler 以显式分支选用修复编排。
from trippostcollect.artifacts.evidence import _write_xhs_repair_report as _write_xhs_repair_report  # noqa: E402
from trippostcollect.platforms.xhs.repair import (  # noqa: E402
    _repair_exception_is_blocking as _repair_exception_is_blocking,
    _xhs_repair_blocker as _xhs_repair_blocker,
    _xhs_repair_failure as _xhs_repair_failure,
    _xhs_repair_failure_scope as _xhs_repair_failure_scope,
)


def install_xhs_repair_resilience() -> None:
    # 保留旧 hook 的读取时点与幂等锁存；修复编排只存在于根 crawler。
    from trippostcollect.application.worker_inputs import xhs_repair_reader
    from trippostcollect.platforms import entry

    if xhs_repair_reader()():
        entry._xhs_repair = True


def install_douyin_browser_detail_fallback() -> None:
    # 旧桥只在安装时锁存开关；根 crawler 和 fork client 装配共同消费该值。
    from trippostcollect.application.worker_inputs import douyin_browser_detail_fallback_reader
    from trippostcollect.platforms import entry

    if douyin_browser_detail_fallback_reader()():
        entry._douyin_browser_detail_fallback = True


def install_weibo_browser_detail_fallback() -> None:
    # 保留旧 hook 的读取时点与幂等锁存，回退算法只存在于根客户端。
    if os.environ.get("TRIPPOSTCOLLECT_POST_REPAIR") != "1":
        return
    from media_platform.weibo import core as weibo_core

    weibo_core._post_repair = True


def main() -> None:
    main_path = MEDIACRAWLER_ROOT / "main.py"
    if not main_path.is_file():
        raise RuntimeError(f"MediaCrawler main module is missing: {main_path}")
    source_text = str(SOURCE_ROOT)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    install_export_hook()
    install_xhs_repair_resilience()
    install_douyin_browser_detail_fallback()
    install_weibo_browser_detail_fallback()
    runpy.run_path(str(main_path), run_name="__main__")


if __name__ == "__main__":
    main()
