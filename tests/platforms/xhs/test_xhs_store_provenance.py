from __future__ import annotations

import pytest

from .support import XiaoHongShuCrawler, config


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
