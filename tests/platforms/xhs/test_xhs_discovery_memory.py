from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from playwright.async_api import Error as PlaywrightError
from tenacity import Future, RetryError
from trippostcollect.platforms.xhs import core as xhs_core
from trippostcollect.platforms.xhs.errors import (
    DataFetchError,
    IPBlockError,
    PlatformRuntimeError,
    XHSCreatorProfileUnavailable,
    XHSImageDownloadError,
    XHSMainPageClosedUnexpected,
    XHSNoteDetailUnavailable,
    xhs_cdp_lifecycle_stop_detail,
)
from trippostcollect.platforms.xhs.manual_wait import XHSManualWaitBudgetExhausted
from trippostcollect.runtime.browser import CDPBrowserLifecycleError

from .support import XiaoHongShuCrawler, config


class SearchClient:
    def __init__(self, items):
        self.items = items
        self.calls = []

    async def get_note_by_keyword(self, **kwargs):
        self.calls.append(kwargs)
        return {"items": self.items, "has_more": False}


class LoginExpiredSearchClient:
    def __init__(self, *, recover: bool):
        self.recover = recover
        self.calls = []

    async def get_note_by_keyword(self, **kwargs):
        self.calls.append(kwargs)
        if not self.recover or len(self.calls) == 1:
            failure = DataFetchError("登录已过期")
            raise RetryError(Future.construct(3, failure, has_exception=True))
        return {"items": [], "has_more": False}


class NetworkOutageSearchClient:
    def __init__(self, *, recover: bool):
        self.recover = recover
        self.calls = []

    async def get_note_by_keyword(self, **kwargs):
        self.calls.append(kwargs)
        if not self.recover or len(self.calls) == 1:
            request = httpx.Request(
                "GET",
                "https://edith.xiaohongshu.com/api/sns/web/v1/search/notes",
            )
            raise RetryError(
                Future.construct(
                    3,
                    httpx.ReadTimeout("temporary disconnect", request=request),
                    has_exception=True,
                )
            )
        return {"items": [], "has_more": False}


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


def prepare_crawler(monkeypatch, tmp_path, *, items, start_page=3):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", "saved-search-id")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.setattr(config, "START_PAGE", start_page)
    monkeypatch.setattr(config, "KEYWORDS", "青岛旅游")
    monkeypatch.setattr(config, "SORT_TYPE", "")
    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)

    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = SearchClient(items)
    detail_ids = []

    async def fetch_detail(*, note_id, **kwargs):
        detail_ids.append(note_id)
        return valid_note(note_id)

    crawler.get_note_detail_async_task = fetch_detail
    crawler._guarded_pause = AsyncMock(return_value=0.0)
    crawler._maybe_run_post_interaction = AsyncMock(return_value=None)
    crawler.enrich_note_creator = AsyncMock(return_value=None)
    crawler.get_notice_media = AsyncMock(return_value=None)
    crawler.is_video_note = lambda note: False
    crawler.note_detail_summaries = lambda notes: [{"note_id": note["note_id"]} for note in notes]
    monkeypatch.setattr(crawler, "update_xhs_note", AsyncMock(return_value=None))
    monkeypatch.setattr(
        xhs_core,
        "_normalized_creator_item",
        lambda user_id, profile, **_injected: {"fans_count": profile["fans_count"]},
    )
    return crawler, detail_ids, state_path


def attach_network_clock(
    monkeypatch: pytest.MonkeyPatch,
    crawler: XiaoHongShuCrawler,
    *,
    wait_seconds: float,
) -> tuple[object, object, list[float]]:
    page = SimpleNamespace(is_closed=lambda: False, url="https://www.xiaohongshu.com")
    context = SimpleNamespace(pages=[page], new_page=AsyncMock())
    crawler.context_page = page
    crawler.browser_context = context
    crawler._record_network_recovery_event = AsyncMock()
    crawler.launch_browser = AsyncMock()
    crawler.launch_browser_with_cdp = AsyncMock()

    clock = {"now": 50.0}
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock["now"]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    crawler._popup_monotonic = monotonic
    crawler._popup_sleep = sleep
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_NETWORK_WAIT_SECONDS",
        str(wait_seconds),
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MIN_SECONDS", "2")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MAX_SECONDS", "4")
    return context, page, sleeps


@pytest.mark.asyncio
async def test_known_note_is_skipped_before_detail_request(monkeypatch, tmp_path):
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        conn.execute("INSERT INTO web_posts VALUES ('xhs', 'known-note', NULL)")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    crawler, detail_ids, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "known-note"}, {"id": "new-note"}],
    )

    await crawler.search()

    assert detail_ids == ["new-note"]
    assert crawler.xhs_client.calls[0]["page"] == 3
    assert crawler.xhs_client.calls[0]["search_id"] == "saved-search-id"
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["resume_page"] == 4
    assert stopped["details"]["batch_complete"] is True


@pytest.mark.asyncio
async def test_all_unknown_candidates_are_processed_before_exhaustion(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "first"}, {"id": "second"}],
    )

    await crawler.search()

    assert detail_ids == ["first", "second"]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 4
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["batch_complete"] is True
    assert stopped["details"]["candidate_identities"] == ["first", "second"]


@pytest.mark.asyncio
async def test_top_refresh_uses_fresh_search_id_before_saved_frontier(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "top-new"}],
        start_page=1,
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "2")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "1")
    monkeypatch.setattr(xhs_core, "get_search_id", lambda: "fresh-search-id")

    await crawler.search()

    assert detail_ids == ["top-new"]
    assert [(call["page"], call["search_id"]) for call in crawler.xhs_client.calls] == [
        (1, "fresh-search-id")
    ]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_detail"] == "saved_source_exhausted"


@pytest.mark.asyncio
async def test_browser_context_close_is_recorded_as_resumable_runtime_failure(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "new-note"}],
    )
    crawler.enrich_note_creator = AsyncMock(
        side_effect=PlaywrightError(
            "BrowserContext.new_page: Target page, context or browser has been closed"
        )
    )

    await crawler.search()

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == "browser_context_closed"
    assert stopped["details"]["resume_page"] == 3
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["batch_complete"] is False


@pytest.mark.asyncio
async def test_creator_security_limit_is_recorded_as_terminal_runtime_failure(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "security-limited-note"}],
    )
    crawler.enrich_note_creator = AsyncMock(
        side_effect=PlatformRuntimeError(
            "XHS creator profile is blocked by a platform security limit",
            code="platform_security_limit_300011",
        )
    )

    await crawler.search()

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    assert not [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == "platform_security_limit_300011"
    assert stopped["details"]["source_page"] == 3
    assert stopped["details"]["source_cursor"] == "saved-search-id"
    assert stopped["details"]["resume_page"] == 3
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["batch_complete"] is False
    assert stopped["details"]["candidate_identities"] == []


@pytest.mark.asyncio
async def test_detail_failure_is_recorded_seen_and_search_continues(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "retry-detail"}],
    )
    crawler.get_note_detail_async_task = AsyncMock(
        side_effect=XHSNoteDetailUnavailable(
            "retry-detail", "api_and_html_empty", attempts=3
        )
    )
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_by_keyword.side_effect = [
        {"items": [{"id": "retry-detail"}], "has_more": True},
        {"items": [], "has_more": False},
    ]

    await crawler.search()

    crawler.update_xhs_note.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert skipped[0]["details"]["identity"] == "retry-detail"
    assert skipped[0]["details"]["failure_scope"] == "post"
    assert skipped[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 5
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["candidate_identities"] == ["retry-detail"]


@pytest.mark.asyncio
async def test_ip_block_stops_run_without_skipping_or_marking_seen(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "blocked-detail"}],
    )
    crawler.get_note_detail_async_task = AsyncMock(
        side_effect=RetryError(
            Future.construct(
                3,
                IPBlockError("Network connection error, code 300012"),
                has_exception=True,
            )
        )
    )

    await crawler.search()

    crawler.update_xhs_note.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    assert not [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == "ip_blocked_300012"
    assert stopped["details"]["candidate_identities"] == []


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
async def test_image_failure_is_recorded_and_later_xhs_candidate_continues(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "retry-image"}, {"id": "success-image"}],
    )
    crawler.get_notice_media = AsyncMock(
        side_effect=[
            XHSImageDownloadError(
                "retry-image", 0, "image_download_retryable", attempts=3
            ),
            None,
        ]
    )

    await crawler.search()

    crawler.update_xhs_note.assert_awaited_once()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert skipped[0]["details"]["identity"] == "retry-image"
    assert skipped[0]["details"]["failure_scope"] == "image"
    assert skipped[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 4
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["candidate_identities"] == [
        "retry-image",
        "success-image",
    ]


@pytest.mark.asyncio
async def test_creator_failure_is_recorded_and_later_xhs_candidate_continues(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "retry-creator"}, {"id": "success-creator"}],
    )
    crawler.enrich_note_creator = AsyncMock(
        side_effect=[
            XHSCreatorProfileUnavailable("author-retry", attempts=3),
            None,
        ]
    )

    await crawler.search()

    crawler.update_xhs_note.assert_awaited_once()
    assert crawler.get_notice_media.await_count == 1
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert skipped[0]["details"]["identity"] == "retry-creator"
    assert skipped[0]["details"]["failure_scope"] == "post"
    assert skipped[0]["details"]["error_code"] == "creator_profile_unavailable"
    assert skipped[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["candidate_identities"] == [
        "retry-creator",
        "success-creator",
    ]


@pytest.mark.asyncio
async def test_terminal_image_failure_is_recorded_and_later_candidate_continues(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "terminal-image"}, {"id": "must-not-store"}],
    )
    crawler.get_notice_media = AsyncMock(
        side_effect=[
            XHSImageDownloadError(
                "terminal-image", 0, "image_decode_failed", attempts=1
            ),
            None,
        ]
    )

    await crawler.search()

    crawler.update_xhs_note.assert_awaited_once()
    assert crawler.get_notice_media.await_count == 2
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    assert skipped[0]["details"]["identity"] == "terminal-image"
    assert skipped[0]["details"]["failure_scope"] == "image"
    assert skipped[0]["details"]["retryable"] is False
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 4
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["candidate_identities"] == [
        "must-not-store",
        "terminal-image",
    ]


@pytest.mark.asyncio
async def test_wrapped_login_expiry_waits_and_retries_same_search_page(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[],
    )
    crawler.xhs_client = LoginExpiredSearchClient(recover=True)
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=True)

    await crawler.search()

    assert [call["page"] for call in crawler.xhs_client.calls] == [3, 3]
    crawler._wait_for_midrun_login_recovery.assert_awaited_once_with("青岛旅游")
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["stop_detail"] == "has_more_false"


@pytest.mark.asyncio
async def test_wrapped_login_expiry_timeout_keeps_current_page_as_frontier(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[],
    )
    crawler.xhs_client = LoginExpiredSearchClient(recover=False)
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=False)

    await crawler.search()

    assert [call["page"] for call in crawler.xhs_client.calls] == [3]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == "login_required"
    assert stopped["details"]["resume_page"] == 3
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["batch_complete"] is False


@pytest.mark.asyncio
async def test_shared_manual_budget_exhaustion_is_a_fixed_runtime_stop(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[],
    )
    crawler.xhs_client = LoginExpiredSearchClient(recover=False)
    crawler._wait_for_midrun_login_recovery = AsyncMock(
        side_effect=XHSManualWaitBudgetExhausted()
    )

    await crawler.search()

    assert [call["page"] for call in crawler.xhs_client.calls] == [3]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [
        event for event in events if event["type"] == "adaptive_search_stopped"
    ][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert (
        stopped["details"]["stop_detail"]
        == "xhs_manual_checkpoint_budget_exhausted"
    )
    assert stopped["details"]["resume_page"] == 3
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["batch_complete"] is False


@pytest.mark.asyncio
async def test_search_disconnect_retries_same_page_cursor_and_browser_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[],
    )
    crawler.xhs_client = NetworkOutageSearchClient(recover=True)
    context, page, sleeps = attach_network_clock(
        monkeypatch,
        crawler,
        wait_seconds=10.0,
    )

    await crawler.search()

    assert [call["page"] for call in crawler.xhs_client.calls] == [3, 3]
    assert [call["search_id"] for call in crawler.xhs_client.calls] == [
        "saved-search-id",
        "saved-search-id",
    ]
    assert crawler.browser_context is context
    assert crawler.context_page is page
    assert sleeps == [2.0]
    context.new_page.assert_not_awaited()
    crawler.launch_browser.assert_not_awaited()
    crawler.launch_browser_with_cdp.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["stop_detail"] == "has_more_false"


@pytest.mark.asyncio
async def test_search_disconnect_timeout_preserves_current_recovery_frontier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[],
    )
    crawler.xhs_client = NetworkOutageSearchClient(recover=False)
    context, page, sleeps = attach_network_clock(
        monkeypatch,
        crawler,
        wait_seconds=5.0,
    )

    await crawler.search()

    assert [call["page"] for call in crawler.xhs_client.calls] == [3, 3, 3]
    assert [call["search_id"] for call in crawler.xhs_client.calls] == [
        "saved-search-id",
        "saved-search-id",
        "saved-search-id",
    ]
    assert sleeps == [2.0, 3.0]
    assert crawler.browser_context is context
    assert crawler.context_page is page
    context.new_page.assert_not_awaited()
    crawler.launch_browser.assert_not_awaited()
    crawler.launch_browser_with_cdp.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == "network_recovery_timeout"
    assert stopped["details"]["resume_page"] == 3
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["batch_complete"] is False


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
async def test_cdp_lifecycle_failure_preserves_frontier_without_retry_or_relaunch(
    code: str,
    stop_detail: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, detail_ids, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[],
    )
    crawler.xhs_client.get_note_by_keyword = AsyncMock(
        side_effect=lifecycle_error(code)
    )
    crawler.launch_browser = AsyncMock()
    crawler.launch_browser_with_cdp = AsyncMock()

    await crawler.search()

    assert crawler.xhs_client.get_note_by_keyword.await_count == 1
    assert detail_ids == []
    crawler.launch_browser.assert_not_awaited()
    crawler.launch_browser_with_cdp.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    assert not any(event["type"] == "adaptive_batch_completed" for event in events)
    stopped = [
        event for event in events if event["type"] == "adaptive_search_stopped"
    ][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == stop_detail
    assert stopped["details"]["resume_page"] == 3
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["batch_complete"] is False
    assert stopped["details"].get("candidate_identities") in (None, [])


def test_main_page_closed_is_lifecycle_failure_with_unchanged_message() -> None:
    exc = XHSMainPageClosedUnexpected(stage="behavior")

    assert isinstance(exc, CDPBrowserLifecycleError)
    assert str(exc) == "xhs_main_page_closed_unexpected:stage=behavior"
    assert xhs_cdp_lifecycle_stop_detail(exc) == "main_page_closed"
    assert xhs_cdp_lifecycle_stop_detail(lifecycle_error("xhs_unknown_lifecycle")) == "browser_runtime_failed"


@pytest.mark.asyncio
async def test_cdp_disconnect_after_transport_pause_preserves_same_frontier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[],
    )
    crawler.xhs_client = NetworkOutageSearchClient(recover=False)
    crawler._pause_for_network_recovery = AsyncMock(
        side_effect=lifecycle_error(
            "xhs_cdp_disconnected_unexpected",
            stage="search:frontier:page=3",
        )
    )
    crawler.launch_browser = AsyncMock()
    crawler.launch_browser_with_cdp = AsyncMock()

    await crawler.search()

    assert [call["page"] for call in crawler.xhs_client.calls] == [3]
    assert crawler._pause_for_network_recovery.await_count == 1
    crawler.launch_browser.assert_not_awaited()
    crawler.launch_browser_with_cdp.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    assert not any(event["type"] == "adaptive_batch_completed" for event in events)
    stopped = [
        event for event in events if event["type"] == "adaptive_search_stopped"
    ][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == "cdp_disconnected"
    assert stopped["details"]["resume_page"] == 3
    assert stopped["details"]["resume_cursor"] == "saved-search-id"
    assert stopped["details"]["batch_complete"] is False


@pytest.mark.asyncio
async def test_cdp_process_exit_from_detail_gather_is_not_candidate_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "new-note", "xsec_token": "token"}],
    )
    crawler.get_note_detail_async_task = AsyncMock(
        side_effect=lifecycle_error(
            "xhs_browser_process_exited",
            stage="detail:new-note",
        )
    )

    await crawler.search()

    assert crawler.get_note_detail_async_task.await_count == 1
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    assert not any(event["type"] == "candidate_skipped" for event in events)
    assert not any(event["type"] == "adaptive_batch_completed" for event in events)
    stopped = [
        event for event in events if event["type"] == "adaptive_search_stopped"
    ][-1]
    assert stopped["details"]["stop_detail"] == "browser_process_exited"
    assert stopped["details"]["resume_page"] == 3
    assert stopped["details"]["resume_cursor"] == "saved-search-id"


@pytest.mark.asyncio
async def test_cdp_context_close_from_creator_is_not_candidate_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "new-note", "xsec_token": "token"}],
    )
    crawler.enrich_note_creator = AsyncMock(
        side_effect=lifecycle_error(
            "xhs_browser_context_closed_unexpected",
            stage="creator:new-note",
        )
    )

    await crawler.search()

    assert crawler.enrich_note_creator.await_count == 1
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    assert not any(event["type"] == "candidate_skipped" for event in events)
    assert not any(event["type"] == "adaptive_batch_completed" for event in events)
    stopped = [
        event for event in events if event["type"] == "adaptive_search_stopped"
    ][-1]
    assert stopped["details"]["stop_detail"] == "browser_context_closed"
    assert stopped["details"]["resume_page"] == 3
    assert stopped["details"]["resume_cursor"] == "saved-search-id"


@pytest.mark.asyncio
async def test_detail_transport_outage_retries_same_detail_without_html_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = XiaoHongShuCrawler()
    attach_network_clock(monkeypatch, crawler, wait_seconds=10.0)
    request = httpx.Request(
        "GET",
        "https://edith.xiaohongshu.com/api/sns/web/v1/feed",
    )
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_by_id.side_effect = [
        RetryError(
            Future.construct(
                3,
                httpx.ConnectError("temporary disconnect", request=request),
                has_exception=True,
            )
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
