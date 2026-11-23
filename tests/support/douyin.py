"""抖音迁入用例的显式装配；生产实现保持无 fork 依赖，测试装配也不再加载 fork。"""

from dataclasses import replace
from types import SimpleNamespace

from trippostcollect.application.worker_inputs import worker_config
from trippostcollect.platforms import entry
from trippostcollect.platforms.douyin import core, parser
from trippostcollect.platforms.douyin.client import DouYinClient as Client


# 根配置对象 worker_config()。
config = worker_config()
# 迁入用例仍按原样 monkeypatch 这个 fork 数量键；根装配不读取它，只补 fork 默认值供 setattr。
config.CRAWLER_MAX_NOTES_COUNT = 15


def make_crawler():
    return core.DouYinCrawler(**entry.douyin_dependencies(config))


def make_client(**kwargs):
    return Client(ports=entry.douyin_dependencies(config)["ports"].client, **kwargs)


def attach_search_capture(crawler, search):
    """把只实现 ``search_info_by_keyword`` 的替身挂到截获通道上。"""

    class _Capture:
        last_search_visit_id = ""
        last_search_http_status = 200

        def begin_search(self) -> None:
            return None

        async def search_info_by_keyword(self, **kwargs):
            return await search.search_info_by_keyword(**kwargs)

        def record_search_page(self, payload, *, page_number, http_status):
            del payload, page_number, http_status
            return ""

    crawler.capture = _Capture()
    return crawler.capture


def refresh_settings(crawler):
    """旧用例在构造后修改全局配置；迁入后显式替换该测试实例切片。"""
    dependencies = entry.douyin_dependencies(config)
    crawler.settings = dependencies["settings"]


def install_detail_passthrough(crawler, *, with_profile: bool = True):
    """搜索单测不走浮层。补上作者资料，供粉丝量与主页字段判定。"""

    class _Pages:
        calls: list[str] = []

        async def open_from_search(self, aweme, *, search_visit_id: str = ""):
            del search_visit_id
            self.calls.append(str(aweme.get("aweme_id") or ""))
            item = dict(aweme)
            if with_profile and not item.get("creator_profile"):
                author = item.get("author") if isinstance(item.get("author"), dict) else {}
                item["creator_profile"] = {
                    "status_code": 0,
                    "user": {
                        "uid": author.get("uid"),
                        "sec_uid": author.get("sec_uid"),
                        "follower_count": 7,
                    },
                }
            return item

    crawler.detail_pages = _Pages()
    return crawler.detail_pages


class ProjectionStore:
    """测试捕获出口；投影、时间与写出调用仍走真实 core。"""

    create_store = staticmethod(lambda: entry.douyin_dependencies(config)["ports"].content_sink(""))


async def update_douyin_aweme(aweme_item, *, content_detail_source=parser.DOUYIN_SEARCH_CONTENT_SOURCE):
    crawler = make_crawler()
    crawler.ports = replace(crawler.ports, content_sink=lambda _: ProjectionStore.create_store())
    await crawler.update_douyin_aweme(
        aweme_item,
        content_detail_source=content_detail_source,
        expected_aweme_id=str(aweme_item.get("aweme_id") or ""),
    )


douyin_store = SimpleNamespace(
    _extract_note_image_assets=parser._extract_note_image_assets,
    DouyinStoreFactory=ProjectionStore,
    update_douyin_aweme=update_douyin_aweme,
)

