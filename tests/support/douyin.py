"""抖音迁入用例的显式装配；生产实现保持无 fork 依赖。"""

from dataclasses import replace
from types import SimpleNamespace

from trippostcollect.platforms import entry
from trippostcollect.platforms.douyin import core, parser
from trippostcollect.platforms.douyin.client import DouYinClient as Client


entry._fork_bridge.install()
config = entry.import_module("config")


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


async def update_legacy_comment(aweme_id, comment_item):
    """评论属 T12 退出项；只为原隐私断言保留旧投影入口。"""
    from store import douyin as legacy

    original = legacy.DouyinStoreFactory.create_store
    legacy.DouyinStoreFactory.create_store = ProjectionStore.create_store
    try:
        await legacy.update_dy_aweme_comment(aweme_id, comment_item)
    finally:
        legacy.DouyinStoreFactory.create_store = original


douyin_store.update_dy_aweme_comment = update_legacy_comment
