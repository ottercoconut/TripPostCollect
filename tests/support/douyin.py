"""抖音迁入用例的显式装配；生产实现保持无 fork 依赖，测试装配也不再加载 fork。"""

import ast
from dataclasses import replace
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List

from trippostcollect.application.worker_inputs import worker_config
from trippostcollect.platforms import entry
from trippostcollect.platforms.douyin import core, parser
from trippostcollect.platforms.douyin.client import DouYinClient as Client
from trippostcollect.records.identity import anonymize_user_id, mask_nickname
from trippostcollect.runtime import helpers


# T14：根配置对象取代 fork config；与 fork 默认值逐键相同由 test_adapter_t12 的配置守护证明。
config = worker_config()
# 迁入用例仍按原样 monkeypatch 这个 fork 数量键；根装配不读取它，只补 fork 默认值供 setattr。
config.CRAWLER_MAX_NOTES_COUNT = 15
FROZEN_STORE = Path(__file__).resolve().parents[1] / "fixtures/adapter_t06/store/douyin/__init__.py.txt"


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


def _frozen_comment_projection():
    """评论属 T12 退出项；只为原隐私断言从冻结 T06 fixture 执行旧投影（与 weibo_privacy 同一做法）。

    fixture 中 update_dy_aweme_comment 与 _extract_comment_image_list 和 fork 3488cf2 原文 AST 相同；
    写出口经 ProjectionStore（测试再替换为 fake），不加载 fork store。
    """
    names = {"update_dy_aweme_comment", "_extract_comment_image_list"}
    source = ast.parse(FROZEN_STORE.read_text(encoding="utf-8"))
    body = [node for node in source.body if getattr(node, "name", "") in names]
    assert {node.name for node in body} == names
    namespace = {
        "Dict": Dict, "List": List, "anonymize_user_id": anonymize_user_id, "mask_nickname": mask_nickname,
        "DouyinStoreFactory": ProjectionStore,
        "utils": SimpleNamespace(get_current_timestamp=helpers.get_current_timestamp,
                                 logger=logging.getLogger("MediaCrawler")),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), "旧抖音评论投影", "exec"), namespace)
    return namespace["update_dy_aweme_comment"]


douyin_store.update_dy_aweme_comment = _frozen_comment_projection()
