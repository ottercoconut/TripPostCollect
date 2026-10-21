"""小红书互动计数与页面显示、搜索响应的对照取证（#94）：只取证，不改有效性判定。

feed `note_card.interact_info` 的四个计数约四分之一的笔记有空串；空串仍算缺失（用户 2026-10-18 决定：不能直接
按 0 处理，须有明确依据）。全量扫描的浮层与 repair 的整页在翻图之后、点作者之前读一次互动栏与评论区，逐篇追加
到行为证据同目录的 `behavior_evidence.engagement.jsonl`（不截断；只有笔记 ID、类别与数字，不写正文、评论、
作者或地址），子摘要的平台记录 `output.engagement_observation` 按计数汇总成对照表。

全量扫描另记登记本篇卡片所用的目标会话搜索条目里的四项计数（操作人 2026-10-21 DevTools 核对：feed 为空串的地方
搜索响应写 "0"，但搜索的数字可能比 feed 旧），与 feed 原值用同一套类别；repair 整页没有搜索来源。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping

from trippostcollect.records.formal import parse_int


ENGAGEMENT_SCHEMA_VERSION = 2
ENGAGEMENT_COUNTS = ("liked_count", "collected_count", "comment_count", "share_count")
# 计数键 → 搜索响应 `note_card.interact_info` 里的键：分享为 shared_count（feed 为 share_count）。
SEARCH_COUNT_KEYS = {
    "liked_count": "liked_count", "collected_count": "collected_count", "comment_count": "comment_count",
    "share_count": "shared_count",
}
# 计数键 → 页面脚本 note_engagement 返回的位置：点赞、收藏、评论在互动栏 .left 内，分享只有按钮。
PAGE_SLOTS = {"liked_count": "like", "collected_count": "collect", "comment_count": "chat", "share_count": "share"}
# 读到总数行（含解析不了的）或空状态即视为评论区已给出结果，不再等待。
COMMENT_SECTION_SETTLED = frozenset({"total", "unparsed_total", "empty_state"})
_COMMENT_TOTAL = re.compile(r"共\s*(\S+?)\s*条评论")
_VALUE_BUCKETS = ("0", "1-9", "10+")
_SEARCH_KINDS = ("empty_string", "missing", "null", "other", "no_source")


def engagement_evidence_path(behavior_evidence_path: str | Path) -> Path:
    path = Path(behavior_evidence_path).expanduser()
    return path.with_name(f"{path.stem}.engagement.jsonl")


def raw_kind(interact: Any, key: str) -> tuple[str, int | None]:
    """feed 与搜索原值的类别：number（`parse_int` 可解析且不为 0，含“万”）、zero（解析为 0，实地为 "0"）、
    empty_string、missing（键不存在）、null；其余为 other。zero 单列，以区分“写了 0”与“写了空串”。"""
    if not isinstance(interact, Mapping) or key not in interact:
        return "missing", None
    value = interact[key]
    if value is None:
        return "null", None
    if value == "":
        return "empty_string", None
    parsed = parse_int(value)
    if parsed is None:
        return "other", None
    return ("zero" if parsed == 0 else "number"), parsed


def search_counts(interact: Any) -> dict[str, Any]:
    """搜索条目 `note_card.interact_info` 的四项计数原值（搜索键 → 原值，只含出现的键，不留 liked 等其他字段）。"""
    if not isinstance(interact, Mapping):
        return {}
    return {key: interact[key] for key in SEARCH_COUNT_KEYS.values() if key in interact}


def _lag_seconds(search_at: str, feed_at: str) -> int | None:
    try:
        return int((datetime.fromisoformat(feed_at) - datetime.fromisoformat(search_at)).total_seconds())
    except (TypeError, ValueError):
        return None


def search_source(
    counts: Mapping[str, Any], *, page: int, search_seq: int, search_at: str, feed_seq: int, feed_at: str,
) -> dict[str, Any]:
    """登记本篇卡片所用的搜索条目：四项原值 `counts`（只在内存中，写证据时只留类别与数字）与出处 `origin`——
    页码、搜索与 feed 的截获序号、feed 晚于搜索的秒数（两次截获时刻相减，精确到秒）。"""
    return {
        "counts": dict(counts),
        "origin": {
            "source": "search_results",
            "page": page,
            "search_seq": search_seq,
            "feed_seq": feed_seq,
            "lag_seconds": _lag_seconds(search_at, feed_at),
        },
    }


def display_kind(text: Any) -> tuple[str, int | None]:
    """页面显示类别：absent（找不到元素）、empty（文字为空）、number（可解析，含“万”）、label（其余文字）。"""
    if not isinstance(text, str):
        return "absent", None
    if not text.strip():
        return "empty", None
    parsed = parse_int(text)
    return ("number", parsed) if parsed is not None else ("label", None)


def comment_section(raw: Any) -> dict[str, Any]:
    """评论区：total（总数行解析出 N）、unparsed_total、empty_state（空状态）、container_only（只有容器）、
    not_rendered；`empty_state` 另记空状态元素是否出现（总数行与空状态同时出现时也能看到）。"""
    comments = raw.get("comments") if isinstance(raw, Mapping) else None
    comments = comments if isinstance(comments, Mapping) else {}
    empty_state = bool(comments.get("empty_state"))
    section: dict[str, Any] = {"container": bool(comments.get("container")), "empty_state": empty_state}
    total_text = comments.get("total_text")
    if isinstance(total_text, str):
        match = _COMMENT_TOTAL.search(total_text)
        total = parse_int(match.group(1)) if match else None
        if total is None:
            section["section"] = "unparsed_total"
        else:
            section.update(section="total", total=total)
    elif empty_state:
        section["section"] = "empty_state"
    else:
        section["section"] = "container_only" if section["container"] else "not_rendered"
    return section


def comments_pending(raw: Any) -> bool:
    """详情视图在、评论区还没给出总数行或空状态时为真（读数方据此短暂轮询）。"""
    if not isinstance(raw, Mapping) or not raw.get("view_visible"):
        return False
    return comment_section(raw)["section"] not in COMMENT_SECTION_SETTLED


def _page_entry(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    slot = raw.get(PAGE_SLOTS[key])
    slot = slot if isinstance(slot, Mapping) else {}
    text = slot.get("text")
    if key == "share_count" and text is None and slot.get("button"):
        return {"page": "icon_only"}
    kind, value = display_kind(text)
    entry: dict[str, Any] = {"page": kind}
    if value is not None:
        entry["page_value"] = value
    if kind == "absent":
        # 便于下一轮修选择器：互动栏内匹配完整选择器的个数、整个详情视图内同类包装元素的个数。
        entry.update(matches=int(slot.get("matches") or 0), wrappers=int(slot.get("wrappers") or 0))
    return entry


def engagement_observation(
    note_id: str,
    *,
    mode: str,
    interact: Any,
    page: Any,
    search: Mapping[str, Any] | None = None,
    failure: str = "",
    at: str | None = None,
) -> dict[str, Any]:
    """一篇的逐篇证据。`failure` 为读数异常类别（timeout、script_error）；读数失败时页面一侧记 unread。
    `search` 为 `search_source` 的结果；没有搜索来源（repair 整页）时为 None，各计数的搜索一侧记 no_source。"""
    if failure:
        read = failure
    elif not isinstance(page, Mapping):
        read = "invalid_result"
    elif not page.get("view_visible"):
        read = "view_missing"
    else:
        read = "ok"
    counts: dict[str, Any] = {}
    for key in ENGAGEMENT_COUNTS:
        kind, value = raw_kind(interact, key)
        entry: dict[str, Any] = {"feed": kind}
        if value is not None:
            entry["feed_value"] = value
        kind, value = raw_kind(search["counts"], SEARCH_COUNT_KEYS[key]) if search else ("no_source", None)
        entry["search"] = kind
        if value is not None:
            entry["search_value"] = value
        entry.update(_page_entry(page, key) if read == "ok" else {"page": "unread"})
        counts[key] = entry
    line: dict[str, Any] = {
        "schema_version": ENGAGEMENT_SCHEMA_VERSION,
        "at": at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "note_id": str(note_id),
        "read": read,
    }
    if read == "ok":
        line.update(bar=bool(page.get("bar")), left=bool(page.get("left")))
    line["search"] = dict(search["origin"]) if search else {"source": "none"}
    line["counts"] = counts
    line["comments"] = comment_section(page) if read == "ok" else {"section": "unread"}
    return line


def append_engagement_observation(path: str | Path, line: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")


def _bucket(value: int) -> str:
    return "0" if value == 0 else "1-9" if value < 10 else "10+"


def _cross_table(pairs: list[tuple[str, int | None, str, int | None]], other_kinds: tuple[str, ...]) -> dict[str, Any]:
    """(feed 类别, feed 数, 页面类别, 页面数) → feed×页面类别表、两边都是数字时相等与不等的篇数、
    feed 为空串时页面一侧的分布（数字按 0、1–9、10 以上分档，其余按类别）。"""
    by_page: dict[str, Counter[str]] = {}
    both = {"equal": 0, "different": 0}
    empty_feed = dict.fromkeys((*_VALUE_BUCKETS, *other_kinds), 0)
    for feed, feed_value, page, page_value in pairs:
        by_page.setdefault(feed, Counter())[page] += 1
        if feed_value is not None and page_value is not None:
            both["equal" if feed_value == page_value else "different"] += 1
        if feed == "empty_string":
            slot = _bucket(page_value) if page_value is not None else page
            empty_feed[slot] = empty_feed.get(slot, 0) + 1
    return {
        "feed_by_page": {feed: dict(sorted(pages.items())) for feed, pages in sorted(by_page.items())},
        "both_numbers": both,
        "feed_empty_page": empty_feed,
    }


def _search_table(pairs: list[tuple[str, int | None, str, int | None]]) -> dict[str, Any]:
    """(feed 类别, feed 数, 搜索类别, 搜索数) → feed×搜索类别表；feed 为空串时搜索一侧的分布（写成 0 记 `0`，其余数字
    按 1–9、10 以上分档，非数字按类别）；两边都有数时搜索等于、小于、大于 feed 的篇数（搜索可能比 feed 旧）。"""
    by_search: dict[str, Counter[str]] = {}
    both = {"equal": 0, "search_lower": 0, "search_higher": 0}
    empty_feed = dict.fromkeys((*_VALUE_BUCKETS, *_SEARCH_KINDS), 0)
    for feed, feed_value, search, search_value in pairs:
        by_search.setdefault(feed, Counter())[search] += 1
        if feed_value is not None and search_value is not None:
            both[
                "equal" if search_value == feed_value else "search_lower" if search_value < feed_value
                else "search_higher"
            ] += 1
        if feed == "empty_string":
            slot = _bucket(search_value) if search_value is not None else search
            empty_feed[slot] = empty_feed.get(slot, 0) + 1
    return {
        "feed_by_search": {feed: dict(sorted(kinds.items())) for feed, kinds in sorted(by_search.items())},
        "both_numbers": both,
        "feed_empty_search": empty_feed,
    }


def _read_lines(path: Path) -> tuple[list[dict[str, Any]], int]:
    lines: list[dict[str, Any]] = []
    malformed = 0
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], 0
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(line, dict) and isinstance(line.get("counts"), dict):
            lines.append(line)
        else:
            malformed += 1
    return lines, malformed


def summarize_engagement_evidence(path: str | Path) -> dict[str, Any]:
    """子摘要用的汇总；同一笔记（整篇重做）只取最后一行。文件不存在时各项为 0。"""
    path = Path(path)
    lines, malformed = _read_lines(path)
    latest = {str(line.get("note_id") or ""): line for line in lines}
    notes = list(latest.values())
    summary: dict[str, Any] = {
        "schema_version": ENGAGEMENT_SCHEMA_VERSION,
        "evidence_path": str(path),
        "lines": len(lines),
        "malformed_lines": malformed,
        "notes": len(notes),
        "reads": dict(sorted(Counter(str(line.get("read")) for line in notes).items())),
        "bar_missing": sum(1 for line in notes if line.get("read") == "ok" and not line.get("bar")),
        "counts": {},
        "search": {
            "sources": dict(sorted(Counter(str((line.get("search") or {}).get("source")) for line in notes).items())),
            "counts": {},
        },
    }
    for key in ENGAGEMENT_COUNTS:
        entries = [line["counts"].get(key) or {} for line in notes]
        pairs = [
            (str(entry.get("feed")), entry.get("feed_value"), str(entry.get("page")), entry.get("page_value"))
            for entry in entries
        ]
        others = ("label", "empty", "absent", "unread") + (("icon_only",) if key == "share_count" else ())
        summary["counts"][key] = _cross_table(pairs, others)
        search_pairs = [
            (str(entry.get("feed")), entry.get("feed_value"), str(entry.get("search")), entry.get("search_value"))
            for entry in entries
        ]
        summary["search"]["counts"][key] = {"search_key": SEARCH_COUNT_KEYS[key], **_search_table(search_pairs)}
    section_pairs = []
    for line in notes:
        entry = line["counts"].get("comment_count") or {}
        comments = line.get("comments") or {}
        section_pairs.append(
            (str(entry.get("feed")), entry.get("feed_value"), str(comments.get("section")), comments.get("total"))
        )
    summary["comment_section"] = _cross_table(
        section_pairs, ("unparsed_total", "empty_state", "container_only", "not_rendered", "unread"),
    )
    return summary
