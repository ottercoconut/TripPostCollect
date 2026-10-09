"""旧测试的局部配置与装配接缝；不导入 fork，不读取真实配置。"""

from types import SimpleNamespace
from trippostcollect.platforms.entry import _zhihu_dependencies
from trippostcollect.platforms.zhihu.core import ZhihuCrawler


config = SimpleNamespace(
    PLATFORM="zhihu", LOGIN_TYPE="cookie", COOKIES="", CRAWLER_TYPE="search",
    ENABLE_CDP_MODE=True, CDP_HEADLESS=True, HEADLESS=True,
    CRAWLER_MAX_SLEEP_SEC=0, ENABLE_GET_MEIDAS=False, KEYWORDS="青岛旅游",
    MAX_CONCURRENCY_NUM=1, SAVE_LOGIN_STATE=True, START_PAGE=1,
    ZHIHU_SPECIFIED_ID_LIST=[], SAVE_DATA_PATH="",
    CDP_CONNECT_EXISTING=False, CDP_DEBUG_PORT=9222, BROWSER_LAUNCH_TIMEOUT=60,
    CUSTOM_BROWSER_PATH="", AUTO_CLOSE_BROWSER=True, DISABLE_SSL_VERIFY=False,
    CRAWLER_MAX_NOTES_COUNT=15,
)


def make_crawler():
    """旧 __new__ 用例允许在构造后 patch 测试配置；生产构造始终冻结。"""
    crawler = ZhihuCrawler(*_zhihu_dependencies(config))
    crawler.settings = config
    return crawler
