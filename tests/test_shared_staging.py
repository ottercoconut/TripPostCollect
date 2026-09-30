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
    import config
    from tools import utils
    from trippostcollect.application.contracts import ImageStagingError

    config.SAVE_DATA_PATH = str(directory)
    store = cls()
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

    with patch.object(utils.logger, "info", side_effect=logs.append):
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
    assert asyncio.run(_images(baseline_image, old_root, id_keyword)) == asyncio.run(
        _images(image_cls, new_root, id_keyword),
    )
    assert _tree(old_root) == _tree(new_root)
    asyncio.run(_jsonl(baseline_jsonl, old_root))
    asyncio.run(_jsonl(jsonl_cls, new_root))
    assert _tree(old_root) == _tree(new_root)
    # 默认目录只检查属性，绝不访问真实目录或写出默认产物。
    import config
    config.SAVE_DATA_PATH = ""
    assert baseline_image().save_data_root == image_cls().save_data_root
    result = {
        "platform": platform, "crawler": crawler.__name__, "start_called": False,
        "files": {path: sha256(data).hexdigest() for path, data in _tree(new_root).items() if data is not None},
    }
    (directory / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"platform": platform, "files": len(result["files"]), "equal": True}))


@pytest.mark.parametrize("platform", PLATFORMS)
def test_four_platforms_match_baseline_after_entry_setup(platform, tmp_path):
    # 子进程避免 fork config/tools 包污染其他根测试；只绑定当前副本的根 src。
    env = dict(
        os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1",
        TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS="1",
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
