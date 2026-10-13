"""全量滚动扫描的抓取记忆与停止口径：已知候选滑过、候选失败、运行级失败集中写停止事件（full_scan，无恢复位置）。

搜索流程经离线站点驱动；单篇处理暂走过渡的旧 API 详情路径（提交 5 改为浮层），本文件只验证其候选与运行级口径。
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import httpx
import pytest

from playwright.async_api import Error as PlaywrightError
from tenacity import Future, RetryError
from support.xhs_fake_site import XhsFakeSite, note
from trippostcollect.application import events as app_events
from trippostcollect.platforms.xhs import core as xhs_core
from trippostcollect.platforms.xhs import scan as xhs_scan
from trippostcollect.platforms.xhs.errors import (
    DataFetchError,
    IPBlockError,
    PlatformRuntimeError,
    XHSCreatorProfileUnavailable,
    XHSImageDownloadError,
    XHSMainPageClosedUnexpected,
    XHSNetworkRecoveryTimeout,
    XHSNoteDetailUnavailable,
    xhs_cdp_lifecycle_stop_detail,
)
from trippostcollect.platforms.xhs.manual_wait import XHSManualWaitBudgetExhausted
from trippostcollect.runtime.browser import CDPBrowserLifecycleError

from .support import XiaoHongShuCrawler, config, make_capture

KEYWORD = "青岛旅游"
LOGIN_EXPIRED = {"code": -101, "success": False, "msg": "登录已过期"}


def lifecycle_error(code: str, *, stage: str = "search") -> CDPBrowserLifecycleError:
    return CDPBrowserLifecycleError(
        {
            "code": code,
            "kind": code,
            "pid": 4321,
            "returncode": 23 if code == "xhs_browser_process_exited" else None,
        },
        stage=stage,
    )


def valid_note(note_id: str) -> dict:
    return {
        "note_id": note_id,
        "title": f"note {note_id}",
        "desc": "青岛 body",
        "content_detail_status": "detail_observed",
        "content_detail_source": "note_detail",
        "time": 1_700_000_000_000,
        "user": {"user_id": f"author-{note_id}", "nickname": "author"},
        "image_list": [{"url_default": "https://example.test/image.jpg"}],
        "creator_profile": {"fans_count": 100},
        "interact_info": {
            "liked_count": 1,
            "collected_count": 2,
            "comment_count": 3,
            "share_count": 4,
        },
        "xsec_token": "token",
    }


async def _no_sleep(*_args, **_kwargs) -> None:
    return None


def prepare_crawler(monkeypatch, tmp_path, *, items, pages=None):
    """目标会话第 1 页为 `items`（或多页 `pages`），其后是 has_more=false 的空页。"""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setattr(config, "KEYWORDS", KEYWORD)
    monkeypatch.setattr(xhs_scan, "merge_pending_behavior_evidence", lambda pending, ports: None)
    monkeypatch.setattr(xhs_scan, "XHS_SEARCH_LOAD_WAIT_SECONDS", 0.05)

    site = XhsFakeSite(KEYWORD, pages if pages is not None else [[note(item) for item in items]])
    crawler = XiaoHongShuCrawler()
    crawler.context_page = site.page
    crawler.browser_context = site.context
    crawler.capture = make_capture(tmp_path, context=site.context)
    crawler.capture.install(site.page)
    crawler._scan_sleep = _no_sleep

    async def guarded_pause(stage, minimum, maximum):
        start = crawler.capture.pause_started()
        continuity = "completed" if stage in {"search_results", "search_page"} else "n/a"
        crawler.capture.pause_finished(stage, start, 0.0, continuity)
        return 0.0

    async def behavior(keyword):
        site.wheel(600)
        site.wheel(-600)
        return {"status": "completed"}

    crawler._guarded_pause = guarded_pause
    crawler._run_human_behavior_on_primary_page = behavior
    crawler._popup_checkpoint_state = AsyncMock(
        return_value={"closed": False, "visible_text": "ok", "manual_markers": [], "terminal": ""}
    )
    detail_ids = []

    async def fetch_detail(*, note_id, **kwargs):
        detail_ids.append(note_id)
        return valid_note(note_id)

    crawler.get_note_detail_async_task = fetch_detail
    crawler._maybe_run_post_interaction = AsyncMock(return_value=None)
    crawler.enrich_note_creator = AsyncMock(return_value=None)
    crawler.get_notice_media = AsyncMock(return_value=None)
    crawler.is_video_note = lambda note: False
    monkeypatch.setattr(crawler, "update_xhs_note", AsyncMock(return_value=None))
    monkeypatch.setattr(
        xhs_core,
        "_normalized_creator_item",
        lambda user_id, profile, **_injected: {"fans_count": profile["fans_count"]},
    )
    return crawler, detail_ids, state_path, site


def events_of(state_path, kind: str) -> list[dict]:
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    return [event["details"] for event in events if event["type"] == kind]


def last_stop(state_path) -> dict:
    return events_of(state_path, "adaptive_search_stopped")[-1]


def assert_runtime_stop(stopped: dict, detail: str, *, source_page=1) -> None:
    assert stopped["stop_reason"] == "runtime_failed"
    assert stopped["stop_detail"] == detail
    assert stopped["discovery_phase"] == "full_scan"
    assert stopped["source_page"] == source_page
    assert stopped["resume_page"] is None and stopped["resume_cursor"] is None
    assert stopped["batch_complete"] is False
    assert "source_visit_id" not in stopped


@pytest.mark.asyncio
async def test_known_note_is_skipped_before_detail_request(monkeypatch, tmp_path):
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        conn.execute("INSERT INTO web_posts VALUES ('xhs', 'known-note', NULL)")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    crawler, detail_ids, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=["known-note", "new-note"],
    )

    await crawler.search()

    assert detail_ids == ["new-note"]
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["resume_page"] is None
    assert stopped["batch_complete"] is True


@pytest.mark.asyncio
async def test_all_unknown_candidates_are_processed_before_exhaustion(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=["first", "second"],
    )

    await crawler.search()

    assert detail_ids == ["first", "second"]
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["stop_detail"] == "has_more_false"
    assert stopped["discovery_phase"] == "full_scan"
    assert stopped["source_page"] == 2 and stopped["source_has_more"] is False
    assert stopped["source_cursor"] == site.search_id
    assert stopped["resume_page"] is None and stopped["resume_cursor"] is None
    assert stopped["batch_complete"] is True
    assert stopped["candidate_identities"] == ["first", "second"]
    assert stopped["source_visit_id"].startswith(crawler.capture.ledger.ledger_id)


@pytest.mark.asyncio
async def test_browser_context_close_is_recorded_as_runtime_failure(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["new-note"])
    crawler.enrich_note_creator = AsyncMock(
        side_effect=PlaywrightError(
            "BrowserContext.new_page: Target page, context or browser has been closed"
        )
    )

    await crawler.search()

    stopped = last_stop(state_path)
    assert_runtime_stop(stopped, "browser_context_closed")
    assert stopped["source_cursor"] == site.search_id


@pytest.mark.asyncio
async def test_creator_security_limit_is_recorded_as_terminal_runtime_failure(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["security-limited-note"])
    crawler.enrich_note_creator = AsyncMock(
        side_effect=PlatformRuntimeError(
            "XHS creator profile is blocked by a platform security limit",
            code="platform_security_limit_300011",
        )
    )

    await crawler.search()

    assert not events_of(state_path, "candidate_skipped")
    stopped = last_stop(state_path)
    assert_runtime_stop(stopped, "platform_security_limit_300011")
    assert stopped["source_cursor"] == site.search_id
    assert stopped["candidate_identities"] == []


@pytest.mark.asyncio
async def test_retryable_detail_failure_is_run_only_and_search_continues(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=None, pages=[[note("retry-detail")], [note("later")]],
    )
    crawler.get_note_detail_async_task = AsyncMock(
        side_effect=[XHSNoteDetailUnavailable("retry-detail", "api_and_html_empty", attempts=3), valid_note("later")]
    )

    await crawler.search()

    crawler.update_xhs_note.assert_awaited_once()
    (skipped,) = events_of(state_path, "candidate_skipped")
    assert skipped["identity"] == "retry-detail"
    assert skipped["failure_scope"] == "post"
    assert skipped["attempts"] == 3
    assert skipped["retryable"] is True
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    # 可重试的候选失败只算本轮已决定，不进入跨轮记忆。
    assert stopped["candidate_identities"] == ["later"]


@pytest.mark.asyncio
async def test_ip_block_stops_run_without_skipping_or_marking_seen(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["blocked-detail"])
    crawler.get_note_detail_async_task = AsyncMock(
        side_effect=RetryError(
            Future.construct(3, IPBlockError("Network connection error, code 300012"), has_exception=True)
        )
    )

    await crawler.search()

    crawler.update_xhs_note.assert_not_awaited()
    assert not events_of(state_path, "candidate_skipped")
    stopped = last_stop(state_path)
    assert_runtime_stop(stopped, "ip_blocked_300012")
    assert stopped["candidate_identities"] == []


@pytest.mark.asyncio
async def test_detail_fallback_attempt_count_matches_actual_requests(monkeypatch) -> None:
    crawler = XiaoHongShuCrawler()
    crawler._guarded_pause = AsyncMock(return_value=0.0)
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_by_id.return_value = None
    crawler.xhs_client.get_note_by_id_from_html.side_effect = RetryError(
        Future.construct(
            3,
            DataFetchError("temporary HTML detail failure"),
            has_exception=True,
        )
    )

    with pytest.raises(XHSNoteDetailUnavailable) as exc_info:
        await crawler.get_note_detail_async_task(
            note_id="attempt-count",
            xsec_source="pc_search",
            xsec_token="token",
            semaphore=xhs_core.asyncio.Semaphore(1),
        )

    assert exc_info.value.attempts == 4
    assert crawler.xhs_client.get_note_by_id.await_count == 1
    assert crawler.xhs_client.get_note_by_id_from_html.await_count == 1


@pytest.mark.asyncio
async def test_image_failure_is_recorded_and_later_xhs_candidate_continues(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=["retry-image", "success-image"],
    )
    crawler.get_notice_media = AsyncMock(
        side_effect=[XHSImageDownloadError("retry-image", 0, "image_download_retryable", attempts=3), None]
    )

    await crawler.search()

    crawler.update_xhs_note.assert_awaited_once()
    (skipped,) = events_of(state_path, "candidate_skipped")
    assert skipped["identity"] == "retry-image"
    assert skipped["failure_scope"] == "image"
    assert skipped["attempts"] == 3
    assert skipped["retryable"] is True
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["candidate_identities"] == ["success-image"]


@pytest.mark.asyncio
async def test_creator_failure_is_recorded_and_later_xhs_candidate_continues(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=["retry-creator", "success-creator"],
    )
    crawler.enrich_note_creator = AsyncMock(
        side_effect=[XHSCreatorProfileUnavailable("author-retry", attempts=3), None]
    )

    await crawler.search()

    crawler.update_xhs_note.assert_awaited_once()
    assert crawler.get_notice_media.await_count == 1
    (skipped,) = events_of(state_path, "candidate_skipped")
    assert skipped["identity"] == "retry-creator"
    assert skipped["failure_scope"] == "post"
    assert skipped["error_code"] == "creator_profile_unavailable"
    assert skipped["attempts"] == 3
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["candidate_identities"] == ["success-creator"]


@pytest.mark.asyncio
async def test_terminal_image_failure_is_recorded_and_later_candidate_continues(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=["terminal-image", "must-not-store"],
    )
    crawler.get_notice_media = AsyncMock(
        side_effect=[XHSImageDownloadError("terminal-image", 0, "image_decode_failed", attempts=1), None]
    )

    await crawler.search()

    crawler.update_xhs_note.assert_awaited_once()
    assert crawler.get_notice_media.await_count == 2
    (skipped,) = events_of(state_path, "candidate_skipped")
    assert skipped["identity"] == "terminal-image"
    assert skipped["failure_scope"] == "image"
    assert skipped["retryable"] is False
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["candidate_identities"] == ["must-not-store", "terminal-image"]


def expire_login_on_page_two(site: XhsFakeSite, *, times: int = 1) -> None:
    site.search_overrides[2] = [dict(LOGIN_EXPIRED) for _ in range(times)]


@pytest.mark.asyncio
async def test_search_login_expiry_waits_and_retriggers_the_same_session(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    expire_login_on_page_two(site)
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=True)

    await crawler.search()

    crawler._wait_for_midrun_login_recovery.assert_awaited_once_with()
    assert [request["page"] for request in site.requests if "filters" in request] == [1, 2, 2]
    assert len({request["search_id"] for request in site.requests if "filters" in request}) == 1
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["stop_detail"] == "has_more_false"


@pytest.mark.asyncio
async def test_search_login_expiry_without_recovery_stops_as_login_required(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    expire_login_on_page_two(site)
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=False)

    await crawler.search()

    assert_runtime_stop(last_stop(state_path), "login_required", source_page=ANY)


@pytest.mark.asyncio
async def test_main_page_closed_in_search_login_recovery_stops_at_current_page(monkeypatch, tmp_path):
    """第 1 页完成并发布；第 2 页登录过期，同时主页面关闭（浏览器仍存活）。"""
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(
        monkeypatch, tmp_path, items=None, pages=[[note("note-confirmed")], [note("never")]],
    )
    closed = {"value": False}
    site.page.is_closed = lambda: closed["value"]

    async def still_behavior(keyword):
        return {"status": "completed"}

    # 行为阶段不滚动：第 2 页只在第 1 页处理完、扫描向下滚动时才请求。
    crawler._run_human_behavior_on_primary_page = still_behavior

    def close_on_page_two(number, request_json):
        if number == 2 and "filters" in request_json:
            closed["value"] = True

    site.before_respond = close_on_page_two
    expire_login_on_page_two(site)
    crawler.cdp_manager = Mock(assert_alive=Mock())
    published = []
    monkeypatch.setattr(app_events, "_batch_publisher", published.append)

    await crawler.search()

    crawler.cdp_manager.assert_alive.assert_any_call("midrun_login_recovery")
    assert detail_ids == ["note-confirmed"]
    assert not events_of(state_path, "candidate_skipped")
    completed = events_of(state_path, "adaptive_batch_completed")
    assert [(batch["source_page"], batch["resume_page"]) for batch in completed] == [(1, None)]
    assert published == completed
    assert_runtime_stop(last_stop(state_path), "main_page_closed", source_page=2)


class TargetClosedError(PlaywrightError):
    """与 Playwright 同名的关闭异常；分类按类名识别。"""


LOGIN_RECOVERY_RUN_FAILURES = [
    (
        PlatformRuntimeError("xhs_rate_limited_terminal:midrun_login_recovery", code="xhs_rate_limited_terminal"),
        "xhs_rate_limited_terminal",
    ),
    (XHSManualWaitBudgetExhausted(), "xhs_manual_checkpoint_budget_exhausted"),
    (XHSNetworkRecoveryTimeout("midrun_login_confirmation", 600.0), "network_recovery_timeout"),
    (IPBlockError("Network connection error"), "ip_blocked_300012"),
    (PlaywrightError("Protocol error (Runtime.evaluate): boom"), "browser_runtime_failed"),
    (TargetClosedError("Target page, context or browser has been closed"), "browser_context_closed"),
    (XHSMainPageClosedUnexpected(stage="midrun_login_recovery"), "main_page_closed"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "stop_detail"),
    LOGIN_RECOVERY_RUN_FAILURES,
    ids=[
        "terminal_popup", "manual_budget", "network_timeout", "ip_block",
        "playwright", "target_closed", "main_page_closed",
    ],
)
async def test_search_login_recovery_run_failure_writes_structured_stop(
    monkeypatch, tmp_path, failure, stop_detail,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=[])
    site.search_overrides[1] = [dict(LOGIN_EXPIRED)]
    crawler._wait_for_midrun_login_recovery = AsyncMock(side_effect=failure)

    await crawler.search()

    crawler._wait_for_midrun_login_recovery.assert_awaited_once_with()
    assert detail_ids == []
    assert not events_of(state_path, "candidate_skipped")
    assert not events_of(state_path, "adaptive_batch_completed")
    (stopped,) = events_of(state_path, "adaptive_search_stopped")
    assert stopped["stop_reason"] == "runtime_failed"
    assert stopped["stop_detail"] == stop_detail
    assert stopped["resume_page"] is None and stopped["batch_complete"] is False


@pytest.mark.asyncio
async def test_search_login_recovery_terminal_popup_stops_through_real_recovery(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    expire_login_on_page_two(site)
    crawler.cdp_manager = Mock(assert_alive=Mock())
    crawler._popup_checkpoint_state = AsyncMock(
        return_value={"closed": False, "visible_text": "访问频繁", "manual_markers": [], "terminal": "rate_limited"}
    )
    crawler._profile_ui_visible = AsyncMock(return_value=False)

    await crawler.search()

    crawler._popup_checkpoint_state.assert_awaited_once()
    crawler._profile_ui_visible.assert_not_awaited()
    assert_runtime_stop(last_stop(state_path), "xhs_rate_limited_terminal", source_page=ANY)


@pytest.mark.asyncio
async def test_search_login_recovery_unclassified_error_still_escapes(monkeypatch, tmp_path):
    # 未识别异常照常上抛，不伪造停止事件或来源耗尽。
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    expire_login_on_page_two(site)
    failure = ValueError("unexpected recovery bug")
    crawler._wait_for_midrun_login_recovery = AsyncMock(side_effect=failure)

    with pytest.raises(ValueError) as exc_info:
        await crawler.search()

    assert exc_info.value is failure
    assert not events_of(state_path, "adaptive_search_stopped")


POST_INTERACTION_RUN_FAILURES = [
    (lifecycle_error("xhs_browser_context_closed_unexpected", stage="post_interaction"), "browser_context_closed"),
    (lifecycle_error("xhs_browser_process_exited", stage="post_interaction"), "browser_process_exited"),
    (lifecycle_error("xhs_cdp_disconnected_unexpected", stage="post_interaction"), "cdp_disconnected"),
    (XHSNetworkRecoveryTimeout("post_interaction", 600.0), "network_recovery_timeout"),
]


def prepare_post_interaction(monkeypatch, tmp_path, failure):
    """真实帖子互动入口；互动辅助页导航按用例抛错，后续互动步骤不会到达。"""
    crawler, detail_ids, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["note-interacted"])
    del crawler._maybe_run_post_interaction
    crawler.post_interaction_mode = "like"
    crawler.post_interaction_attempted = False
    interaction_page = object()
    crawler._new_guarded_page = AsyncMock(return_value=interaction_page)
    crawler._goto_with_deadline = AsyncMock(side_effect=failure)
    crawler._close_page_with_deadline = AsyncMock()
    return crawler, state_path, interaction_page


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "stop_detail"), POST_INTERACTION_RUN_FAILURES)
async def test_post_interaction_lifecycle_or_network_timeout_fails_the_run(
    monkeypatch, tmp_path, failure, stop_detail
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, state_path, interaction_page = prepare_post_interaction(monkeypatch, tmp_path, failure)

    await crawler.search()

    crawler._close_page_with_deadline.assert_awaited_once_with(
        interaction_page, reason="post_interaction_cleanup"
    )
    crawler.enrich_note_creator.assert_not_awaited()
    crawler.update_xhs_note.assert_not_awaited()
    assert not events_of(state_path, "candidate_skipped")
    assert not events_of(state_path, "adaptive_batch_completed")
    assert_runtime_stop(last_stop(state_path), stop_detail)


@pytest.mark.asyncio
async def test_post_interaction_helper_page_closure_does_not_stop_crawl(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    failure = PlaywrightError("Target page, context or browser has been closed")
    crawler, state_path, interaction_page = prepare_post_interaction(monkeypatch, tmp_path, failure)

    await crawler.search()

    crawler._close_page_with_deadline.assert_awaited_once_with(
        interaction_page, reason="post_interaction_cleanup"
    )
    crawler.enrich_note_creator.assert_awaited_once()
    crawler.update_xhs_note.assert_awaited_once()
    assert last_stop(state_path)["stop_reason"] == "source_exhausted"


@pytest.mark.asyncio
async def test_post_interaction_plain_runtime_error_does_not_stop_crawl(monkeypatch, tmp_path):
    # 普通 RuntimeError 只是互动自身失败，记录告警后照常补全、入库并继续到来源耗尽。
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    failure = RuntimeError("xhs_post_interaction_button_missing")
    crawler, state_path, interaction_page = prepare_post_interaction(monkeypatch, tmp_path, failure)

    await crawler.search()

    crawler._close_page_with_deadline.assert_awaited_once_with(
        interaction_page, reason="post_interaction_cleanup"
    )
    assert crawler.post_interaction_attempted is True
    crawler.enrich_note_creator.assert_awaited_once()
    crawler.update_xhs_note.assert_awaited_once()
    assert not events_of(state_path, "candidate_skipped")
    assert last_stop(state_path)["stop_reason"] == "source_exhausted"


def attach_network_clock(monkeypatch, crawler, *, wait_seconds: float) -> list[float]:
    crawler._record_network_recovery_event = AsyncMock()
    clock = {"now": 50.0}
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock["now"]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    crawler._popup_monotonic = monotonic
    crawler._popup_sleep = sleep
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_WAIT_SECONDS", str(wait_seconds))
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MIN_SECONDS", "2")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MAX_SECONDS", "4")
    return sleeps


@pytest.mark.asyncio
async def test_search_disconnect_pauses_and_rescrolls_in_the_same_browser_session(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    site.search_overrides[2] = ["net::ERR_INTERNET_DISCONNECTED"]
    sleeps = attach_network_clock(monkeypatch, crawler, wait_seconds=10.0)
    context, page = crawler.browser_context, crawler.context_page

    await crawler.search()

    assert [request["page"] for request in site.requests if "filters" in request] == [1, 2, 2]
    assert sleeps == [2.0]
    assert crawler.browser_context is context and crawler.context_page is page
    outcomes = [call.kwargs["outcome"] for call in crawler._record_network_recovery_event.await_args_list]
    assert outcomes == ["network_paused", "network_recovered"]
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["stop_detail"] == "has_more_false"


@pytest.mark.asyncio
async def test_search_disconnect_timeout_is_a_runtime_stop(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    site.search_overrides[2] = ["net::ERR_INTERNET_DISCONNECTED"] * 5
    sleeps = attach_network_clock(monkeypatch, crawler, wait_seconds=5.0)

    await crawler.search()

    assert sleeps == [2.0, 3.0]
    assert_runtime_stop(last_stop(state_path), "network_recovery_timeout", source_page=ANY)


@pytest.mark.asyncio
async def test_unrecoverable_search_request_failure_is_a_runtime_stop(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    site.search_overrides[2] = ["net::ERR_ABORTED"]

    await crawler.search()

    assert_runtime_stop(last_stop(state_path), "search_request_failed", source_page=ANY)


@pytest.mark.parametrize(
    ("code", "stop_detail"),
    [
        ("xhs_browser_process_exited", "browser_process_exited"),
        ("xhs_browser_context_closed_unexpected", "browser_context_closed"),
        ("xhs_cdp_disconnected_unexpected", "cdp_disconnected"),
        ("xhs_main_page_closed_unexpected", "main_page_closed"),
    ],
)
@pytest.mark.asyncio
async def test_cdp_lifecycle_failure_stops_without_retry_or_relaunch(code, stop_detail, monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    crawler._assert_primary_page_alive = Mock(side_effect=lifecycle_error(code, stage="full_scan"))
    crawler.launch_browser_with_cdp = AsyncMock()

    await crawler.search()

    assert detail_ids == []
    crawler.launch_browser_with_cdp.assert_not_awaited()
    assert not events_of(state_path, "adaptive_batch_completed")
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "runtime_failed"
    assert stopped["stop_detail"] == stop_detail
    assert stopped["resume_page"] is None and stopped["batch_complete"] is False
    assert stopped.get("candidate_identities") in (None, [])


def test_main_page_closed_is_lifecycle_failure_with_unchanged_message() -> None:
    exc = XHSMainPageClosedUnexpected(stage="behavior")

    assert isinstance(exc, CDPBrowserLifecycleError)
    assert str(exc) == "xhs_main_page_closed_unexpected:stage=behavior"
    assert xhs_cdp_lifecycle_stop_detail(exc) == "main_page_closed"
    assert xhs_cdp_lifecycle_stop_detail(lifecycle_error("xhs_unknown_lifecycle")) == "browser_runtime_failed"


@pytest.mark.asyncio
async def test_cdp_disconnect_during_network_pause_is_a_lifecycle_stop(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["first"])
    site.search_overrides[2] = ["net::ERR_INTERNET_DISCONNECTED"]
    crawler._pause_for_network_recovery = AsyncMock(
        side_effect=lifecycle_error("xhs_cdp_disconnected_unexpected", stage="full_scan")
    )

    await crawler.search()

    assert crawler._pause_for_network_recovery.await_count == 1
    assert_runtime_stop(last_stop(state_path), "cdp_disconnected", source_page=ANY)


@pytest.mark.asyncio
async def test_cdp_process_exit_from_detail_is_not_candidate_failure(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["new-note"])
    crawler.get_note_detail_async_task = AsyncMock(
        side_effect=lifecycle_error("xhs_browser_process_exited", stage="detail:new-note")
    )

    await crawler.search()

    assert crawler.get_note_detail_async_task.await_count == 1
    assert not events_of(state_path, "candidate_skipped")
    assert not events_of(state_path, "adaptive_batch_completed")
    assert_runtime_stop(last_stop(state_path), "browser_process_exited")


@pytest.mark.asyncio
async def test_cdp_context_close_from_creator_is_not_candidate_failure(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path, site = prepare_crawler(monkeypatch, tmp_path, items=["new-note"])
    crawler.enrich_note_creator = AsyncMock(
        side_effect=lifecycle_error("xhs_browser_context_closed_unexpected", stage="creator:new-note")
    )

    await crawler.search()

    assert crawler.enrich_note_creator.await_count == 1
    assert not events_of(state_path, "candidate_skipped")
    assert not events_of(state_path, "adaptive_batch_completed")
    assert_runtime_stop(last_stop(state_path), "browser_context_closed")


@pytest.mark.asyncio
async def test_detail_transport_outage_retries_same_detail_without_html_fallback(monkeypatch) -> None:
    crawler = XiaoHongShuCrawler()
    page = SimpleNamespace(is_closed=lambda: False, url="https://www.xiaohongshu.com")
    crawler.context_page = page
    crawler.browser_context = SimpleNamespace(pages=[page], new_page=AsyncMock())
    attach_network_clock(monkeypatch, crawler, wait_seconds=10.0)
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/sns/web/v1/feed")
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_by_id.side_effect = [
        RetryError(
            Future.construct(3, httpx.ConnectError("temporary disconnect", request=request), has_exception=True)
        ),
        valid_note("network-detail"),
    ]
    crawler._guarded_pause = AsyncMock(return_value=0.0)

    result = await crawler.get_note_detail_async_task(
        note_id="network-detail",
        xsec_source="pc_search",
        xsec_token="token",
        semaphore=xhs_core.asyncio.Semaphore(1),
    )

    assert result and result["note_id"] == "network-detail"
    assert crawler.xhs_client.get_note_by_id.await_count == 2
    crawler.xhs_client.get_note_by_id_from_html.assert_not_awaited()
