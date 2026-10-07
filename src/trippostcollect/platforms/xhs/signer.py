# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/playwright_sign.py
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

"""小红书签名：每次调用延迟导入并新建 xhshow 实例，trace id 保留随机回退。"""

# 致谢：本签名实现依赖 xhshow 开源库, 由 Cloxl 提供
# 仓库地址: https://github.com/Cloxl/xhshow
# 许可协议: MIT License

import random
from typing import Any, Dict, Optional, Union


def sign_with_xhshow(
    uri: str,
    data: Optional[Union[Dict, str]] = None,
    cookie_str: str = "",
    method: str = "POST",
) -> Dict[str, Any]:
    """
    使用 xhshow 纯算法生成完整签名请求头

    Args:
        uri: API path
        data: Request data (GET params dict or POST payload dict)
        cookie_str: Cookie string
        method: Request method (GET or POST)

    Returns:
        Dictionary containing x-s, x-t, x-s-common, x-b3-traceid
    """
    from xhshow import Xhshow
    xhshow_client = Xhshow()

    if method.upper() == "POST":
        headers = xhshow_client.sign_headers_post(
            uri=uri,
            cookies=cookie_str,
            payload=data if isinstance(data, dict) else {},
        )
    else:
        headers = xhshow_client.sign_headers_get(
            uri=uri,
            cookies=cookie_str,
            params=data if isinstance(data, dict) else {},
        )

    return {
        "x-s": headers.get("x-s", ""),
        "x-t": headers.get("x-t", ""),
        "x-s-common": headers.get("x-s-common", ""),
        "x-b3-traceid": headers.get("x-b3-traceid", get_trace_id()),
    }


def get_trace_id() -> str:
    """Generate trace id for link tracing"""
    return "".join(random.choice("abcdef0123456789") for _ in range(16))
