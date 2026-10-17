from __future__ import annotations

import pytest

from .support import XiaoHongShuCrawler, assert_no_avatar_log_lines, config, crawler_log_lines


@pytest.mark.asyncio
async def test_jsonl_store_preserves_note_detail_provenance(monkeypatch) -> None:
    captured: dict = {}

    class Store:
        async def store_content(self, content_item):
            captured.update(content_item)

    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "jsonl")
    crawler = XiaoHongShuCrawler()
    monkeypatch.setattr(
        crawler.ports,
        "content_sink_factory",
        lambda _crawler_type: Store(),
    )
    await crawler.update_xhs_note(
        {
            "note_id": "note-1",
            "type": "normal",
            "title": "title",
            "desc": "complete body",
            "content_detail_status": "detail_observed",
            "content_detail_source": "note_detail",
            "user": {"user_id": "author-1", "nickname": "author"},
            "creator_profile": {"fans_count": 10},
            "interact_info": {},
            "image_list": [{"url_default": "https://sns-img.test/body.jpg"}],
        }
    )

    assert captured["content_detail_status"] == "detail_observed"
    assert captured["content_detail_source"] == "note_detail"
    assert captured["desc"] == "complete body"


@pytest.mark.asyncio
async def test_store_log_names_the_note_without_avatar_key_token_or_body(monkeypatch) -> None:
    """写出记录的日志只含笔记 ID 与图片数：整条记录带头像键与值，曾让子进程日志整段替换为审计标记（#80）。"""
    stored: list[dict] = []

    class Store:
        async def store_content(self, content_item):
            stored.append(content_item)

    crawler = XiaoHongShuCrawler()
    monkeypatch.setattr(crawler.ports, "content_sink_factory", lambda _crawler_type: Store())
    note = {
        "note_id": "note-1", "type": "normal", "title": "崂山", "desc": "complete body", "xsec_token": "token-1",
        "user": {"user_id": "author-1", "nickname": "author", "avatar": "https://sns-avatar-qc.xhscdn.com/a.jpg"},
        "creator_profile": {"fans_count": 10}, "interact_info": {},
        "image_list": [{"url_default": "https://sns-img.test/body.jpg"}],
    }

    with crawler_log_lines() as lines:
        await crawler.update_xhs_note(note)

    assert stored[0]["avatar_url"] == "https://sns-avatar-qc.xhscdn.com/a.jpg"  # 记录本身由导出清除器处理
    assert lines == ["[store.xhs.update_xhs_note] xhs note: note_id=note-1, images=1"]
    assert_no_avatar_log_lines(lines)
