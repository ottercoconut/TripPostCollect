"""旧桥与新入口共用根微博实现，并沿用固定基线测试的同一套离线边界。"""

from dataclasses import replace
from importlib import import_module
import runpy
import sys
from types import ModuleType

import pytest

from support.weibo_adapter import ROOT, settings
from test_adapter_t05 import drive
from trippostcollect.platforms import entry
from trippostcollect.platforms.weibo import client, core, login, models, parser


def bridge_types(monkeypatch):
    """只加载工厂定义；不执行 fork main 的抓取入口。"""
    settings()
    fork = import_module("media_platform.weibo")
    fork_core = import_module("media_platform.weibo.core")
    fork_client = import_module("media_platform.weibo.client")
    fork_login = import_module("media_platform.weibo.login")
    fork_models = import_module("media_platform.weibo.exception")
    fork_parser = import_module("media_platform.weibo.help")
    monkeypatch.setattr(fork_core, "_post_repair", False)
    monkeypatch.setattr(entry, "_weibo_post_repair", False)
    # 根环境不安装旧 CLI/ORM 与退出站依赖；这些模块不得参与微博工厂路径。
    for name, symbol in (
        ("cmd_arg", "parse_cmd"), ("database.db", "init_db"),
        ("media_platform.bilibili", "BilibiliCrawler"),
        ("media_platform.kuaishou", "KuaishouCrawler"),
        ("media_platform.tieba", "TieBaCrawler"),
    ):
        module = ModuleType(name)

        def reject(*args, **kwargs):
            pytest.fail("微博工厂不得调用旧 CLI/ORM 或其他站入口")

        setattr(module, symbol, reject)
        monkeypatch.setitem(sys.modules, name, module)
    factory = runpy.run_path(str(ROOT / "tools/MediaCrawler/main.py"))["CrawlerFactory"]
    new_type = entry.load_crawler("wb")
    assert factory.CRAWLERS["wb"] is fork.WeiboCrawler
    assert issubclass(fork.WeiboCrawler, core.WeiboCrawler)
    assert issubclass(new_type, core.WeiboCrawler)
    assert fork_client.WeiboClient is client.WeiboClient
    assert fork_login.WeiboLogin is login.WeiboLogin
    assert fork_models.DataFetchError is models.DataFetchError
    assert fork_parser.filter_search_result_card is parser.filter_search_result_card
    for name in ("start", "search", "get_specified_notes", "get_note_images", "close"):
        assert getattr(fork.WeiboCrawler, name) is getattr(core.WeiboCrawler, name)
    return factory, new_type, fork_core


@pytest.mark.asyncio
@pytest.mark.parametrize("post_repair", [0, 1])
@pytest.mark.parametrize("scenario", [
    "search", "detail", "empty", "search_retry", "search_error", "detail_retry", "detail_error",
    "detail_403", "detail_429", "image_failure", "image_rate_limit", "image_retry", "image_bad_bytes",
    "login_expired", "repair_api", "repair_page", "repair_empty", "repair_navigation", "repair_rate_limit",
])
async def test_fork_factory_matches_new_entry_requests_and_artifacts(tmp_path, scenario, post_repair):
    async def execute(old_bridge, directory):
        with pytest.MonkeyPatch.context() as patch:
            factory, new_type, fork_core = bridge_types(patch)
            bridge = import_module("mediacrawler_export_entrypoint")

            def construct(options, ports):
                # drive 已提供全部 fake；这里只把相同端口传给两个真实无参构造入口。
                patch.setattr(entry, "weibo_dependencies", lambda config, *, post_repair=False: (
                    options, replace(ports, post_repair=post_repair),
                ))
                if old_bridge:
                    bridge.install_weibo_browser_detail_fallback()
                    instance = factory.create_crawler("wb")
                    assert fork_core._post_repair is bool(post_repair)
                else:
                    patch.setattr(bridge, "install_xhs_repair_resilience", lambda: None)
                    patch.setattr(bridge, "install_douyin_browser_detail_fallback", lambda: None)
                    entry.install_hooks()
                    instance = new_type()
                assert instance.ports.post_repair is bool(post_repair)
                assert instance.config is options
                return instance

            # 原 50 项测试与 fixtures 不变，只在本测试作用域接入同一 drive 的构造边界。
            patch.setattr(core, "WeiboCrawler", construct)
            return await drive(None, directory, patch, scenario, post_repair)

    old = await execute(True, tmp_path / "bridge")
    new = await execute(False, tmp_path / "entry")
    assert old == new


def test_legacy_hook_latches_only_at_install_time(monkeypatch):
    factory, new_type, fork_core = bridge_types(monkeypatch)
    bridge = import_module("mediacrawler_export_entrypoint")
    monkeypatch.setenv("TRIPPOSTCOLLECT_POST_REPAIR", "0")
    bridge.install_weibo_browser_detail_fallback()
    assert fork_core._post_repair is False
    monkeypatch.setenv("TRIPPOSTCOLLECT_POST_REPAIR", "1")
    assert fork_core._post_repair is False
    bridge.install_weibo_browser_detail_fallback()
    monkeypatch.setenv("TRIPPOSTCOLLECT_POST_REPAIR", "0")
    bridge.install_weibo_browser_detail_fallback()
    assert factory.create_crawler("wb").ports.post_repair is True
    assert new_type().ports.post_repair is False


def test_legacy_store_uses_root_projection_and_shared_stagers():
    from functools import partial
    from trippostcollect.artifacts.image_staging import PostImageStager
    from trippostcollect.artifacts.jsonl import JsonlContentStore

    settings()
    store = import_module("store.weibo")
    assert isinstance(store.update_weibo_note, partial)
    assert store.update_weibo_note.func is core.WeiboCrawler.update_weibo_note
    for name in ("_first_present", "_weibo_pic_url", "_weibo_pic_urls", "_weibo_pic_assets", "persisted_weibo_content_text"):
        assert getattr(store, name) is getattr(parser, name)
    assert issubclass(store.WeiboJsonlStoreImplement, JsonlContentStore)
    assert issubclass(store.WeiboStoreImage, PostImageStager)
