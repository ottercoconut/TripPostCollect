# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/main.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

# TripPostCollect：改为根解释器入口，仅延迟装配所选站点。
"""根解释器运行的选站 worker 入口。"""

import io
import sys
from importlib import import_module

from trippostcollect.application.worker_inputs import WorkerInputs, apply_to_config, parse_cmd
from trippostcollect.platforms import _fork_bridge
from trippostcollect.runtime import worker


def configure(argv) -> WorkerInputs:
    # 保留上游 UTF-8 包装，避免非 UTF-8 终端输出中文失败。
    if sys.stdout and hasattr(sys.stdout, 'buffer'):
        if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if sys.stderr and hasattr(sys.stderr, 'buffer'):
        if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    _fork_bridge.install()
    inputs = parse_cmd(argv)
    config = import_module("config")
    apply_to_config(inputs, config)
    return inputs


def install_hooks() -> None:
    from mediacrawler_export_entrypoint import (
        install_export_hook,
        install_xhs_repair_resilience,
        install_douyin_browser_detail_fallback,
        install_weibo_browser_detail_fallback,
    )

    install_export_hook()
    install_xhs_repair_resilience()
    install_douyin_browser_detail_fallback()
    install_weibo_browser_detail_fallback()


def load_crawler(code: str) -> type:
    if code == "wb":
        from media_platform.weibo import WeiboCrawler
        return WeiboCrawler
    if code == "dy":
        from media_platform.douyin import DouYinCrawler
        return DouYinCrawler
    if code == "zhihu":
        from media_platform.zhihu import ZhihuCrawler
        return ZhihuCrawler
    if code == "xhs":
        from media_platform.xhs import XiaoHongShuCrawler
        return XiaoHongShuCrawler
    raise ValueError(f"不支持的 worker 平台：{code}")


def main(argv=None) -> int:
    inputs = configure(argv)
    install_hooks()
    state = {"crawler": None}

    async def app_main():
        crawler = load_crawler(inputs.platform)()
        state["crawler"] = crawler
        await crawler.start()

    async def app_cleanup():
        await worker.async_cleanup(state["crawler"], inputs.platform)

    worker.run(
        app_main, app_cleanup,
        cleanup_timeout_seconds=15.0,
        on_first_interrupt=lambda: worker.force_stop(state["crawler"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
