"""微博离线测试装配与固定旧实现装载，不调用生产 worker。"""

from __future__ import annotations

from dataclasses import fields
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from support.raw_author_identity import use_raw_author_identity
from trippostcollect.platforms import _fork_bridge, entry
from trippostcollect.platforms.weibo import core, models


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/adapter_t05"


def settings(**overrides):
    """只复制静态配置；所有输出由测试的临时目录覆盖。"""
    _fork_bridge.install()
    import config
    from trippostcollect.runtime.browser import CDPBrowserSettings

    keys = {field.name for cls in (models.WeiboConfig, CDPBrowserSettings) for field in fields(cls)}
    result = SimpleNamespace(**{key: getattr(config, key) for key in keys})
    result.CRAWLER_MAX_SLEEP_SEC = 0
    result.SAVE_DATA_OPTION = "jsonl"
    result.CRAWLER_TYPE = "search"
    result.PLATFORM = "wb"
    result.KEYWORDS = "青岛旅游"
    result.COOKIES = ""
    result.__dict__.update(overrides)
    return result


def crawler(config):
    return core.WeiboCrawler(*entry.weibo_dependencies(config))


def client_ports(**overrides):
    from dataclasses import replace
    return replace(entry.weibo_dependencies(settings())[1].client, **overrides)


def load_baseline(tmp_path, monkeypatch):
    """校验所有 fixture 字节后按原包相对导入；共享能力仍用已迁的根实现。"""
    _fork_bridge.install()
    for name in list(sys.modules):
        if name.startswith("t05_legacy_"):
            monkeypatch.delitem(sys.modules, name)
    manifest = json.loads((FIXTURE / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        source = (FIXTURE / (name + ".txt")).read_bytes()
        assert sha256(source).hexdigest() == digest, name
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source)

    def load(name, path, package=False):
        spec = importlib.util.spec_from_file_location(
            name, tmp_path / path,
            submodule_search_locations=[str((tmp_path / path).parent)] if package else None,
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    import tools
    from tools import utils
    old_utils = SimpleNamespace(**vars(utils))
    time_util = load("t05_legacy_time", "tools/time_util.py")
    for name in ("get_current_timestamp", "rfc2822_to_china_datetime", "rfc2822_to_timestamp"):
        setattr(old_utils, name, getattr(time_util, name))
    monkeypatch.setattr(tools, "utils", old_utils)
    old_manifest = load("tools.image_manifest", "tools/image_manifest.py")
    monkeypatch.setattr(tools, "image_manifest", old_manifest, raising=False)
    store = load("t05_legacy_store", "store/weibo/__init__.py", package=True)
    # #49：根实现保存作者原始 ID 与昵称，对照只替换旧 store 的身份转换。
    use_raw_author_identity(monkeypatch, store)
    import store as store_package
    monkeypatch.setattr(store_package, "weibo", store, raising=False)
    package = load("t05_legacy_weibo", "media_platform/weibo/__init__.py", package=True)
    import media_platform
    monkeypatch.setattr(media_platform, "weibo", package, raising=False)
    monkeypatch.setitem(sys.modules, "media_platform.weibo", package)
    bridge = load("t05_legacy_bridge", "mediacrawler_export_entrypoint.py")
    return SimpleNamespace(
        core=sys.modules["t05_legacy_weibo.core"],
        client=sys.modules["t05_legacy_weibo.client"],
        login=sys.modules["t05_legacy_weibo.login"],
        store=store, bridge=bridge, utils=old_utils,
    )
