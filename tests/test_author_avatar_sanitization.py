"""TripPostCollect tests for author avatar sanitization."""

from __future__ import annotations

from trippostcollect.runtime import process as t11_process

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from trippostcollect.records.sanitization import (
    AUTHOR_AVATAR_LOG_REDACTION,
    redact_author_avatar_text,
    sanitize_author_avatar_data,
)


AVATAR_URL = "https://avatar.test/author.jpg"


def test_sanitizer_recursively_removes_aliases_and_exact_duplicate_urls() -> None:
    payload = {
        "avatar_url": AVATAR_URL,
        "author": {
            "name": "作者",
            "profile_url": "https://example.test/author",
            "nested": {"author_avatar": {"url": AVATAR_URL}},
            "copied_image": AVATAR_URL,
        },
        "items": [
            {"avatar": AVATAR_URL, "followers_count": 42},
            AVATAR_URL,
            "正文",
        ],
        "user_avatar": None,
    }

    result = sanitize_author_avatar_data(payload)

    assert result.value == {
        "author": {
            "name": "作者",
            "profile_url": "https://example.test/author",
            "nested": {},
        },
        "items": [{"followers_count": 42}, "正文"],
    }
    assert result.avatar_urls == frozenset({AVATAR_URL})
    assert result.removed_keys == 4
    assert result.removed_values == 2


@pytest.mark.parametrize(
    "alias",
    ["avatar_url", "author_avatar", "author_avatar_url", "avatar", "user_avatar"],
)
def test_all_known_avatar_aliases_are_removed(alias: str) -> None:
    result = sanitize_author_avatar_data({alias: AVATAR_URL, "author_id": "author-1"})

    assert result.value == {"author_id": "author-1"}
    assert result.avatar_urls == frozenset({AVATAR_URL})


def test_only_explicitly_evidenced_url_values_are_removed() -> None:
    body_url = "https://images.test/body.jpg"
    result = sanitize_author_avatar_data(
        {"image_url": body_url, "other": AVATAR_URL, "title": "青岛"},
        known_avatar_urls=[AVATAR_URL],
    )

    assert result.value == {"image_url": body_url, "title": "青岛"}


def test_non_url_avatar_value_does_not_remove_matching_research_text() -> None:
    result = sanitize_author_avatar_data(
        {"avatar": "默认头像", "description": "默认头像", "followers_count": 0}
    )

    assert result.value == {"description": "默认头像", "followers_count": 0}
    assert result.avatar_urls == frozenset()


def test_xhs_serialized_creator_profile_removes_only_mapped_avatar_fields() -> None:
    profile_avatar = "https://sns.example.test/avatar/profile.jpg"
    payload = {
        "creator_profile_json": json.dumps(
            {
                "basicInfo": {
                    "imageb": profile_avatar,
                    "images": [profile_avatar],
                    "nickname": "青岛亲子游作者",
                },
                "author_id": "red-1",
            },
            ensure_ascii=False,
        ),
        "copied_profile_avatar": profile_avatar,
        "followers_count": 42,
    }

    result = sanitize_author_avatar_data(payload)

    assert json.loads(result.value["creator_profile_json"]) == {
        "basicInfo": {"nickname": "青岛亲子游作者"},
        "author_id": "red-1",
    }
    assert result.value == {
        "creator_profile_json": result.value["creator_profile_json"],
        "followers_count": 42,
    }
    assert result.avatar_urls == frozenset({profile_avatar})
    assert result.removed_keys == 2
    assert result.removed_values == 1


def test_unmapped_serialized_json_is_not_interpreted_as_avatar_profile() -> None:
    payload = {
        "other_json": json.dumps({"basicInfo": {"imageb": AVATAR_URL}}),
        "author_id": "author-1",
    }

    assert sanitize_author_avatar_data(payload).value == payload


def test_unstructured_child_output_with_avatar_key_is_discarded_whole() -> None:
    output = (
        'record={"avatar_url": "https://avatar.test/author.jpg"}\n'
        "later duplicate https://avatar.test/author.jpg\n"
    )

    sanitized, changed = redact_author_avatar_text(output)

    assert changed is True
    assert sanitized == AUTHOR_AVATAR_LOG_REDACTION
    assert AVATAR_URL not in sanitized


def test_unstructured_child_output_without_avatar_key_is_unchanged() -> None:
    output = "processed content image https://images.test/body.jpg\n"

    assert redact_author_avatar_text(output) == (output, False)


def test_mediacrawler_run_command_redacts_logs_and_summary_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from scripts import mediacrawler_crawl

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout: int) -> tuple[bytes, bytes]:
            assert timeout == 10
            return (
                f'{{"avatar_url":"{AVATAR_URL}"}}\n'.encode(),
                f"duplicate={AVATAR_URL}\n".encode(),
            )

    monkeypatch.setattr(t11_process, "browser_launch_environment", lambda: {})
    monkeypatch.setattr(
        mediacrawler_crawl.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )

    result = mediacrawler_crawl.run_command(
        ["fake-child"],
        tmp_path,
        10,
        tmp_path / "logs",
    )

    assert (tmp_path / "logs/stdout.log").read_text(encoding="utf-8") == (
        AUTHOR_AVATAR_LOG_REDACTION
    )
    assert AVATAR_URL not in result["stdout_tail"]
    assert AVATAR_URL not in result["stderr_tail"]


def test_mediacrawler_exporter_sanitizes_before_jsonl_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.mediacrawler_export_entrypoint import sanitize_export_item

    monkeypatch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")
    serialized = json.dumps(
        sanitize_export_item(
            {
                "note_id": "note-1",
                "avatar_url": AVATAR_URL,
                "copied": AVATAR_URL,
                "title": "青岛",
            }
        ),
        ensure_ascii=False,
    )
    persisted = json.loads(serialized)
    assert persisted == {"note_id": "note-1", "title": "青岛"}


def test_mediacrawler_export_hook_wraps_writer_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import mediacrawler_export_entrypoint as entrypoint

    class FakeWriter:
        async def write_to_csv(self, item: dict, item_type: str) -> dict:
            return item

        async def write_to_jsonl(self, item: dict, item_type: str) -> dict:
            return item

        async def write_single_item_to_json(self, item: dict, item_type: str) -> dict:
            return item

    fake_tools = types.ModuleType("tools")
    fake_tools.__path__ = []
    fake_writer_module = types.ModuleType("tools.async_file_writer")
    fake_writer_module.AsyncFileWriter = FakeWriter
    monkeypatch.setitem(sys.modules, "tools", fake_tools)
    monkeypatch.setitem(sys.modules, "tools.async_file_writer", fake_writer_module)
    monkeypatch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")

    entrypoint.install_export_hook()
    persisted = asyncio.run(
        FakeWriter().write_to_jsonl(
            {"avatar_url": AVATAR_URL, "copied": AVATAR_URL, "title": "青岛"},
            "contents",
        )
    )

    assert persisted == {"title": "青岛"}


@pytest.mark.parametrize("platform,storage,prefix", [
    ("wb", "weibo", "Weibo"),
    ("dy", "douyin", "Douyin"),
    ("zhihu", "zhihu", "Zhihu"),
    ("xhs", "xhs", "Xhs"),
])
@pytest.mark.parametrize("bridge", ["worker", "legacy"])
def test_worker_store_files_remove_avatar_before_serialization(
    tmp_path: Path, platform: str, storage: str, prefix: str, bridge: str,
) -> None:
    """真实 store 和 writer 落盘；独立解释器隔离旧 hook 的类级修改。"""
    source = Path(__file__).resolve().parents[1]
    code = r'''
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys

from trippostcollect.platforms import entry
from trippostcollect.records.sanitization import AUTHOR_AVATAR_KEYS

platform, storage, prefix, bridge, destination = sys.argv[1:]
entry.configure([
    "--platform", platform, "--lt", "cookie", "--type", "search", "--keywords", "青岛",
    "--get_comment", "false", "--get_sub_comment", "false", "--get_media", "false",
    "--headless", "true", "--save_data_option", "jsonl", "--save_data_path", destination,
    "--start", "1", "--max_concurrency_num", "1", "--enable_ip_proxy", "false",
])
# T12：新入口不再装载 fork；fork store 与 writer 经过渡装载点显式加载。
from trippostcollect.platforms import _fork_bridge
_fork_bridge.install()
if bridge == "worker":
    entry.install_hooks()
else:
    from mediacrawler_export_entrypoint import install_export_hook
    install_export_hook()
assert entry.load_crawler(platform)
import config
config.SAVE_DATA_PATH = destination
config.ENABLE_GET_WORDCLOUD = False
from var import crawler_type_var
crawler_type_var.set("search")
store = importlib.import_module(f"store.{storage}._store_impl")
from tools.async_file_writer import AsyncFileWriter
url = "https://fixture.test/private-photo.jpg"
body_url = "https://fixture.test/body.jpg"
raw = {
    "title": "青岛", "author": {key: url for key in AUTHOR_AVATAR_KEYS},
    "copied": url, "images": [url, body_url], "followers_count": 0,
    "creator_profile_json": json.dumps({"basicInfo": {"imageb": url, "images": [url]}}),
}
expected = {"title": "青岛", "author": {}, "images": [body_url],
            "followers_count": 0, "creator_profile_json": '{"basicInfo":{}}'}
original_dumps = json.dumps
serialized = []
def checked_dumps(value, *args, **kwargs):
    text = original_dumps(value, *args, **kwargs)
    assert url not in text
    assert not any(key in text for key in AUTHOR_AVATAR_KEYS)
    serialized.append(text)
    return text
json.dumps = checked_dumps
async def write():
    await getattr(store, f"{prefix}JsonlStoreImplement")().store_content(raw)
    writer = AsyncFileWriter(storage, "search")
    await writer.write_single_item_to_json(raw, "contents")
    await writer.write_to_csv(raw, "contents")
asyncio.run(write())
assert len(serialized) >= 2
files = list(Path(destination).rglob("search_contents_*"))
assert len(files) == 3
for path in files:
    text = path.read_text(encoding="utf-8-sig")
    assert url not in text
    assert not any(key in text for key in AUTHOR_AVATAR_KEYS)
    assert body_url in text
    if path.suffix == ".jsonl":
        assert json.loads(text) == expected
    elif path.suffix == ".json":
        assert json.loads(text) == [expected]
assert raw["copied"] == url
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(source / "src"), str(source / "scripts")))
    environment["TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS"] = "1"
    from trippostcollect.xhs.batch_checkpoint import ENABLED_ENV
    environment.pop(ENABLED_ENV, None)
    result = subprocess.run(
        [sys.executable, "-c", code, platform, storage, prefix, bridge, str(tmp_path / "files")],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_root_jsonl_writer_sanitizes_without_hook(monkeypatch, tmp_path):
    """直接调用根 writer 也必须在首次写文件前净化。"""
    from trippostcollect.artifacts.jsonl import AsyncFileWriter

    monkeypatch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")
    writer = AsyncFileWriter("weibo", "search", save_data_path=lambda: str(tmp_path))
    asyncio.run(writer.write_to_jsonl(
        {"title": "青岛", "avatar_url": AVATAR_URL, "copied": AVATAR_URL}, "contents",
    ))
    paths = list(tmp_path.rglob("*.jsonl"))
    assert len(paths) == 1
    assert paths[0].read_text() == '{"title": "青岛"}\n'


def test_root_jsonl_sanitizer_failure_precedes_any_file_operation(monkeypatch, tmp_path):
    from trippostcollect.artifacts.jsonl import AsyncFileWriter

    monkeypatch.delenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", raising=False)
    destination = tmp_path / "uncreated"
    writer = AsyncFileWriter("xhs", "search", save_data_path=lambda: str(destination))
    with pytest.raises(RuntimeError, match="export sanitizer is not enabled"):
        asyncio.run(writer.write_to_jsonl({"avatar_url": AVATAR_URL}, "contents"))
    assert not destination.exists()
