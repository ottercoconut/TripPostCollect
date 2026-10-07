"""旧 fork 用例的局部配置与装配接缝；不导入 fork，不读取真实配置。"""

from dataclasses import fields, is_dataclass
from types import SimpleNamespace

from trippostcollect.platforms.entry import xhs_dependencies
from trippostcollect.platforms.xhs.core import XiaoHongShuCrawler as RootXiaoHongShuCrawler


# 与原 fork 用例运行时的 base_config/xhs_config 默认值一致，用例再按需 patch。
config = SimpleNamespace(
    PLATFORM="xhs", LOGIN_TYPE="qrcode", COOKIES="", CRAWLER_TYPE="search",
    ENABLE_CDP_MODE=False, CDP_HEADLESS=False, HEADLESS=False, KEYWORDS="编程副业,编程兼职",
    START_PAGE=1, SORT_TYPE="popularity_descending", MAX_CONCURRENCY_NUM=1, ENABLE_GET_MEIDAS=False,
    SAVE_DATA_OPTION="jsonl", SAVE_DATA_PATH="", XHS_INTERNATIONAL=False,
    XHS_SPECIFIED_NOTE_URL_LIST=[
        "https://www.xiaohongshu.com/explore/64b95d01000000000c034587"
        "?xsec_token=AB0EFqJvINCkj6xOCKCQgfNNh8GdnBC_6XecG4QOddo3Q=&xsec_source=pc_cfeed",
    ],
    CDP_CONNECT_EXISTING=False, CDP_DEBUG_PORT=9222, BROWSER_LAUNCH_TIMEOUT=60, CUSTOM_BROWSER_PATH="",
    SAVE_LOGIN_STATE=True, USER_DATA_DIR="%s_user_data_dir", AUTO_CLOSE_BROWSER=True, DISABLE_SSL_VERIFY=False,
    CRAWLER_MAX_NOTES_COUNT=15,
)


def mutable(value):
    """把冻结端口展开为可 patch 的命名空间；嵌套端口同样展开。"""
    if not is_dataclass(value):
        return value
    return SimpleNamespace(**{field.name: mutable(getattr(value, field.name)) for field in fields(value)})


def dependencies():
    resolved = xhs_dependencies(config, repair=False)
    resolved["ports"] = mutable(resolved["ports"])
    return resolved


class XiaoHongShuCrawler(RootXiaoHongShuCrawler):
    """旧用例无参构造；构造后仍可 patch 测试配置与注入端口。"""

    def __init__(self):
        super().__init__(**dependencies())
        self.settings = config


client_ports = dependencies()["ports"].client
login_ports = dependencies()["ports"].login
