"""#116 有意偏离：抖音正文来源如实标注、内容记录去掉视频/封面/音乐字段、请求头补齐。

固化文件不改、不重新生成（见 ``support.legacy_expectations``）。与 ``platform_session_deviation`` 同样，
根侧比较对加载的预期做“钉住旧值 → 单向替换为新值”的变换，其余部分仍整体 ``==`` 比较：

1. 图片 CDN 请求原先只有 httpx 默认的 ``host`` 头；现在带与 API 同源的浏览器 UA、
   ``Referer: https://www.douyin.com/`` 与图片 Accept，不带 Cookie（参照上游 0ca7b29）。
2. ``/aweme/v1/web/aweme/detail/`` 请求补 ``x-tt-argus: 1``（参照上游 380b426），其他请求不加。
3. 内容 JSONL 删除 ``cover_url``、``video_download_url``、``music_download_url`` 与
   ``author_followers_zero_suspicious``；搜索路径的 ``content_detail_source`` 由 ``aweme_detail``
   改为 ``search_aweme_info``，detail 路径仍为 ``aweme_detail``。
4. detail 模式取得的空壳（``aweme_id`` 为空）不再写内容记录；文件因此为空时不再生成。
5. 首页重建的每个搜索项带 ``trippostcollect_content_source=aweme_detail`` 来源标记。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit


DRIVE_USER_AGENT = "Mozilla/5.0 Chrome/125.0.0.0"
IMAGE_HOST = "image.test"
DETAIL_PATH = "/aweme/v1/web/aweme/detail/"
MEDIA_HEADERS = (
    ("accept", "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"),
    ("referer", "https://www.douyin.com/"),
    ("user-agent", DRIVE_USER_AGENT),
)
REMOVED_RECORD_KEYS = (
    "cover_url", "video_download_url", "music_download_url", "author_followers_zero_suspicious",
)
SEARCH_CONTENTS_PREFIX = "douyin/jsonl/search_contents_"
DETAIL_CONTENTS_PREFIX = "douyin/jsonl/detail_contents_"
REBUILT_ITEM_SOURCE_KEY = "trippostcollect_content_source"


def _request_row(row: tuple) -> tuple:
    if row[0] != "request":
        return row
    url = urlsplit(row[2])
    headers = list(row[4])
    if url.hostname == IMAGE_HOST:
        assert headers == [("host", IMAGE_HOST)], row
        return (*row[:4], sorted([*headers, *MEDIA_HEADERS]), *row[5:])
    if url.path == DETAIL_PATH:
        assert all(name != "x-tt-argus" for name, _ in headers), row
        return (*row[:4], sorted([*headers, ("x-tt-argus", "1")]), *row[5:])
    assert all(name != "x-tt-argus" for name, _ in headers), row
    return row


def _content_lines(name: str, content: bytes, scenario: str) -> bytes:
    rows = []
    for line in content.decode("utf-8").splitlines():
        record = json.loads(line)
        for key in REMOVED_RECORD_KEYS:
            assert key in record, (name, key)
            del record[key]
        assert record["content_detail_source"] == "aweme_detail", name
        if record["aweme_id"] is None:
            # 只有 detail_shell 无兜底时才写过空壳。
            assert scenario == "detail_shell" and name.startswith(DETAIL_CONTENTS_PREFIX), name
            continue
        if name.startswith(SEARCH_CONTENTS_PREFIX):
            record["content_detail_source"] = "search_aweme_info"
        rows.append(json.dumps(record, ensure_ascii=False) + "\n")
    return "".join(rows).encode("utf-8")


def douyin_drive(expected: dict[str, Any], scenario: str) -> dict[str, Any]:
    """T06 drive 预期：请求头与内容 JSONL 两处偏离；事件与图片字节保持原样比较。"""
    files = {}
    for name, content in expected["files"].items():
        if name.startswith((SEARCH_CONTENTS_PREFIX, DETAIL_CONTENTS_PREFIX)):
            content = _content_lines(name, content, scenario)
            if not content:
                continue
        files[name] = content
    trace = [_request_row(row) if isinstance(row, tuple) else row for row in expected["trace"]]
    return {**expected, "files": files, "trace": trace}


def douyin_first_page_rebuild(expected: Any) -> Any:
    """首页重建预期：完整重建时每项补来源标记；其余情形（None）不变。"""
    result, calls = expected
    if result is None:
        return expected
    for item in result["data"]:
        assert REBUILT_ITEM_SOURCE_KEY not in item, item
    data = [{**item, REBUILT_ITEM_SOURCE_KEY: "aweme_detail"} for item in result["data"]]
    return ({**result, "data": data}, calls)
