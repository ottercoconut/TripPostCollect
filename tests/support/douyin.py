"""抖音迁入用例的显式装配；生产实现保持无 fork 依赖，测试装配也不再加载 fork。"""

from dataclasses import replace
from types import SimpleNamespace

from trippostcollect.application.worker_inputs import worker_config
from trippostcollect.platforms import entry
from trippostcollect.platforms.douyin import core, parser
from trippostcollect.platforms.douyin.client import DouYinClient as Client


# T14：根配置对象取代 fork config；与 fork 默认值逐键相同由 test_adapter_t12 的配置守护证明。
config = worker_config()
# 迁入用例仍按原样 monkeypatch 这个 fork 数量键；根装配不读取它，只补 fork 默认值供 setattr。
config.CRAWLER_MAX_NOTES_COUNT = 15


def make_crawler():
    return core.DouYinCrawler(**entry.douyin_dependencies(config))


def make_client(**kwargs):
    return Client(ports=entry.douyin_dependencies(config)["ports"].client, **kwargs)


def refresh_settings(crawler):
    """旧用例在构造后修改全局配置；迁入后显式替换该测试实例切片。"""
    dependencies = entry.douyin_dependencies(config)
    crawler.settings = dependencies["settings"]
    crawler.inputs = dependencies["inputs"]


class ProjectionStore:
    """测试捕获出口；投影、时间与写出调用仍走真实 core。"""

    create_store = staticmethod(lambda: entry.douyin_dependencies(config)["ports"].content_sink(""))


async def update_douyin_aweme(aweme_item):
    crawler = make_crawler()
    crawler.ports = replace(crawler.ports, content_sink=lambda _: ProjectionStore.create_store())
    await crawler.update_douyin_aweme(aweme_item)


douyin_store = SimpleNamespace(
    _extract_note_image_assets=parser._extract_note_image_assets,
    DouyinStoreFactory=ProjectionStore,
    update_douyin_aweme=update_douyin_aweme,
)

