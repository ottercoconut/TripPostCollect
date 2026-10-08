# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/weibo/exception.py
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


# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/2 18:44
# @Desc    :


# TripPostCollect T05：迁自 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303；仅拆分职责与注入依赖。
from enum import Enum
from dataclasses import dataclass
from httpx import RequestError


@dataclass(frozen=True)
class WeiboConfig:
    """worker 已解析配置的只读快照；不包含 Cookie 刷新或 env 操作读取。"""

    ENABLE_CDP_MODE: bool
    CDP_HEADLESS: bool
    HEADLESS: bool
    LOGIN_TYPE: str
    COOKIES: str
    CRAWLER_TYPE: str
    KEYWORDS: str
    START_PAGE: int
    WEIBO_SEARCH_TYPE: str
    CRAWLER_MAX_SLEEP_SEC: float
    MAX_CONCURRENCY_NUM: int
    WEIBO_SPECIFIED_ID_LIST: tuple[str, ...]
    ENABLE_GET_MEIDAS: bool
    SAVE_LOGIN_STATE: bool
    PLATFORM: str
    ENABLE_WEIBO_FULL_TEXT: bool
    SAVE_DATA_OPTION: str
    SAVE_DATA_PATH: str

class DataFetchError(RequestError):
    """something error when fetch"""


class PlatformRuntimeError(DataFetchError):
    """A login or rate-limit response that must stop the current run."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/weibo/field.py
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


# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/23 15:41
# @Desc    :

# TripPostCollect T05：迁自 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303；仅拆分职责与注入依赖。
class SearchType(Enum):
    # Comprehensive
    DEFAULT = "1"

    # Real-time
    REAL_TIME = "61"

    # Popular
    POPULAR = "60"

    # Video
    VIDEO = "64"


# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/weibo/core.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/23 15:41
# @Desc    : Weibo crawler main workflow code

# TripPostCollect：资源与 profile 基目录不再依赖进程 cwd。

# TripPostCollect T05：迁自 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303；仅拆分职责与注入依赖。
class WeiboImageDownloadError(RuntimeError):
    """A post image exhausted its applicable fetch attempts."""

    def __init__(self, note_id: str, source_index: int, code: str, attempts: int = 1):
        super().__init__(
            f"Weibo image download failed: note_id={note_id}, "
            f"source_index={source_index}, code={code}"
        )
        self.note_id = note_id
        self.source_index = source_index
        self.code = code
        self.attempts = max(1, int(attempts))


class WeiboFullTextFetchError(RuntimeError):
    """A long Weibo post remained unavailable after its detail attempts."""

    def __init__(
        self,
        note_id: str,
        code: str,
        attempts: int = 3,
        *,
        runtime_blocking: bool = False,
    ):
        super().__init__(
            f"Weibo full-text fetch failed: note_id={note_id or '<missing>'}, code={code}"
        )
        self.note_id = note_id
        self.code = code
        self.attempts = max(1, int(attempts))
        self.runtime_blocking = runtime_blocking
