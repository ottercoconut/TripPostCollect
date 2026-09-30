# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/async_file_writer.py
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

"""逐条 JSONL 出口；首次文件操作前默认净化头像。"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
from typing import Callable, Dict

import aiofiles

from trippostcollect.application.contracts import JsonlWriter
from trippostcollect.core.paths import MEDIACRAWLER_DIR
from trippostcollect.records.sanitization import sanitize_export_item


class JsonlContentStore:
    """共用内容出口；保留调用方的 writer 属性名和每次 await 写出时点。"""

    def __init__(self, writer: JsonlWriter, *, writer_attribute: str = "writer"):
        self._writer_attribute = writer_attribute
        setattr(self, writer_attribute, writer)

    async def store_content(self, content_item: Dict):
        await getattr(self, self._writer_attribute).write_to_jsonl(
            item_type="contents", item=content_item,
        )


def get_current_date() -> str:
    """
    Get current date: '2023-12-02'
    :return:
    """
    return time.strftime('%Y-%m-%d', time.localtime())


class AsyncFileWriter:
    def __init__(
        self, platform: str, crawler_type: str, *,
        save_data_path: Callable[[], str] = lambda: "",
        current_date: Callable[[], str] = get_current_date,
        sanitizer: Callable[[dict], dict] = sanitize_export_item,
    ):
        self.lock = asyncio.Lock()
        self.platform = platform
        self.crawler_type = crawler_type
        self._save_data_path = save_data_path
        self._current_date = current_date
        self.sanitizer = sanitizer

    def _get_file_path(self, file_type: str, item_type: str) -> str:
        save_data_path = self._save_data_path()
        if save_data_path:
            base_path = f"{save_data_path}/{self.platform}/{file_type}"
        else:
            base_path = str(MEDIACRAWLER_DIR / "data" / self.platform / file_type)
        pathlib.Path(base_path).mkdir(parents=True, exist_ok=True)
        file_name = f"{self.crawler_type}_{item_type}_{self._current_date()}.{file_type}"
        return f"{base_path}/{file_name}"

    async def write_to_jsonl(self, item: Dict, item_type: str):
        item = self.sanitizer(item)
        file_path = self._get_file_path('jsonl', item_type)
        async with self.lock:
            async with aiofiles.open(file_path, 'a', encoding='utf-8') as f:
                await f.write(json.dumps(item, ensure_ascii=False) + '\n')
