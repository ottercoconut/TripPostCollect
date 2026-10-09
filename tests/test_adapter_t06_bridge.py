"""T06 第二轮：旧桥装配共用根实现，并复用固定基线的全部场景。"""

import importlib
import importlib.util
from collections import Counter
from dataclasses import replace
from pathlib import Path
import sys
from types import ModuleType

import pytest

from trippostcollect.platforms import _fork_bridge, entry
from trippostcollect.platforms.douyin import client, core, login, login_support, parser
from support import legacy_expectations as expectations
from support import platform_session_deviation as deviation
from test_adapter_t06 import SCENARIOS, T14_BRIDGE, current_douyin_profile, drive


# T14：本文件只做旧桥（fork 工厂/E）与根的双轨对照或旧桥自测，T14-C 随旧桥整体删除。
pytestmark = list(expectations.legacy_only_marks())


ROOT = Path(__file__).resolve().parents[1]


def fork_factory():
    """加载真实 fork main.py，但不运行其入口或构造其他平台。"""
    _fork_bridge.install()
    spec = importlib.util.spec_from_file_location(
        "t06_fork_main", ROOT / "tools/MediaCrawler/main.py",
    )
    module = importlib.util.module_from_spec(spec)
    # 根环境已退出 Typer、旧 DB 与 B站 video/快手/贴吧；只替换未调用的入口依赖。
    # 抖音导入、CrawlerFactory 定义与 create_crawler 均执行 fork 原文件。
    with pytest.MonkeyPatch.context() as patch:
        for name, class_name in (
            ("cmd_arg", None), ("database.db", None),
            ("media_platform.bilibili", "BilibiliCrawler"),
            ("media_platform.kuaishou", "KuaishouCrawler"),
            ("media_platform.tieba", "TieBaCrawler"),
        ):
            stub = ModuleType(name)
            if class_name:
                setattr(stub, class_name, type(class_name, (), {}))
            patch.setitem(sys.modules, name, stub)
        spec.loader.exec_module(module)
    return module.CrawlerFactory


async def drive_assembly(tmp_path, patch, scenario, fallback, *, old_bridge):
    factory = fork_factory()
    legacy_class = importlib.import_module("media_platform.douyin").DouYinCrawler
    new_class = entry.load_crawler("dy")
    root_class = core.DouYinCrawler
    assert factory.CRAWLERS["dy"] is legacy_class
    assert issubclass(legacy_class, root_class)
    assert issubclass(new_class, root_class)
    assert legacy_class.start is new_class.start is root_class.start

    def construct(**dependencies):
        # drive 提供同一套 fake；只替换装配结果，不替换任何业务方法。
        patch.setattr(entry, "_douyin_browser_detail_fallback", False)
        if old_bridge:
            repair = importlib.import_module("mediacrawler_export_entrypoint")
            repair.install_douyin_browser_detail_fallback()
        else:
            entry.install_hooks()
        assert entry._douyin_browser_detail_fallback is bool(fallback)
        dependencies["ports"] = replace(
            dependencies["ports"],
            browser_detail_fallback=entry._douyin_browser_detail_fallback,
        )
        patch.setattr(entry, "douyin_dependencies", lambda config: dependencies)
        crawler = factory.create_crawler("dy") if old_bridge else new_class()
        assert type(crawler) is (legacy_class if old_bridge else new_class)
        return crawler

    # 原对照 drive 保持逐字不变；在其构造接缝注入两条真实入口。
    patch.setattr(core, "DouYinCrawler", construct)
    return await drive(None, tmp_path, patch, scenario, fallback)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [0, 1])
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_fork_factory_matches_new_entry(tmp_path, monkeypatch, scenario, fallback):
    with monkeypatch.context() as patch:
        old = await drive_assembly(tmp_path / "old", patch, scenario, fallback, old_bridge=True)
    with monkeypatch.context() as patch:
        new = await drive_assembly(tmp_path / "new", patch, scenario, fallback, old_bridge=False)
    assert old == new
    assert new["error"] is None or scenario == "login_expired", new["error"]


@expectations.legacy_guard
@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [0, 1])
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_t14_guard_fork_factory_drive(tmp_path, monkeypatch, scenario, fallback):
    """旧桥 fork 工厂当场结果与登记 #59 偏离后的固化预期相等；根侧比较见 test_adapter_t06.py。"""
    with monkeypatch.context() as patch:
        old = await drive_assembly(tmp_path / "old", patch, scenario, fallback, old_bridge=True)
    # fork 工厂构造的是根 crawler 子类：#59 后 profile 已在新位置，按与根侧同一偏离登记比较；
    # 固化文件只由冻结 fixture 路径再生成，本守卫不写出。
    assert expectations.scrub(old, (tmp_path, "<TMP>")) == deviation.douyin_profile(
        expectations.load(*T14_BRIDGE, f"{scenario}-fallback{fallback}"), current_douyin_profile(tmp_path))


def test_fork_exports_root_implementations_and_injection_only():
    factory = fork_factory()
    assert issubclass(factory.CRAWLERS["dy"], core.DouYinCrawler)
    old_client = importlib.import_module("media_platform.douyin.client").DouYinClient
    old_login = importlib.import_module("media_platform.douyin.login").DouYinLogin
    assert issubclass(old_client, client.DouYinClient)
    assert issubclass(old_login, login.DouYinLogin)
    for thin_class in (old_client, old_login):
        assert {name for name, value in vars(thin_class).items() if callable(value)} == {"__init__"}
    assert importlib.import_module("cache.abs_cache").AbstractCache is login_support.AbstractCache
    assert importlib.import_module("cache.local_cache").ExpiringLocalCache is login_support.ExpiringLocalCache
    assert importlib.import_module("tools.slider_util").Slide is login_support.Slide
    assert importlib.import_module("tools.easing").get_tracks is login_support.get_easing_tracks
    assert importlib.import_module("store.douyin")._extract_note_image_assets is parser._extract_note_image_assets
    repair = importlib.import_module("mediacrawler_export_entrypoint")
    assert repair._find_douyin_detail is parser._find_douyin_detail
    assert repair._douyin_detail_urls is client._douyin_detail_urls


@pytest.mark.parametrize("fallback", [0, 1])
def test_old_hook_latches_at_install_and_injects_client(monkeypatch, fallback):
    _fork_bridge.install()
    repair = importlib.import_module("mediacrawler_export_entrypoint")
    old_client = importlib.import_module("media_platform.douyin.client").DouYinClient
    monkeypatch.setattr(entry, "_douyin_browser_detail_fallback", False)
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_FALLBACK", str(fallback))
    method = old_client.get_video_by_id
    repair.install_douyin_browser_detail_fallback()
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_FALLBACK", str(1 - fallback))
    value = old_client(headers={}, playwright_page=None, cookie_dict={})
    assert value.browser_detail_fallback is bool(fallback)
    assert old_client.get_video_by_id is method is client.DouYinClient.get_video_by_id
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_FALLBACK", "0")
    repair.install_douyin_browser_detail_fallback()
    assert entry._douyin_browser_detail_fallback is bool(fallback)


def test_t06_progress_only_keeps_required_split_adapters():
    ledger = importlib.import_module("adapter_ledger")
    report = ledger.build_progress(ROOT)
    assert report["missing"] == []
    rows = [row for row in report["rows"] if row["card"] == "T06"]
    assert Counter(row["state"] for row in rows) == {"moved": 105, "pending": 7, "exited": 2}
    pending = {(row["file"], row["qualname"]) for row in rows if row["state"] == "pending"}
    assert pending == {
        ("scripts/mediacrawler_export_entrypoint.py", "install_douyin_browser_detail_fallback"),
        ("tools/MediaCrawler/store/douyin/__init__.py", "update_dy_aweme_images"),
        ("tools/MediaCrawler/store/douyin/__init__.py", "record_dy_aweme_image_failure"),
        ("tools/MediaCrawler/store/douyin/_store_impl.py", "DouyinJsonlStoreImplement"),
        ("tools/MediaCrawler/store/douyin/_store_impl.py", "DouyinJsonlStoreImplement.__init__"),
        ("tools/MediaCrawler/store/douyin/douyin_store_media.py", "DouYinImage"),
        ("tools/MediaCrawler/store/douyin/douyin_store_media.py", "DouYinImage.__init__"),
    }
    assert all(row["disposition"] == "拆" for row in rows if row["state"] == "pending")
