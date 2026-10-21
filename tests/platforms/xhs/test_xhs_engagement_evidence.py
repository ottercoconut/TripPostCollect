"""互动计数与页面显示、搜索响应的对照取证（#94）：只取证，判定不变，空串仍算缺失。

浮层（repair 为整页）在翻图之后、点作者之前读一次互动栏与评论区，逐篇追加到 `behavior_evidence.engagement.jsonl`；
子摘要的平台记录 `output.engagement_observation` 按计数汇总。假站点的互动栏按操作人 2026-10-18 实地结构建模，
读数用与页面脚本相同的选择器。全量扫描另记登记本篇卡片所用的搜索条目里的四项计数（搜索键 shared_count 对 feed
share_count，操作人 2026-10-21 DevTools 核对）；repair 整页没有搜索来源。
"""

from __future__ import annotations

import json

import pytest

from datetime import datetime, timedelta, timezone

from support.xhs_fake_site import detail_card, engagement_view, image_url, note
from trippostcollect.platforms.xhs import overlay as xhs_overlay
from trippostcollect.xhs.engagement import (
    comment_section,
    display_kind,
    engagement_observation,
    raw_kind,
    search_counts,
    search_source,
    summarize_engagement_evidence,
)

from .test_xhs_discovery_memory import events_of, ledger_lines, prepare_crawler
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
    assert [raw_kind(interact, key) for key in COUNTS] == [
        ("number", 12000), ("empty_string", None), ("null", None), ("other", None),
    ]
    assert raw_kind({}, "liked_count") == ("missing", None) and raw_kind(None, "liked_count") == ("missing", None)
    # “写了 0”单列，与“写了空串”区分（feed 与搜索同一套类别）。
    assert raw_kind({"liked_count": "0"}, "liked_count") == ("zero", 0)
    assert raw_kind({"liked_count": 0}, "liked_count") == ("zero", 0)
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
    # 没有搜索来源的行：各计数的搜索一侧记 no_source。
    assert summary["search"]["sources"] == {"none": 7}
    assert summary["search"]["counts"]["liked_count"]["feed_by_search"] == {
        "empty_string": {"no_source": 4}, "number": {"no_source": 3},
    }
    assert summary["search"]["counts"]["liked_count"]["feed_empty_search"]["no_source"] == 4
    missing = summarize_engagement_evidence(tmp_path / "missing.jsonl")
    assert missing["notes"] == 0 and missing["search"]["sources"] == {}


def test_search_counts_keep_only_the_four_raw_values_and_record_their_origin():
    interact = {"liked": True, "liked_count": "1", "collected": False, "collected_count": "0", "comment_count": "",
                "nice_count": "5"}
    # 只留四项计数原值（分享键为 shared_count，这里缺键），不留 liked、collected 等其他字段。
    assert search_counts(interact) == {"liked_count": "1", "collected_count": "0", "comment_count": ""}
    assert search_counts(None) == {} and search_counts("x") == {}
    source = search_source(
        search_counts(interact), page=2, search_seq=14, search_at="2026-10-21T05:00:00+00:00",
        feed_seq=40, feed_at="2026-10-21T05:00:37+00:00",
    )
    feed = {"liked_count": "2", "collected_count": "", "comment_count": "3", "share_count": "4"}
    line = engagement_observation("n", mode="discovery", interact=feed, page=None, search=source, failure="timeout")
    assert line["search"] == {
        "source": "search_results", "page": 2, "search_seq": 14, "feed_seq": 40, "lag_seconds": 37,
    }
    assert line["counts"] == {
        "liked_count": {"feed": "number", "feed_value": 2, "search": "number", "search_value": 1, "page": "unread"},
        "collected_count": {"feed": "empty_string", "search": "zero", "search_value": 0, "page": "unread"},
        "comment_count": {"feed": "number", "feed_value": 3, "search": "empty_string", "page": "unread"},
        "share_count": {"feed": "number", "feed_value": 4, "search": "missing", "page": "unread"},
    }
    unparsed = search_source({}, page=1, search_seq=1, search_at="", feed_seq=2, feed_at="2026-10-21T05:00:00+00:00")
    assert unparsed["origin"]["lag_seconds"] is None
    repair = engagement_observation("r", mode="repair", interact=feed, page=None, failure="timeout")
    assert repair["search"] == {"source": "none"}
    assert {entry["search"] for entry in repair["counts"].values()} == {"no_source"}
    assert all("search_value" not in entry for entry in repair["counts"].values())


def test_search_tables_cross_feed_and_search_categories(tmp_path):
    """feed×搜索类别；feed 为空串时搜索一侧的分布；两边都有数时搜索等于、小于、大于 feed；repair 记无搜索来源。"""
    path = tmp_path / "behavior_evidence.engagement.jsonl"

    def feed(liked, collected, comment, share):
        return {"liked_count": liked, "collected_count": collected, "comment_count": comment, "share_count": share}

    def search(**counts):
        return search_source(
            counts, page=1, search_seq=3, search_at="2026-10-21T05:00:00+00:00",
            feed_seq=9, feed_at="2026-10-21T05:00:20+00:00",
        )

    rows = [
        # 操作人核对的 A、B、C 三篇：A 全空串对全 "0"；B 的搜索比 feed 小、收藏空串对 "0"；C 两边相同。
        ("a", feed("", "", "", ""), search(liked_count="0", collected_count="0", comment_count="0", shared_count="0")),
        ("b", feed("2", "", "3", "4"), search(liked_count="1", collected_count="0", comment_count="1", shared_count="3")),
        ("c", feed("1918", "110", "441", "97"),
         search(liked_count="1918", collected_count="110", comment_count="441", shared_count="97")),
        # feed 空串时搜索为 1–9、10 以上、空串、缺键；搜索比 feed 大；null；feed 写 0。
        ("d", feed("", "", "", ""), search(liked_count="3", collected_count="1.2万", comment_count="")),
        ("e", feed("5", "5", "", "5"), search(liked_count="7", collected_count="5", comment_count=None,
                                              shared_count="2")),
        ("f", feed("0", "0", "0", "0"), search(liked_count="0", collected_count="1", comment_count="0",
                                               shared_count="0")),
    ]
    with path.open("w", encoding="utf-8") as handle:
        for note_id, interact, source in rows:
            line = engagement_observation(note_id, mode="discovery", interact=interact, page=None, search=source,
                                          failure="timeout")
            handle.write(json.dumps(line) + "\n")
        handle.write(json.dumps(engagement_observation("r", mode="repair", interact=feed("", "1", "", ""), page=None,
                                                       failure="timeout")) + "\n")

    summary = summarize_engagement_evidence(path)

    assert summary["search"]["sources"] == {"none": 1, "search_results": 6}
    tables = summary["search"]["counts"]
    assert [tables[key]["search_key"] for key in COUNTS] == [
        "liked_count", "collected_count", "comment_count", "shared_count",
    ]
    liked = tables["liked_count"]
    assert liked["feed_by_search"] == {
        "empty_string": {"no_source": 1, "number": 1, "zero": 1},
        "number": {"number": 3},
        "zero": {"zero": 1},
    }
    assert liked["both_numbers"] == {"equal": 2, "search_lower": 1, "search_higher": 1}
    assert liked["feed_empty_search"] == {
        "0": 1, "1-9": 1, "10+": 0, "empty_string": 0, "missing": 0, "null": 0, "other": 0, "no_source": 1,
    }
    collected = tables["collected_count"]
    assert collected["feed_empty_search"] == {
        "0": 2, "1-9": 0, "10+": 1, "empty_string": 0, "missing": 0, "null": 0, "other": 0, "no_source": 0,
    }
    assert collected["both_numbers"] == {"equal": 2, "search_lower": 0, "search_higher": 1}
    comment = tables["comment_count"]
    assert comment["feed_by_search"] == {
        "empty_string": {"empty_string": 1, "no_source": 1, "null": 1, "zero": 1},
        "number": {"number": 2},
        "zero": {"zero": 1},
    }
    assert comment["both_numbers"] == {"equal": 2, "search_lower": 1, "search_higher": 0}
    share = tables["share_count"]
    assert share["feed_empty_search"] == {
        "0": 1, "1-9": 0, "10+": 0, "empty_string": 0, "missing": 1, "null": 0, "other": 0, "no_source": 1,
    }
    assert share["both_numbers"] == {"equal": 2, "search_lower": 2, "search_higher": 0}
    # 页面一侧的表照常：feed 写 0 单列为 zero。
    assert summary["counts"]["liked_count"]["feed_by_page"] == {
        "empty_string": {"unread": 3}, "number": {"unread": 3}, "zero": {"unread": 1},
    }


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
    # 搜索条目为假站点默认值（与 feed 默认计数相同）。
    assert lines["plain"]["counts"] == {
        "liked_count": {
            "feed": "number", "feed_value": 1, "search": "number", "search_value": 1, "page": "number", "page_value": 1,
        },
        "collected_count": {
            "feed": "number", "feed_value": 2, "search": "number", "search_value": 2, "page": "number", "page_value": 2,
        },
        "comment_count": {
            "feed": "number", "feed_value": 3, "search": "number", "search_value": 3, "page": "number", "page_value": 3,
        },
        "share_count": {"feed": "number", "feed_value": 4, "search": "number", "search_value": 4, "page": "icon_only"},
    }
    assert {line["search"]["source"] for line in lines.values()} == {"search_results"}
    # 评论区的评论点赞同样是 .like-wrapper span.count，但不在互动栏内，不被读到。
    assert lines["plain"]["comments"] == {"section": "total", "total": 3, "container": True, "empty_state": False}
    assert lines["wan"]["counts"]["liked_count"] == {
        "feed": "number", "feed_value": 12000, "search": "number", "search_value": 1, "page": "number",
        "page_value": 12000,
    }
    assert {key: entry["page"] for key, entry in lines["zero"]["counts"].items()} == {
        "liked_count": "empty", "collected_count": "label", "comment_count": "label", "share_count": "icon_only",
    }
    assert {entry["feed"] for entry in lines["zero"]["counts"].values()} == {"empty_string"}
    assert lines["zero"]["comments"] == {"section": "empty_state", "container": True, "empty_state": True}
    assert lines["drift"]["counts"]["liked_count"] == {
        "feed": "number", "feed_value": 7, "search": "number", "search_value": 1, "page": "number", "page_value": 8,
    }
    assert lines["absent"]["counts"]["comment_count"] == {
        "feed": "number", "feed_value": 3, "search": "number", "search_value": 3, "page": "absent", "matches": 0,
        "wrappers": 0,
    }
    assert lines["absent"]["counts"]["share_count"] == {
        "feed": "number", "feed_value": 4, "search": "number", "search_value": 4, "page": "absent", "matches": 0,
        "wrappers": 0,
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


def ticking_clock(start: str = "2026-10-21T05:00:00+00:00"):
    """页面取数通道的时钟：每次读数前进 1 秒，使搜索与 feed 的截获时刻可区分。"""
    moment = [datetime.fromisoformat(start)]

    def clock() -> str:
        moment[0] += timedelta(seconds=1)
        return moment[0].astimezone(timezone.utc).isoformat(timespec="seconds")

    return clock


@pytest.mark.asyncio
async def test_overlay_records_the_registered_search_counts_and_validity_is_unchanged(monkeypatch, tmp_path):
    """逐篇证据带登记本篇卡片所用的搜索条目计数：feed 空串对搜索 "0"、搜索比 feed 小、搜索空串与缺键；同一篇出现在两页
    时用首次出现的页，同一页重复送达时用第一次送达；判定仍只看 feed，空串照常判无效。"""
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)

    def interact(liked, collected, comment, shared=None):
        values = {"liked": False, "liked_count": liked, "collected": False, "collected_count": collected,
                  "comment_count": comment}
        return values if shared is None else {**values, "shared_count": shared}

    page_one = [
        note("zeros", interact=interact("0", "0", "0", "0")),
        note("older", interact=interact("1", "0", "1", "3")),
        note("blank", interact=interact("", "2", "3")),
        note("dup", interact=interact("9", "2", "3", "4")),
    ]
    page_two = [note("later"), note("dup", interact=interact("99", "2", "3", "4"))]
    crawler, _, _, site = prepare_crawler(monkeypatch, tmp_path, items=[], pages=[page_one, page_two])
    crawler.capture._clock = ticking_clock()
    site.details["zeros"] = card_with("zeros", liked_count="", collected_count="", comment_count="", share_count="")
    site.details["older"] = card_with("older", liked_count="2", collected_count="", comment_count="3")
    # 第 2 页第一次送达的“later”点赞为 8；前端随后重发第 2 页（默认值 1），登记仍是第一次送达的那一条。
    first_delivery = {
        "code": 0, "success": True,
        "data": {"has_more": True, "items": [note("later", interact=interact("8", "2", "3", "4")), page_two[1]]},
    }
    site.search_overrides[2] = [first_delivery]
    verdicts = spy_validity(crawler)

    await crawler.search()

    ids = ["zeros", "older", "blank", "dup", "later"]
    assert verdicts == {"zeros": False, "older": False, "blank": True, "dup": True, "later": True}
    lines = {line["note_id"]: line for line in evidence_lines(tmp_path)}
    assert sorted(lines) == sorted(ids)

    def side(note_id: str) -> dict:
        return {
            key: {field: value for field, value in entry.items() if field.startswith(("feed", "search"))}
            for key, entry in lines[note_id]["counts"].items()
        }

    # A 型：feed 全空串、搜索全 "0"。
    assert side("zeros") == {
        "liked_count": {"feed": "empty_string", "search": "zero", "search_value": 0},
        "collected_count": {"feed": "empty_string", "search": "zero", "search_value": 0},
        "comment_count": {"feed": "empty_string", "search": "zero", "search_value": 0},
        "share_count": {"feed": "empty_string", "search": "zero", "search_value": 0},
    }
    # B 型：搜索比 feed 小，收藏 feed 空串、搜索 "0"。
    assert side("older") == {
        "liked_count": {"feed": "number", "feed_value": 2, "search": "number", "search_value": 1},
        "collected_count": {"feed": "empty_string", "search": "zero", "search_value": 0},
        "comment_count": {"feed": "number", "feed_value": 3, "search": "number", "search_value": 1},
        "share_count": {"feed": "number", "feed_value": 4, "search": "number", "search_value": 3},
    }
    assert side("blank")["liked_count"] == {"feed": "number", "feed_value": 1, "search": "empty_string"}
    assert side("blank")["share_count"] == {"feed": "number", "feed_value": 4, "search": "missing"}
    assert side("dup")["liked_count"]["search_value"] == 9
    assert side("later")["liked_count"]["search_value"] == 8

    # 出处：页码、搜索与 feed 的截获序号与账本访问一致，时间差为两次截获时刻之差。
    visits = [line for line in ledger_lines(crawler) if line["kind"] == "visit"]
    search_visits = [line for line in visits if line["stage"] == "search_results" and line.get("is_target") is True]
    assert [line["search_page"] for line in search_visits] == [1, 2, 2, 3]
    first = {}
    for line in search_visits:
        first.setdefault(line["search_page"], line)
    feeds = {line["subject_id"]: line for line in visits if line["stage"] == "note_detail"}
    for note_id in ids:
        origin = lines[note_id]["search"]
        page = 2 if note_id == "later" else 1
        search_visit, feed_visit = first[page], feeds[note_id]
        lag = datetime.fromisoformat(feed_visit["at"]) - datetime.fromisoformat(search_visit["at"])
        assert origin == {
            "source": "search_results", "page": page, "search_seq": search_visit["seq"],
            "feed_seq": feed_visit["seq"], "lag_seconds": int(lag.total_seconds()),
        }
        assert origin["lag_seconds"] > 0

    summary = summarize_engagement_evidence(tmp_path / "behavior_evidence.engagement.jsonl")
    assert summary["search"]["sources"] == {"search_results": 5}
    liked = summary["search"]["counts"]["liked_count"]
    assert liked["feed_by_search"] == {"empty_string": {"zero": 1}, "number": {"empty_string": 1, "number": 3}}
    assert liked["both_numbers"] == {"equal": 0, "search_lower": 1, "search_higher": 2}
    collected = summary["search"]["counts"]["collected_count"]
    assert collected["feed_empty_search"] == {
        "0": 2, "1-9": 0, "10+": 0, "empty_string": 0, "missing": 0, "null": 0, "other": 0, "no_source": 0,
    }
    assert summary["search"]["counts"]["share_count"]["feed_by_search"] == {
        "empty_string": {"zero": 1}, "number": {"missing": 1, "number": 3},
    }
    # 证据只有类别与数字：搜索条目的 liked、collected 等其他字段不写出。
    text = (tmp_path / "behavior_evidence.engagement.jsonl").read_text(encoding="utf-8")
    assert '"liked"' not in text and '"collected"' not in text and "shared_count" not in text


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
    # 读数失败不影响搜索一侧。
    assert lines["slow"]["counts"]["liked_count"] == {
        "feed": "number", "feed_value": 1, "search": "number", "search_value": 1, "page": "unread",
    }
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
    # 整页直开没有搜索响应：记无搜索来源。
    assert line["search"] == {"source": "none"}
    assert line["counts"]["comment_count"] == {"feed": "empty_string", "search": "no_source", "page": "label"}
    assert {entry["search"] for entry in line["counts"].values()} == {"no_source"}
    assert line["comments"]["section"] == "empty_state"
    assert crawler.records[0]["note_id"] == "repaired"
    summary = summarize_engagement_evidence(tmp_path / "behavior_evidence.engagement.jsonl")
    assert summary["search"]["sources"] == {"none": 1}
    assert summary["search"]["counts"]["comment_count"]["feed_empty_search"]["no_source"] == 1
