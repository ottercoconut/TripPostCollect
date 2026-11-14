"""四站正文图暂存与 JSONL 内容出口经根入口装配运行：目录布局、manifest 行、拒绝与冲突；不启动抓取。"""

from __future__ import annotations

import ast
import asyncio
from hashlib import sha256
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image, UnidentifiedImageError
import pytest

from support.stable_png import solid_png
from trippostcollect.application.contracts import ContentSink, ImageStager, JsonlWriter
from trippostcollect.artifacts.jsonl import JsonlContentStore


ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {
    "weibo": ("wb", "WeiboStoreImage", "WeiboJsonlStoreImplement", "note_id"),
    "douyin": ("dy", "DouYinImage", "DouyinJsonlStoreImplement", "aweme_id"),
    "zhihu": ("zhihu", "ZhihuStoreImage", "ZhihuJsonlStoreImplement", "content_id"),
    "xhs": ("xhs", "XiaoHongShuImage", "XhsJsonlStoreImplement", "note_id"),
}


def _tree(directory):
    """同时比较目录、相对路径和完整文件字节，不忽略 manifest 或失败行。"""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes() if path.is_file() else None
        for path in directory.rglob("*")
    }


# 截断后的 PNG：签名 8 字节 + IHDR 块 25 字节 = 33，再留 IDAT 块 8 字节头中的 4 字节，截断落在 IDAT 头内。
# 签名可识别而 Image.open 抛 UnidentifiedImageError，命中 inspect_image_bytes 的
# “recognized raster image could not be decoded” 分支（与原 Pillow 压缩 PNG 截一半时相同）。
# 不能用 len(png)//2：手工 PNG 截一半时 IHDR 完整、open 成功，会改走 verify() 的 payload 分支。
TRUNCATED_PNG_BYTES = 37


def _truncated_png(png):
    content = png[:TRUNCATED_PNG_BYTES]
    with pytest.raises(UnidentifiedImageError):
        Image.open(BytesIO(content))
    return content


def _raster(kind, color="blue"):
    if kind == "PNG":
        # 与主机无关的纯色 PNG（见 support/stable_png.py；Pillow 压缩字节随 CPU 不同）。
        return solid_png(8, 6, {"blue": (0, 0, 255), "red": (255, 0, 0)}[color])
    output = BytesIO()
    Image.new("RGB", (8, 6), color=color).save(output, format=kind)
    return output.getvalue()


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
            ("broken", _truncated_png(png), "image_decode_failed"),
        ):
            bad = dict(items[1], content=content)
            with pytest.raises(ImageStagingError) as caught:
                await stage(label, [items[0], bad])
            assert caught.value.code == code
            assert caught.value.source_index == 1
            if label == "broken":
                # 截断须命中“签名可识别但 open 失败”的分支，而不是 verify() 的 payload 分支。
                assert "recognized raster image could not be decoded" in str(caught.value)
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


# 平台 → (manifest 的来源键, 第一张图的资产键)。资产键优先取平台图片 ID，否则按平台规则归一 URL。
SOURCE_KEYS = {
    "weibo": ("image_list", "weibo:pid:pid-1"),
    "douyin": ("note_download_url", "douyin:uri:uri-1"),
    # 知乎按去掉协议与查询串后的地址取散列。
    "zhihu": ("image_list", "zhihu:urlsha256:" + sha256(b"pic.example/notes/first.jpg").hexdigest()),
    "xhs": ("image_list", "xhs:path:/notes/first.jpg"),
}


@pytest.mark.parametrize("platform", PLATFORMS)
def test_root_staging_and_jsonl(platform, tmp_path, monkeypatch):
    """根暂存与内容出口经根入口装配；正式 worker 开关下写出前净化作者头像。"""
    import logging

    monkeypatch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")
    root = tmp_path / "staging"
    returned, errors, logs = asyncio.run(_stage_images(
        lambda: _root_image_stager(platform, str(root)), root, "platform_post_id",
        lambda sink: patch.object(logging.getLogger("TripPostCollect"), "info", side_effect=sink),
    ))
    # 不安全的帖子 ID 用其散列命名目录；拒绝的帖子不留目录。
    safe = "id-" + sha256("青岛/路径".encode()).hexdigest()
    images = [f"{platform}/images/{post}/{name}" for post in (safe, "success") for name in ("000.png", "001.jpg", "002.png")]
    files = {name for name, data in _tree(root).items() if data is not None}
    assert files == {f"{platform}/image_manifest.jsonl", *images}
    source_key, asset_key = SOURCE_KEYS[platform]
    first = returned[0][0]
    assert {key: first[key] for key in (
        "platform_key", "platform_post_id", "image_role", "source_index", "source_key", "source_asset_key",
        "fetch_status", "attempts", "http_status", "staging_path", "mime_type", "width", "height", "error_code",
    )} == {
        "platform_key": platform, "platform_post_id": "success", "image_role": "content", "source_index": 0,
        "source_key": source_key, "source_asset_key": asset_key, "fetch_status": "downloaded", "attempts": 2,
        "http_status": 200, "staging_path": f"{platform}/images/success/000.png", "mime_type": "image/png",
        "width": 8, "height": 6, "error_code": None,
    }
    assert first["sha256"] == sha256((root / first["staging_path"]).read_bytes()).hexdigest()
    rejected = returned[2]
    assert (rejected["platform_post_id"], rejected["fetch_status"], rejected["staging_path"], rejected["error_code"]) == (
        "unsupported", "failed", None, "image_non_raster_response")
    manifest = [json.loads(line) for line in (root / platform / "image_manifest.jsonl").read_text().splitlines()]
    assert {row["platform_post_id"] for row in manifest} >= {"success", "青岛/路径", "unsupported", "broken"}
    assert [error[2] for error in errors] == ["image_non_raster_response", "image_decode_failed", "image_existing_conflict"]
    assert len(logs) == 5
    asyncio.run(_root_jsonl(platform, root, monkeypatch))
    files_after = {name for name, data in _tree(root).items() if data is not None}
    assert files_after - files == {f"{platform}/jsonl/search_contents_2026-09-30.jsonl",
                                   f"changed/{platform}/jsonl/search_contents_2026-10-01.jsonl"}
    # #59：缺省暂存根不回落到其他数据目录，显式失败。
    with pytest.raises(RuntimeError, match="worker_save_data_path_required"):
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
