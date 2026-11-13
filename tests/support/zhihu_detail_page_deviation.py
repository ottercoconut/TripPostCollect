"""#185 有意偏离：知乎详情改为点击新标签读取页面状态。

固化文件不改。比较前对已经过此前偏离变换的预期再做单向替换：

1. 删除回答/文章详情的 httpx 请求，以及紧挨着的 ``http_client``、``sign``、``compile`` 和请求后的
   ``sleep 0`` / ``sleep 1``。没有剩余签名时去掉 ``compile``。
2. 已观察的回答/文章把点赞、评论、收藏、分享改成详情页的值（7/1/2/0）；``search_content`` 改为
   ``answer_detail`` 或 ``article_detail``。
3. 两侧内容 JSONL 去掉 ``content_visit_refs`` 后按键排序再比较，避免账本序号进入固化字节。
4. 搜索详情 429 的停止细节从 ``rate_limited`` 改为页面上的 ``captcha_detected``。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit


DETAIL_PREFIXES = ("/question/", "/p/", "/zvideo/")

PARSER_AND_MODEL_REPLACEMENTS = (
    (
        '    voteup_count: int = Field(default=0, description="Upvote count")\n'
        '    comment_count: int = Field(default=0, description="Comment count")',
        '    voteup_count: int | None = Field(default=None, description="Upvote count")\n'
        '    comment_count: int | None = Field(default=None, description="Comment count")\n'
        '    collected_count: int | None = Field(default=None, description="Favorite count from detail favlistsCount")\n'
        '    share_count: int | None = Field(default=None, description="Share count from detail reaction.statistics.shareCount")',
    ),
    (
        '    author_desc: str = Field(default="", description="Creator headline or description")\n'
        '    verified_text: str = Field(default="", description="Creator verification text")',
        '    author_desc: str = Field(default="", description="Creator headline or description")\n'
        '    verified_text: str = Field(default="", description="Creator verification text")\n'
        '    content_visit_refs: dict = Field(\n'
        '        default_factory=dict,\n'
        '        description="Detail visit reference prepared for the visit gate",\n'
        '    )',
    ),
    (
        '        res.question_id = str(answer.get("question", {}).get("id") or "")',
        '        question = answer.get("question")\n'
        '        if isinstance(question, dict):\n'
        '            res.question_id = str(question.get("id") or "")\n'
        '        elif question not in (None, ""):\n'
        '            res.question_id = str(question)\n'
        '        else:\n'
        '            res.question_id = ""',
    ),
    (
        '        res.created_time = answer.get("created_time")\n'
        '        res.updated_time = answer.get("updated_time")\n'
        '        res.voteup_count = answer.get("voteup_count", 0)\n'
        '        res.comment_count = answer.get("comment_count", 0)',
        '        res.created_time = _present_int(answer, "created_time", "createdTime")\n'
        '        res.updated_time = _present_int(answer, "updated_time", "updatedTime")\n'
        '        res.voteup_count = _metric(answer, "voteup_count", "voteupCount")\n'
        '        res.comment_count = _metric(answer, "comment_count", "commentCount")\n'
        '        res.collected_count = _metric(answer, "favlists_count", "favlistsCount")\n'
        '        res.share_count = _share_count(answer)',
    ),
    (
        '        res.created_time = article.get("created_time", 0) or article.get("created", 0)\n'
        '        res.updated_time = article.get("updated_time", 0) or article.get("updated", 0)\n'
        '        res.voteup_count = article.get("voteup_count", 0)\n'
        '        res.comment_count = article.get("comment_count", 0)',
        '        res.created_time = _present_int(article, "created_time", "createdTime", "created")\n'
        '        res.updated_time = _present_int(article, "updated_time", "updatedTime", "updated")\n'
        '        res.voteup_count = _metric(article, "voteup_count", "voteupCount")\n'
        '        res.comment_count = _metric(article, "comment_count", "commentCount")\n'
        '        res.collected_count = _metric(article, "favlists_count", "favlistsCount")\n'
        '        res.share_count = _share_count(article)',
    ),
    (
        '        res.voteup_count = zvideo.get("voteup_count")\n'
        '        res.comment_count = zvideo.get("comment_count")',
        '        res.voteup_count = _metric(zvideo, "voteup_count", "voteupCount")\n'
        '        res.comment_count = _metric(zvideo, "comment_count", "commentCount")',
    ),
    (
        '    search_content.image_count = len(search_content.image_list)\n'
        '    search_content.content_detail_status = "detail_observed"',
        '    search_content.image_count = len(search_content.image_list)\n'
        '    search_content.voteup_count = detail_content.voteup_count\n'
        '    search_content.comment_count = detail_content.comment_count\n'
        '    search_content.collected_count = detail_content.collected_count\n'
        '    search_content.share_count = detail_content.share_count\n'
        '    search_content.created_time = detail_content.created_time\n'
        '    search_content.updated_time = detail_content.updated_time\n'
        '    if detail_content.content_visit_refs:\n'
        '        search_content.content_visit_refs = dict(detail_content.content_visit_refs)\n'
        '    search_content.content_detail_status = "detail_observed"',
    ),
)


def parser_and_model_source(text: str) -> str:
    for old, new in PARSER_AND_MODEL_REPLACEMENTS:
        if old not in text:
            continue
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def _detail_request(row) -> bool:
    if row[0] != "request":
        return False
    path = urlsplit(row[2]).path
    return path.startswith(DETAIL_PREFIXES)


def drop_detail_http(trace: list) -> list:
    drop: set[int] = set()
    for index, row in enumerate(trace):
        if not _detail_request(row):
            continue
        drop.add(index)
        cursor = index - 1
        if cursor >= 0 and trace[cursor][0] == "http_client":
            drop.add(cursor)
            cursor -= 1
        if cursor >= 0 and trace[cursor][0] == "sign":
            drop.add(cursor)
            cursor -= 1
        if cursor >= 0 and trace[cursor][0] == "compile":
            drop.add(cursor)
        nxt = index + 1
        if nxt < len(trace) and trace[nxt][0] == "sleep" and trace[nxt][1] in {0.0, 1.0}:
            drop.add(nxt)
    kept = [row for index, row in enumerate(trace) if index not in drop]
    if not any(row[0] == "sign" for row in kept):
        kept = [row for row in kept if row[0] != "compile"]
    return kept


def _project_record(record: dict, *, from_expected: bool) -> dict:
    record = dict(record)
    record.pop("content_visit_refs", None)
    if from_expected and record.get("content_type") in {"answer", "article"} and "content_detail_source" in record:
        record["voteup_count"] = 7
        record["comment_count"] = 1
        record["collected_count"] = 2
        record["share_count"] = 0
        if record.get("content_detail_source") == "search_content":
            record["content_detail_source"] = (
                "answer_detail" if record.get("content_type") == "answer" else "article_detail"
            )
    return record


def _rewrite_jsonl(blob: bytes, *, from_expected: bool) -> bytes:
    lines = []
    for raw in blob.splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        item = _project_record(item, from_expected=from_expected)
        lines.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
    return ("\n".join(lines) + ("\n" if lines else "")).encode()


def _rewrite_files(files: dict, *, from_expected: bool) -> dict:
    rewritten = dict(files)
    for path, blob in files.items():
        if "/jsonl/" not in str(path) or not str(path).endswith(".jsonl"):
            continue
        rewritten[path] = _rewrite_jsonl(blob, from_expected=from_expected)
    return rewritten


def _rewrite_events(events: list, scenario: str) -> list:
    if scenario != "search_detail_429":
        return events
    rewritten = []
    for event in events:
        event = json.loads(json.dumps(event))
        details = event.get("details") or {}
        if details.get("stop_detail") == "rate_limited":
            details["stop_detail"] = "captcha_detected"
            event["details"] = details
        rewritten.append(event)
    return rewritten


def align(actual: dict[str, Any], expected: dict[str, Any], scenario: str) -> tuple[dict, dict]:
    """把固化预期里的浏览器外详情轨迹换成页面取数结果，并抹平访问引用。"""

    expected = {
        **expected,
        "trace": drop_detail_http(list(expected["trace"])),
        "counts": {
            key: value for key, value in expected["counts"].items()
            if not str(key).startswith(DETAIL_PREFIXES)
        },
        "records": [_project_record(record, from_expected=True) for record in expected["records"]],
        "files": _rewrite_files(expected["files"], from_expected=True),
        "events": _rewrite_events(expected["events"], scenario),
    }
    actual = {
        **actual,
        "records": [_project_record(record, from_expected=False) for record in actual["records"]],
        "files": _rewrite_files(actual["files"], from_expected=False),
    }
    return actual, expected
