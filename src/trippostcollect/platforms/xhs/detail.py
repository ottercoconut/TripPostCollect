# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/core.py
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

# TripPostCollect：T09 迁入根平台；来源 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303，许可见 resources/licenses/MediaCrawler-LICENSE。

"""指定笔记只经 repair：直开保存的链接并读取页面状态（repair.py）。"""

from trippostcollect.platforms.xhs.repair import run_xhs_repair


class XhsDetailMixin:
    """指定笔记；作为 XiaoHongShuCrawler 的 mixin。"""

    async def get_specified_notes(self):
        """指定笔记模式：只保留 repair 分支，非 repair 的 detail 模式直接报错。"""
        if not self.ports.repair:
            # 经 API 取指定笔记详情的分支已删除；指定笔记只经 repair 处理。
            raise RuntimeError("xhs_detail_mode_requires_repair")
        # 修复开关在 install_hooks 时点读取并经装配传入；显式分支取代旧桥对类方法的替换。
        return await run_xhs_repair(self)
