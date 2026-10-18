"""互动计数与页面显示的对照取证（#94）：只取证，判定不变，空串仍算缺失。

浮层（repair 为整页）在翻图之后、点作者之前读一次互动栏与评论区，逐篇追加到 `behavior_evidence.engagement.jsonl`；
子摘要的平台记录 `output.engagement_observation` 按计数汇总。假站点的互动栏按操作人 2026-10-18 实地结构建模，
读数用与页面脚本相同的选择器。
"""

from __future__ import annotations

import json

import pytest

from support.xhs_fake_site import detail_card, engagement_view, image_url
from trippostcollect.platforms.xhs import overlay as xhs_overlay
from trippostcollect.xhs.engagement import (
    comment_section,
    display_kind,
    engagement_observation,
    feed_kind,
    summarize_engagement_evidence,
)

from .test_xhs_discovery_memory import events_of, prepare_crawler
from .test_xhs_repair_state import prepare_repair

COUNTS = ("liked_count", "collected_count", "comment_count", "share_count")


def card_with(note_id: str, **interact: str) -> dict:
    card = detail_card(note_id)
    card["interact_info"].update(interact)
    return card


def evidence_lines(tmp_path) -> list[dict]:
    path = tmp_path / "behavior_evidence.engagement.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def spy_validity(crawler) -> dict[str, bool]:
    verdicts: dict[str, bool] = {}
    original = crawler._note_fields_valid

    def spy(note_id, note_detail):
        verdicts[note_id] = original(note_id, note_detail)
        return verdicts[note_id]

    crawler._note_fields_valid = spy
    return verdicts


def test_value_and_display_categories():
    interact = {"liked_count": "1.2万", "collected_count": "", "comment_count": None, "share_count": "很多"}
    assert [feed_kind(interact, key) for key in COUNTS] == [
        ("number", 12000), ("empty_string", None), ("null", None), ("other", None),
    ]
    assert feed_kind({}, "liked_count") == ("missing", None) and feed_kind(None, "liked_count") == ("missing", None)
    assert feed_kind({"liked_count": "0"}, "liked_count") == ("number", 0)
    assert [display_kind(text) for text in ("12", "3.4万", " ", "收藏", "评论", None)] == [
        ("number", 12), ("number", 34000), ("empty", None), ("label", None), ("label", None), ("absent", None),
    ]
    assert comment_section({"comments": {"container": True, "total_text": "共 12 条评论", "empty_state": False}}) == {
        "section": "total", "total": 12, "container": True, "empty_state": False,
    }
    assert comment_section({"comments": {"container": True, "total_text": "共 1.1万 条评论"}})["total"] == 11000
    assert comment_section({"comments": {"total_text": "评论"}})["section"] == "unparsed_total"
    assert comment_section({"comments": {"container": True, "empty_state": True}})["section"] == "empty_state"
    assert comment_section({"comments": {"container": True}})["section"] == "container_only"
    assert comment_section({"comments": {}})["section"] == "not_rendered"


def test_summary_tables_cross_feed_and_page_categories(tmp_path):
    """feed×页面类别、两边都是数字时相等与不等、feed 为空串时页面一侧的分档；同一笔记重做只取最后一行。"""
    path = tmp_path / "behavior_evidence.engagement.jsonl"

    def page(like, collect="收藏", chat="评论", *, total=None, empty=False, share=True):
        return {
            "view_visible": True, "bar": True, "left": True,
            "like": {"text": like, "matches": int(like is not None), "wrappers": int(like is not None)},
            "collect": {"text": collect, "matches": 1, "wrappers": 1},
            "chat": {"text": chat, "matches": 1, "wrappers": 1},
            "share": {"button": share, "text": None, "matches": 0, "wrappers": int(share)},
            "comments": {"container": True, "total_text": total, "empty_state": empty},
        }

    rows = [
        ("a", {"liked_count": "5", "collected_count": "2", "comment_count": "3", "share_count": "1"},
         page("5", "2", "3", total="共 3 条评论")),
        ("b", {"liked_count": "9", "collected_count": "", "comment_count": "", "share_count": ""},
         page("10", empty=True)),
        ("c", {"liked_count": "", "collected_count": "", "comment_count": "", "share_count": ""},
         page("", empty=True, share=False)),
        ("d", {"liked_count": "", "collected_count": "", "comment_count": "", "share_count": ""},
         page("0", "0", "0", total="共 0 条评论")),
        ("e", {"liked_count": "", "collected_count": "", "comment_count": "", "share_count": ""},
         page("12", "3", None)),
        ("f", {"liked_count": "", "collected_count": "", "comment_count": "", "share_count": ""},
         page(None)),
    ]
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(engagement_observation("a", mode="discovery", interact={}, page=None,
                                                       failure="timeout")) + "\n")
        for note_id, interact, raw in rows:
            handle.write(json.dumps(engagement_observation(note_id, mode="discovery", interact=interact, page=raw)) + "\n")
        handle.write(json.dumps(engagement_observation("g", mode="discovery", interact=rows[0][1], page=None,
                                                       failure="script_error")) + "\n")
        handle.write("{broken\n")

    summary = summarize_engagement_evidence(path)

    assert (summary["lines"], summary["malformed_lines"], summary["notes"]) == (8, 1, 7)
    assert summary["reads"] == {"ok": 6, "script_error": 1}
    assert summary["bar_missing"] == 0
    liked = summary["counts"]["liked_count"]
    assert liked["feed_by_page"] == {
        "empty_string": {"absent": 1, "empty": 1, "number": 2},
        "number": {"number": 2, "unread": 1},
    }
    assert liked["both_numbers"] == {"equal": 1, "different": 1}
    assert liked["feed_empty_page"] == {
        "0": 1, "1-9": 0, "10+": 1, "label": 0, "empty": 1, "absent": 1, "unread": 0,
    }
    assert summary["counts"]["collected_count"]["feed_empty_page"] == {
        "0": 1, "1-9": 1, "10+": 0, "label": 3, "empty": 0, "absent": 0, "unread": 0,
    }
    assert summary["counts"]["comment_count"]["feed_empty_page"]["absent"] == 1
    share = summary["counts"]["share_count"]
    assert share["feed_by_page"] == {
        "empty_string": {"absent": 1, "icon_only": 4},
        "number": {"icon_only": 1, "unread": 1},
    }
    assert share["feed_empty_page"]["icon_only"] == 4 and share["feed_empty_page"]["absent"] == 1
    comments = summary["comment_section"]
    assert comments["feed_by_page"] == {
        "empty_string": {"container_only": 2, "empty_state": 2, "total": 1},
        "number": {"total": 1, "unread": 1},
    }
    assert comments["both_numbers"] == {"equal": 1, "different": 0}
    assert comments["feed_empty_page"] == {
        "0": 1, "1-9": 0, "10+": 0, "unparsed_total": 0, "empty_state": 2, "container_only": 2,
        "not_rendered": 0, "unread": 0,
    }
    assert summarize_engagement_evidence(tmp_path / "missing.jsonl")["notes"] == 0


@pytest.mark.asyncio
async def test_overlay_records_engagement_for_every_detail_note_and_validity_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    ids = ["plain", "wan", "zero", "drift", "absent", "late", "imgfail"]
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=ids)
    site.details["wan"] = card_with("wan", liked_count="1.2万")
    site.details["zero"] = card_with("zero", liked_count="", collected_count="", comment_count="", share_count="")
    site.details["drift"] = card_with("drift", liked_count="7")
    # 点赞可另带状态类；页面显示与 feed 不一致；评论与分享元素缺失、评论区不出现；评论区晚两次读数才出现。
    wan = engagement_view(site.details["wan"])
    bar = wan["div.buttons.engage-bar-style"]["div.left"]
    bar["span.like-wrapper.like-active"] = bar.pop("span.like-wrapper")
    site.engagement_views["wan"] = wan
    drift = engagement_view(site.details["drift"])
    drift["div.buttons.engage-bar-style"]["div.left"]["span.like-wrapper"]["span.count"] = "8"
    site.engagement_views["drift"] = drift
    absent = engagement_view(site.detail("absent"))
    del absent["div.buttons.engage-bar-style"]["div.left"]["span.chat-wrapper"]
    del absent["div.buttons.engage-bar-style"]["div.share-wrapper"]
    del absent["div.comments-el"]
    site.engagement_views["absent"] = absent
    site.comments_late_reads["late"] = 2
    site.image_overrides[image_url("imgfail", 1)] = "missing"
    verdicts = spy_validity(crawler)

    await crawler.search()

    # 判定与改动前一致：四个计数都非空才有效，空串仍算缺失。
    expected = {
        note_id: all(site.detail(note_id)["interact_info"][key] not in (None, "") for key in COUNTS) for note_id in ids
    }
    assert verdicts == expected and expected["zero"] is False and sum(expected.values()) == 6
    assert [record["note_id"] for record in crawler.records] == [note_id for note_id in ids if note_id != "imgfail"]
    (skipped,) = events_of(state_path, "candidate_skipped")
    assert (skipped["identity"], skipped["error_code"]) == ("imgfail", "image_response_not_observed")

    lines = {line["note_id"]: line for line in evidence_lines(tmp_path)}
    assert list(lines) == ids
    assert {(line["mode"], line["read"], line["bar"], line["left"]) for line in lines.values()} == {
        ("discovery", "ok", True, True),
    }
    assert lines["plain"]["counts"] == {
        "liked_count": {"feed": "number", "feed_value": 1, "page": "number", "page_value": 1},
        "collected_count": {"feed": "number", "feed_value": 2, "page": "number", "page_value": 2},
        "comment_count": {"feed": "number", "feed_value": 3, "page": "number", "page_value": 3},
        "share_count": {"feed": "number", "feed_value": 4, "page": "icon_only"},
    }
    # 评论区的评论点赞同样是 .like-wrapper span.count，但不在互动栏内，不被读到。
    assert lines["plain"]["comments"] == {"section": "total", "total": 3, "container": True, "empty_state": False}
    assert lines["wan"]["counts"]["liked_count"] == {
        "feed": "number", "feed_value": 12000, "page": "number", "page_value": 12000,
    }
    assert lines["zero"]["counts"] == {
        "liked_count": {"feed": "empty_string", "page": "empty"},
        "collected_count": {"feed": "empty_string", "page": "label"},
        "comment_count": {"feed": "empty_string", "page": "label"},
        "share_count": {"feed": "empty_string", "page": "icon_only"},
    }
    assert lines["zero"]["comments"] == {"section": "empty_state", "container": True, "empty_state": True}
    assert lines["drift"]["counts"]["liked_count"] == {
        "feed": "number", "feed_value": 7, "page": "number", "page_value": 8,
    }
    assert lines["absent"]["counts"]["comment_count"] == {
        "feed": "number", "feed_value": 3, "page": "absent", "matches": 0, "wrappers": 0,
    }
    assert lines["absent"]["counts"]["share_count"] == {
        "feed": "number", "feed_value": 4, "page": "absent", "matches": 0, "wrappers": 0,
    }
    assert lines["absent"]["comments"] == {"section": "not_rendered", "container": False, "empty_state": False}
    assert lines["late"]["comments"]["section"] == "total"
    assert lines["imgfail"]["counts"]["liked_count"]["page"] == "number"
    # 评论区已给出结果即止：缺失的一直轮询到上限（2 秒、每 0.25 秒一次，共 9 次），晚到的第 3 次读到。
    reads = {note_id: site.engagement_reads.count(note_id) for note_id in ids}
    assert reads == {"plain": 1, "wan": 1, "zero": 1, "drift": 1, "absent": 9, "late": 3, "imgfail": 1}

    summary = summarize_engagement_evidence(tmp_path / "behavior_evidence.engagement.jsonl")
    assert summary["notes"] == 7 and summary["reads"] == {"ok": 7}
    liked = summary["counts"]["liked_count"]
    assert liked["feed_by_page"] == {"empty_string": {"empty": 1}, "number": {"number": 6}}
    assert liked["both_numbers"] == {"equal": 5, "different": 1}
    assert summary["counts"]["comment_count"]["feed_by_page"] == {
        "empty_string": {"label": 1}, "number": {"absent": 1, "number": 5},
    }
    assert summary["comment_section"]["feed_by_page"] == {
        "empty_string": {"empty_state": 1}, "number": {"not_rendered": 1, "total": 5},
    }
    # 证据只有 ID、类别与数字：不含正文、昵称、地址或页面原文（“收藏”“评论”只记类别）。
    text = (tmp_path / "behavior_evidence.engagement.jsonl").read_text(encoding="utf-8")
    assert "://" not in text and "青岛" not in text and "作者" not in text
    assert "收藏" not in text and "评论" not in text and "万" not in text


@pytest.mark.asyncio
async def test_failed_engagement_read_records_a_category_and_leaves_the_note_unchanged(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, _, site = prepare_crawler(monkeypatch, tmp_path, items=["slow", "broken", "fine"])
    real = xhs_overlay.dom_note_engagement

    async def flaky(page, roots):
        if site.overlay_note == "slow":
            raise TimeoutError
        if site.overlay_note == "broken":
            raise RuntimeError("script failed")
        return await real(page, roots)

    monkeypatch.setattr(xhs_overlay, "dom_note_engagement", flaky)
    verdicts = spy_validity(crawler)

    await crawler.search()

    assert verdicts == {"slow": True, "broken": True, "fine": True}
    assert [record["note_id"] for record in crawler.records] == ["slow", "broken", "fine"]
    lines = {line["note_id"]: line for line in evidence_lines(tmp_path)}
    assert [lines[note_id]["read"] for note_id in ("slow", "broken", "fine")] == ["timeout", "script_error", "ok"]
    assert lines["slow"]["counts"]["liked_count"] == {"feed": "number", "feed_value": 1, "page": "unread"}
    assert lines["slow"]["comments"] == {"section": "unread"} and "bar" not in lines["slow"]


@pytest.mark.asyncio
async def test_repair_page_view_records_engagement_too(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior_evidence.json"))
    crawler, site, reports = prepare_repair(monkeypatch, tmp_path, ["repaired"])
    site.details["repaired"] = card_with("repaired", comment_count="")

    await crawler.get_specified_notes()

    assert reports[-1]["candidate_failures"] == []
    (line,) = evidence_lines(tmp_path)
    assert (line["mode"], line["note_id"], line["read"]) == ("repair", "repaired", "ok")
    assert line["counts"]["comment_count"] == {"feed": "empty_string", "page": "label"}
    assert line["comments"]["section"] == "empty_state"
    assert crawler.records[0]["note_id"] == "repaired"
