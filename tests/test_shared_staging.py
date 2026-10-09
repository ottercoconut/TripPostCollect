"""四站暂存和内容出口与固定 Git 基线逐字节对照；不启动抓取。"""

from __future__ import annotations

import ast
import asyncio
from hashlib import sha256
import importlib
import importlib.util
import inspect
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from PIL import Image
import pytest

from support import legacy_expectations as expectations
from support import platform_session_deviation as deviation
from trippostcollect.application.contracts import ContentSink, ImageStager, JsonlWriter
from trippostcollect.artifacts.jsonl import JsonlContentStore


ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {
    "weibo": ("wb", "WeiboStoreImage", "WeiboJsonlStoreImplement", "note_id"),
    "douyin": ("dy", "DouYinImage", "DouyinJsonlStoreImplement", "aweme_id"),
    "zhihu": ("zhihu", "ZhihuStoreImage", "ZhihuJsonlStoreImplement", "content_id"),
    "xhs": ("xhs", "XiaoHongShuImage", "XhsJsonlStoreImplement", "note_id"),
}


def _baseline(name, directory):
    """原类取自 Git show，保留原文；纯源码副本使用同一提交快照。"""
    snapshot = json.loads((ROOT / "tests/golden/shared_staging.json").read_text())
    entry = snapshot["classes"][name]
    source = entry["preamble"] + '''
from __future__ import annotations
from pathlib import Path
from typing import Dict, List
import config
from base.base_crawler import AbstractStore, AbstractStoreImage
from tools import utils
from tools.async_file_writer import AsyncFileWriter
from tools.image_manifest import (
    ImageAsset, failed_manifest_row, stage_post_images, upsert_manifest_rows_atomic,
    weibo_source_asset_key, douyin_source_asset_key, zhihu_source_asset_key,
    xhs_source_asset_key,
)
from trippostcollect.core.paths import MEDIACRAWLER_DIR
from var import crawler_type_var
''' + entry["source"]
    path = directory / f"baseline_{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


def _tree(directory):
    """同时比较目录、相对路径和完整文件字节，不忽略 manifest 或失败行。"""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes() if path.is_file() else None
        for path in directory.rglob("*")
    }


def _raster(kind, color="blue"):
    output = BytesIO()
    Image.new("RGB", (8, 6), color=color).save(output, format=kind)
    return output.getvalue()


async def _images(cls, directory, id_keyword):
    """fork/基线类：经 fork config 设暂存根，经 fork utils.logger 捕获日志。"""
    import config
    from tools import utils

    config.SAVE_DATA_PATH = str(directory)
    return await _stage_images(cls, directory, id_keyword,
                               lambda sink: patch.object(utils.logger, "info", side_effect=sink))


async def _stage_images(make_store, directory, id_keyword, capture_logs):
    """同一组暂存输入；make_store 在暂存根已确定后构造 stager，capture_logs(sink) 捕获 info 日志。"""
    from trippostcollect.application.contracts import ImageStagingError

    store = make_store()
    assert isinstance(store, ImageStager)
    png = _raster("PNG")
    items = [
        {"source_index": "0", "url": "https://pic.example/notes/first.jpg?a=1",
         "pid": " pid-1 ", "uri": " uri-1 ", "content": png, "attempts": "2"},
        {"source_index": 1, "url": "https://pic.example/notes/second.png?a=2",
         "content": _raster("JPEG"), "attempts": 0, "http_status": "200"},
        {"source_index": 2, "url": "https://pic.example/notes/first.jpg?a=3",
         "content": png, "http_status": 0},
    ]
    returned, errors, logs = [], [], []

    async def stage(post_id, content):
        return await store.store_post_images(**{
            id_keyword: post_id, "image_content_items": content,
        })

    async def fail(post_id, content):
        return await store.record_failure(**{
            id_keyword: post_id, "image_content_item": content,
        })

    with capture_logs(logs.append):
        assert await stage("empty", []) == []
        assert not directory.exists()
        for post_id in ("success", "青岛/路径"):
            rows = await stage(post_id, items)
            returned.append(rows)
            before = _tree(directory)
            assert await stage(post_id, items) == rows
            assert _tree(directory) == before
            assert rows[0]["sha256"] == rows[2]["sha256"]
            assert len(rows) == 3
        # 后一图片拒绝时不能留下前一图片的部分目录。
        for label, content, code in (
            ("unsupported", b"<svg></svg>", "image_non_raster_response"),
            ("broken", png[:len(png) // 2], "image_decode_failed"),
        ):
            bad = dict(items[1], content=content)
            with pytest.raises(ImageStagingError) as caught:
                await stage(label, [items[0], bad])
            assert caught.value.code == code
            assert caught.value.source_index == 1
            errors.append((type(caught.value).__name__, str(caught.value), code, 1))
            assert not (store.image_store_path / label).exists()
            returned.append(await fail(label, dict(bad, error_code=code)))
        for index, (code, status) in enumerate((
            (None, None), ("image_download_retryable", 503),
            ("image_source_unavailable", 404), ("image_auth_required", 403),
            ("image_rate_limited", 429),
        )):
            returned.append(await fail(f"download-{index}", dict(
                items[0], attempts=3, error_code=code, http_status=status,
            )))
        # 既有目录冲突必须原样向调用方抛出，manifest 不得被改成成功。
        before = _tree(directory)
        with pytest.raises(ImageStagingError) as caught:
            await stage("success", [dict(items[0], content=_raster("PNG", "red"))])
        assert caught.value.code == "image_existing_conflict"
        assert _tree(directory) == before
        errors.append((type(caught.value).__name__, str(caught.value), caught.value.code))
    assert not any(".part" in path for path in _tree(directory))
    return returned, errors, logs


async def _jsonl(cls, directory):
    import config
    from tools.utils import utils
    from var import crawler_type_var

    config.SAVE_DATA_PATH = str(directory)
    token = crawler_type_var.set("search")
    try:
        sink = cls()
        assert isinstance(sink, ContentSink)
        writer = sink.file_writer if hasattr(sink, "file_writer") else sink.writer
        assert isinstance(writer, JsonlWriter)
        assert writer.crawler_type == "search"
        avatar = "https://image.example/avatar.jpg"
        item = {
            "id": "青岛-1", "text": "青岛图文\n正文", "author": {
                "name": "作者", "avatar_url": avatar, "duplicate": avatar,
                "followers_count": 12,
            }, "images": [avatar, "https://image.example/body.jpg"],
        }
        with patch.object(utils, "get_current_date", return_value="2026-09-30"):
            assert await sink.store_content(item) is None
            assert await sink.store_content(item) is None
            payload = next(directory.glob("*/jsonl/*.jsonl")).read_bytes()
            assert len(payload.splitlines()) == 2
            assert avatar.encode() not in payload
            assert b"avatar_url" not in payload
            assert "作者".encode() in payload
            # 原构造时捕获 crawler_type；目录和日期在写出时读取。
            crawler_type_var.set("detail")
            config.SAVE_DATA_PATH = str(directory / "changed")
            with patch.object(utils, "get_current_date", return_value="2026-10-01"):
                await sink.store_content(item)
            await sink.store_comment(item)
            await sink.store_creator(item)
        if hasattr(sink, "flush"):
            assert sink.flush() is None
        assert item["author"]["avatar_url"] == avatar
    finally:
        crawler_type_var.reset(token)


def _exercise(platform, directory):
    """真实装配四站入口，构造 store；不构造 crawler 实例或调用 start。"""
    from trippostcollect.platforms.entry import configure, install_hooks, load_crawler

    code, image_name, jsonl_name, id_keyword = PLATFORMS[platform]
    commands = json.loads((ROOT / "tests/golden/t02_worker_commands.json").read_text())
    scenario = {"weibo": "weibo_search", "douyin": "douyin_search_discovery",
                "zhihu": "zhihu_search", "xhs": "xhs_search_qrcode"}[platform]
    argv = [part.replace("<TMP>", str(directory)) for part in commands[scenario]["cmd"][4:]]
    configure(argv)
    # T12：新入口不再装载 fork；旧桥 store 对照经过渡装载点显式加载。
    from trippostcollect.platforms import _fork_bridge
    _fork_bridge.install()
    install_hooks()
    crawler = load_crawler(code)
    assert inspect.isclass(crawler)
    image_cls = getattr(importlib.import_module(f"store.{platform}.{platform}_store_media"), image_name)
    jsonl_cls = getattr(importlib.import_module(f"store.{platform}._store_impl"), jsonl_name)
    baseline_image = _baseline(image_name, directory)
    baseline_jsonl = _baseline(jsonl_name, directory)
    for new, old, methods in (
        (image_cls, baseline_image, ("__init__", "store_post_images", "record_failure")),
        (jsonl_cls, baseline_jsonl, ("__init__", "store_content", "store_comment", "store_creator")),
    ):
        for method in methods:
            # 原模块的 future import 不统一；参数名称、种类与默认值才是调用契约。
            def parameters(cls):
                return [
                    (p.name, p.kind, p.default)
                    for p in inspect.signature(getattr(cls, method)).parameters.values()
                ]
            assert parameters(new) == parameters(old)
    old_root, new_root = directory / "baseline", directory / "new"
    old_images = asyncio.run(_images(baseline_image, old_root, id_keyword))
    assert old_images == asyncio.run(_images(image_cls, new_root, id_keyword))
    old_tree_after_images = _tree(old_root)
    assert old_tree_after_images == _tree(new_root)
    asyncio.run(_jsonl(baseline_jsonl, old_root))
    asyncio.run(_jsonl(jsonl_cls, new_root))
    assert _tree(old_root) == _tree(new_root)
    # 默认目录只检查属性，绝不访问真实目录或写出默认产物。
    import config
    config.SAVE_DATA_PATH = ""
    assert baseline_image().save_data_root == image_cls().save_data_root
    # T14：基线类（Git 快照原文）的结果另行写出，供守卫与固化预期逐字节比较。
    legacy_view = expectations.scrub({
        "images": old_images, "tree_after_images": old_tree_after_images,
        "tree_after_jsonl": _tree(old_root), "default_save_data_root": str(baseline_image().save_data_root),
    }, (directory, "<TMP>"))
    (directory / "legacy_view.json").write_text(expectations.dumps(legacy_view), encoding="utf-8")
    result = {
        "platform": platform, "crawler": crawler.__name__, "start_called": False,
        "files": {path: sha256(data).hexdigest() for path, data in _tree(new_root).items() if data is not None},
    }
    (directory / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"platform": platform, "files": len(result["files"]), "equal": True}))


@expectations.legacy_only
@pytest.mark.parametrize("platform", PLATFORMS)
def test_four_platforms_match_baseline_after_entry_setup(platform, tmp_path):
    _run_exercise(platform, tmp_path)


def _run_exercise(platform, tmp_path):
    # 子进程避免 fork config/tools 包污染其他根测试；只绑定当前副本的根 src 与测试辅助。
    env = dict(
        os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests"))),
        PYTHONDONTWRITEBYTECODE="1", TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS="1",
    )
    program = (
        "import runpy, sys; from pathlib import Path; "
        "runpy.run_path(sys.argv[1])['_exercise'](sys.argv[2], Path(sys.argv[3]))"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", program, str(Path(__file__).resolve()), platform, str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-6000:]
    assert json.loads(result.stdout.strip().splitlines()[-1])["equal"] is True


@expectations.legacy_guard
@pytest.mark.parametrize("platform", PLATFORMS)
def test_t14_guard_baseline_staging(platform, tmp_path, pytestconfig):
    """Git 基线类在 fork 包上的当场结果与固化预期逐字节一致；根侧比较见下方根用例。"""
    _run_exercise(platform, tmp_path)
    view = expectations.decode(json.loads((tmp_path / "legacy_view.json").read_text(encoding="utf-8")))
    expectations.check_legacy(
        pytestconfig, *T14_STAGING, platform, view,
        source_test="tests/test_shared_staging.py::test_four_platforms_match_baseline_after_entry_setup",
    )


T14_STAGING = ("shared_staging", "baseline_classes")
# T12 退出切片：根内容出口不再有评论/创作者写出；基线这两类文件不参与根侧比较。
EXITED_ITEM_TYPES = ("_comments_", "_creators_")


def _root_config(platform, save_data_path):
    from trippostcollect.application.worker_inputs import worker_config

    config = worker_config()
    config.PLATFORM, config.CRAWLER_TYPE, config.SAVE_DATA_PATH = PLATFORMS[platform][0], "search", save_data_path
    return config


def _root_image_stager(platform, save_data_path):
    """经根入口的装配函数取得 stager，参数（平台键、来源键、资产键、日志）与正式 worker 相同。"""
    from trippostcollect.platforms import entry

    config = _root_config(platform, save_data_path)
    if platform == "weibo":
        return entry.weibo_dependencies(config)[1].image_stager()
    if platform == "douyin":
        return entry.douyin_dependencies(config)["ports"].image_stager()
    if platform == "zhihu":
        return entry._zhihu_dependencies(config)[1].image_stager_factory()
    return entry.xhs_dependencies(config, repair=False)["ports"].image_stager_factory()


def _root_content_sink(platform, save_data_path):
    from trippostcollect.platforms import entry

    config = _root_config(platform, save_data_path)
    if platform == "weibo":
        return entry.weibo_dependencies(config)[1].store_factory()
    if platform == "douyin":
        return entry.douyin_dependencies(config)["ports"].content_sink("search")
    if platform == "zhihu":
        return entry._zhihu_dependencies(config)[1].content_sink_factory()
    return entry.xhs_dependencies(config, repair=False)["ports"].content_sink_factory("search")


async def _root_jsonl(platform, directory, monkeypatch):
    """与 _jsonl 同一输入与断言；根配置在构造时冻结（T12），换目录即按新目录构造新 sink。"""
    from trippostcollect.artifacts import jsonl

    date = ["2026-09-30"]
    monkeypatch.setattr(jsonl.time, "strftime", lambda *args: date[0])
    sink = _root_content_sink(platform, str(directory))
    assert isinstance(sink, ContentSink)
    writer = sink.file_writer if hasattr(sink, "file_writer") else sink.writer
    assert isinstance(writer, JsonlWriter)
    assert writer.crawler_type == "search"
    avatar = "https://image.example/avatar.jpg"
    item = {
        "id": "青岛-1", "text": "青岛图文\n正文", "author": {
            "name": "作者", "avatar_url": avatar, "duplicate": avatar,
            "followers_count": 12,
        }, "images": [avatar, "https://image.example/body.jpg"],
    }
    assert await sink.store_content(item) is None
    assert await sink.store_content(item) is None
    payload = next(directory.glob("*/jsonl/*.jsonl")).read_bytes()
    assert len(payload.splitlines()) == 2
    assert avatar.encode() not in payload
    assert b"avatar_url" not in payload
    assert "作者".encode() in payload
    date[0] = "2026-10-01"
    await _root_content_sink(platform, str(directory / "changed")).store_content(item)
    assert not hasattr(sink, "store_comment") and not hasattr(sink, "store_creator")
    if hasattr(sink, "flush"):
        assert sink.flush() is None
    assert item["author"]["avatar_url"] == avatar


@pytest.mark.parametrize("platform", PLATFORMS)
def test_root_staging_matches_frozen_baseline(platform, tmp_path, monkeypatch):
    """T14：根暂存与内容出口经根入口装配，与固化的 Git 基线类结果比较；不加载 fork。"""
    import logging

    # 与基线子进程相同的正式 worker 开关：写出前净化作者头像。
    monkeypatch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")
    expected = expectations.load(*T14_STAGING, platform)
    root = tmp_path / "baseline"
    images = asyncio.run(_stage_images(
        lambda: _root_image_stager(platform, str(root)), root, "platform_post_id",
        lambda sink: patch.object(logging.getLogger("MediaCrawler"), "info", side_effect=sink),
    ))
    assert expectations.scrub(images, (tmp_path, "<TMP>")) == expected["images"]
    assert _tree(root) == expected["tree_after_images"]
    asyncio.run(_root_jsonl(platform, root, monkeypatch))
    retained = {name: data for name, data in expected["tree_after_jsonl"].items()
                if not any(kind in name for kind in EXITED_ITEM_TYPES)}
    assert _tree(root) == retained
    # #59 有意偏离：根 worker 缺省暂存根不再回落 fork 数据目录。钉住被偏离的旧值，并断言根侧显式失败。
    assert expected["default_save_data_root"] == deviation.LEGACY_DEFAULT_SAVE_DATA_ROOT
    with pytest.raises(RuntimeError, match=deviation.SAVE_DATA_PATH_REQUIRED):
        _root_image_stager(platform, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("写出失败"), asyncio.CancelledError()])
async def test_content_sink_preserves_failure_and_cancellation(error):
    class Writer:
        async def write_to_jsonl(self, item, item_type):
            raise error

    with pytest.raises(type(error)) as caught:
        await JsonlContentStore(Writer()).store_content({"text": "青岛"})
    assert caught.value is error


def test_shared_artifacts_do_not_import_platforms_or_fork():
    for name in ("jsonl", "image_staging"):
        tree = ast.parse((ROOT / f"src/trippostcollect/artifacts/{name}.py").read_text())
        modules = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        modules += [alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names]
        assert not any(module and module.startswith((
            "tools", "store", "config", "media_platform", "trippostcollect.platforms",
        )) for module in modules)
