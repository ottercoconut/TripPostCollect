"""#163 有意偏离：抖音 jsonl 内容记录末尾增加由作者 ``sec_uid`` 拼出的 ``author_profile_url``。

固化文件不改、不重新生成（见 ``support.legacy_expectations``）。T06 drive 的作品作者对象都带
``sec_uid="sec-author"``，作者资料没有简介与认证候选，所以每条内容记录只在末尾多一个主页链接键；
图片清单、事件与请求不变。
"""

from __future__ import annotations

import json
from typing import Any

from support.douyin_116_deviation import DETAIL_CONTENTS_PREFIX, SEARCH_CONTENTS_PREFIX

DRIVE_PROFILE_URL = "https://www.douyin.com/user/sec-author"


def _content_lines(name: str, content: bytes) -> bytes:
    rows = []
    for line in content.decode("utf-8").splitlines():
        record = json.loads(line)
        assert "author_profile_url" not in record, name
        assert record["creator_hash"] == "author", name
        record["author_profile_url"] = DRIVE_PROFILE_URL
        rows.append(json.dumps(record, ensure_ascii=False) + "\n")
    return "".join(rows).encode("utf-8")


def douyin_drive(expected: dict[str, Any]) -> dict[str, Any]:
    """T06 drive 预期：只改搜索与 detail 内容 JSONL。"""
    files = {
        name: _content_lines(name, content)
        if name.startswith((SEARCH_CONTENTS_PREFIX, DETAIL_CONTENTS_PREFIX)) else content
        for name, content in expected["files"].items()
    }
    return {**expected, "files": files}
