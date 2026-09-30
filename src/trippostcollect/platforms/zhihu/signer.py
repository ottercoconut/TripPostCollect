# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/zhihu/help.py
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

# TripPostCollect：T07 迁入根平台；来源 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303，许可见 resources/licenses/MediaCrawler-LICENSE。

from typing import Dict
import execjs
from trippostcollect.core import resources

ZHIHU_SGIN_JS = None


def sign(url: str, cookies: str) -> Dict:
    """
    zhihu sign algorithm
    Args:
        url: request url with query string
        cookies: request cookies with d_c0 key

    Returns:

    """
    global ZHIHU_SGIN_JS
    if not ZHIHU_SGIN_JS:
        ZHIHU_SGIN_JS = execjs.compile(resources.read_text("js/zhihu.js"))

    return ZHIHU_SGIN_JS.call("get_sign", url, cookies)
