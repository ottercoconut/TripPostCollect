# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/model/m_zhihu.py
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

from enum import Enum
from httpx import RequestError
from pydantic import BaseModel, Field
from . import models as zhihu_constant

ZHIHU_URL = "https://www.zhihu.com"
ZHIHU_ZHUANLAN_URL = "https://zhuanlan.zhihu.com"

ANSWER_NAME = "answer"
ARTICLE_NAME = "article"
VIDEO_NAME = "zvideo"


class ZhihuContent(BaseModel):
    """
    Zhihu content (answer, article, video)
    """
    content_id: str = Field(default="", description="Content ID")
    content_type: str = Field(default="", description="Content type (article | answer | zvideo)")
    content_text: str = Field(default="", description="Content text, empty for video type")
    content_url: str = Field(default="", description="Content landing page URL")
    question_id: str = Field(default="", description="Question ID, has value when type is answer")
    title: str = Field(default="", description="Content title")
    desc: str = Field(default="", description="Content description")
    created_time: int = Field(default=0, description="Create time")
    updated_time: int = Field(default=0, description="Update time")
    voteup_count: int = Field(default=0, description="Upvote count")
    comment_count: int = Field(default=0, description="Comment count")
    image_list: list[str] = Field(default_factory=list, description="Content image URLs")
    image_count: int = Field(default=0, description="Content image count")
    image_list_source: str = Field(default="", description="Authoritative body image source")
    image_assets: list[dict] = Field(default_factory=list, description="Ordered body image assets")
    content_detail_status: str = Field(
        default="search_payload",
        description="Detail enrichment status for search results",
    )
    content_detail_source: str = Field(
        default="",
        description="Authoritative source that proved the persisted body complete",
    )
    source_keyword: str = Field(default="", description="Source keyword")
    creator_hash: str = Field(default="", description="Creator raw platform user ID")
    creator_url_token: str = Field(default="", description="Creator URL token")
    user_nickname: str = Field(default="", description="User raw nickname")
    author_profile_url: str = Field(default="", description="Creator profile URL")
    avatar_url: str = Field(default="", description="Creator avatar URL")
    followers_count: int = Field(default=0, description="Creator follower count")
    followers_observed: bool = Field(default=False, description="Whether the platform response explicitly included follower count")
    author_followers_source: str = Field(default="", description="Follower count source")
    following_count: int = Field(default=0, description="Creator following count")
    author_desc: str = Field(default="", description="Creator headline or description")
    verified_text: str = Field(default="", description="Creator verification text")


class ZhihuCreator(BaseModel):
    """
    Zhihu creator (in-memory only; personal profile is no longer persisted)
    """
    creator_hash: str = Field(default="", description="Creator raw platform user ID")
    url_token: str = Field(default="", description="Creator URL token")
    user_nickname: str = Field(default="", description="User raw nickname")
    profile_url: str = Field(default="", description="Creator profile URL")
    avatar_url: str = Field(default="", description="Creator avatar URL")
    follows: int = Field(default=0, description="Follows count")
    fans: int = Field(default=0, description="Fans count")
    followers_observed: bool = Field(default=False, description="Whether the platform response explicitly included follower count")
    author_followers_source: str = Field(default="", description="Follower count source")
    headline: str = Field(default="", description="Creator headline or description")
    verified_text: str = Field(default="", description="Creator verification text")
    anwser_count: int = Field(default=0, description="Answer count")
    video_count: int = Field(default=0, description="Video count")
    question_count: int = Field(default=0, description="Question count")
    article_count: int = Field(default=0, description="Article count")
    column_count: int = Field(default=0, description="Column count")
    get_voteup_count: int = Field(default=0, description="Total upvotes received")


class SearchTime(Enum):
    """
    Search time range
    """
    DEFAULT = ""  # No time limit
    ONE_DAY = "a_day"  # Within one day
    ONE_WEEK = "a_week"  # Within one week
    ONE_MONTH = "a_month"  # Within one month
    THREE_MONTH = "three_months"  # Within three months
    HALF_YEAR = "half_a_year"  # Within half a year
    ONE_YEAR = "a_year"  # Within one year


class SearchType(Enum):
    """
    Search result type
    """
    DEFAULT = ""  # No type limit
    ANSWER = zhihu_constant.ANSWER_NAME  # Answers only
    ARTICLE = zhihu_constant.ARTICLE_NAME  # Articles only
    VIDEO = zhihu_constant.VIDEO_NAME  # Videos only


class SearchSort(Enum):
    """
    Search result sorting
    """
    DEFAULT = ""  # Default sorting
    UPVOTED_COUNT = "upvoted_count"  # Most upvoted
    CREATE_TIME = "created_time"  # Latest published


class DataFetchError(RequestError):
    """something error when fetch"""


class PlatformRuntimeError(DataFetchError):
    """A login or rate-limit response that must stop the current run."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class ZhihuImageDownloadError(RuntimeError):
    """A Zhihu body image exhausted its applicable fetch attempts."""

    def __init__(self, content_id: str, source_index: int, code: str, attempts: int = 1):
        super().__init__(
            f"Zhihu image download failed: content_id={content_id}, "
            f"source_index={source_index}, code={code}"
        )
        self.content_id = content_id
        self.source_index = source_index
        self.code = code
        self.attempts = max(1, int(attempts))


class ZhihuDetailFetchError(RuntimeError):
    """A search candidate detail remained unavailable after retries."""

    def __init__(
        self,
        content_id: str,
        code: str,
        attempts: int = 3,
        *,
        runtime_blocking: bool = False,
    ):
        super().__init__(
            f"Zhihu detail fetch failed: content_id={content_id or '<missing>'}, code={code}"
        )
        self.content_id = content_id
        self.code = code
        self.attempts = max(1, int(attempts))
        self.runtime_blocking = runtime_blocking

