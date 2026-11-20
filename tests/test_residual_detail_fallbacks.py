"""TripPostCollect tests for residual detail fallbacks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trippostcollect.platforms.weibo.parser import show_matches


def test_weibo_show_identity_is_bound_to_idstr_or_mid() -> None:
    assert show_matches({"idstr": "456", "mid": "1"}, "456")
    assert show_matches({"mid": "456"}, "456")
    assert show_matches({"idstr": "1", "text": "其他微博"}, "456") is False


def test_weibo_repair_hook_recovers_exact_browser_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trippostcollect.application import events
    from trippostcollect.platforms import entry
    from trippostcollect.platforms.weibo.client import WeiboClient
    from trippostcollect.platforms.weibo import models as weibo_models
    from trippostcollect.platforms.weibo.detail_page import WeiboDetailPages

    for name in ("_weibo_post_repair", "_xhs_repair"):
        monkeypatch.setattr(entry, name, getattr(entry, name))
    monkeypatch.setattr(events, "_batch_publisher", events._batch_publisher)
    monkeypatch.setenv("TRIPPOSTCOLLECT_POST_REPAIR", "1")

    entry.install_hooks()
    crawler = entry.load_crawler("wb")()
    crawler.config = SimpleNamespace(**{**crawler.config.__dict__, "CRAWLER_TYPE": "detail"})
    assert crawler.ports.post_repair is True
    assert not hasattr(WeiboClient, "get_note_info_by_id")
    assert not hasattr(weibo_models, "WeiboRepairNotImplemented")
    assert hasattr(WeiboDetailPages, "open_by_navigate")
