"""旧测试的局部配置与装配接缝；不导入 fork，不读取真实配置。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from trippostcollect.platforms.entry import _zhihu_dependencies
from trippostcollect.platforms.zhihu.core import ZhihuCrawler
from trippostcollect.platforms.zhihu.models import ZhihuSearchPage


config = SimpleNamespace(
    PLATFORM="zhihu", LOGIN_TYPE="cookie", COOKIES="", CRAWLER_TYPE="search",
    ENABLE_CDP_MODE=True, CDP_HEADLESS=True, HEADLESS=True,
    CRAWLER_MAX_SLEEP_SEC=0, ENABLE_GET_MEIDAS=False, KEYWORDS="青岛旅游",
    MAX_CONCURRENCY_NUM=1, SAVE_LOGIN_STATE=True, START_PAGE=1,
    ZHIHU_SPECIFIED_ID_LIST=[], SAVE_DATA_PATH="",
    BROWSER_LAUNCH_TIMEOUT=60,
    CUSTOM_BROWSER_PATH="", AUTO_CLOSE_BROWSER=True, DISABLE_SSL_VERIFY=False,
    CRAWLER_MAX_NOTES_COUNT=15,
)


def make_crawler():
    """旧 __new__ 用例允许在构造后 patch 测试配置；生产构造始终冻结。"""
    crawler = ZhihuCrawler(*_zhihu_dependencies(config))
    crawler.settings = config
    return crawler


def search_client() -> AsyncMock:
    """搜索替身：同步的 Referer 设置不产生未等待协程，其余方法照旧为 AsyncMock。"""
    client = AsyncMock()
    client.set_search_referer = MagicMock()
    return client


def search_page(contents=(), *, is_end=False, video_ids=(), raw_count=None) -> ZhihuSearchPage:
    """一页已校验的 search_v3 结果；raw_count 缺省为内容数加视频数。"""
    contents, video_ids = list(contents), list(video_ids)
    return ZhihuSearchPage(
        contents=contents,
        video_ids=video_ids,
        raw_count=len(contents) + len(video_ids) if raw_count is None else raw_count,
        is_end=is_end,
    )


END = search_page(is_end=True)
