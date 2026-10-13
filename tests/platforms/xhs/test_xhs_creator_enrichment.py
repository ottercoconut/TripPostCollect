from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import trippostcollect.platforms.xhs.author as xhs_author
from trippostcollect.platforms.xhs.errors import (
    IPBlockError,
    PlatformRuntimeError,
    XHSCreatorProfileUnavailable,
    XhsNoteRedo,
)
from trippostcollect.platforms.xhs.parser import XiaoHongShuExtractor
from trippostcollect.platforms.xhs.manual_wait import (
    XHSManualWaitBudget,
    XHSManualWaitBudgetExhausted,
)

from .support import XiaoHongShuCrawler, make_capture

NOTE = SimpleNamespace(note_id="note-1", detail_visit=None)


def projection(user_id: str, fans: str) -> dict:
    return {"status": "ok", "creator": {"basicInfo": {"nickname": user_id}, "interactions": [
        {"type": "fans", "name": "粉丝", "count": fans},
    ]}}


class _CreatorClient:
    def __init__(self, creator_info):
        self.creator_info = creator_info
        self.calls = []

    async def get_creator_info(self, **kwargs):
        self.calls.append(kwargs)
        return self.creator_info


@pytest.fixture
def crawler(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS", "1")
    instance = XiaoHongShuCrawler()
    instance._guarded_pause = AsyncMock(return_value=0.0)
    instance.context_page = Mock()
    instance.context_page.is_closed.return_value = False
    instance.context_page.bring_to_front = AsyncMock()
    instance._popup_checkpoint_state = AsyncMock(return_value={})
    instance.capture = make_capture(tmp_path)
    return instance


@pytest.mark.asyncio
async def test_profile_ui_visible_accepts_current_button_sidebar_variant(crawler):
    original_layout = Mock()
    original_layout.count = AsyncMock(return_value=0)
    current_layout = Mock()
    current_layout.count = AsyncMock(return_value=1)
    generic_current_layout = Mock()
    generic_current_layout.count = AsyncMock(return_value=1)
    crawler.context_page = Mock()
    crawler.context_page.is_closed.return_value = False
    crawler.context_page.locator.side_effect = [
        original_layout,
        current_layout,
        generic_current_layout,
    ]

    assert await crawler._profile_ui_visible() is True
    assert crawler.context_page.locator.call_count == 2


@pytest.mark.asyncio
async def test_profile_ui_visible_rejects_logged_out_sidebar(crawler):
    locator = Mock()
    locator.count = AsyncMock(return_value=0)
    crawler.context_page = Mock()
    crawler.context_page.is_closed.return_value = False
    crawler.context_page.locator.side_effect = [locator, locator, locator]

    assert await crawler._profile_ui_visible() is False


@pytest.mark.asyncio
async def test_profile_probe_does_not_adopt_newer_signed_in_auxiliary(crawler):
    primary = crawler.context_page
    primary.locator.return_value.count = AsyncMock(return_value=0)
    auxiliary = Mock()
    auxiliary.is_closed.return_value = False
    auxiliary.url = "https://www.xiaohongshu.com/user/profile/author"
    auxiliary.locator.return_value.count = AsyncMock(return_value=1)
    crawler.browser_context = Mock(pages=[primary, auxiliary])
    crawler.xhs_client = Mock(playwright_page=primary)

    assert await crawler._profile_ui_visible() is False
    assert crawler.context_page is primary
    assert crawler.xhs_client.playwright_page is primary
    auxiliary.locator.assert_not_called()


@pytest.mark.asyncio
async def test_profile_ui_visible_accepts_nonsemantic_current_sidebar(crawler):
    missing_layout = Mock()
    missing_layout.count = AsyncMock(return_value=0)
    current_layout = Mock()
    current_layout.count = AsyncMock(return_value=1)
    crawler.context_page = Mock()
    crawler.context_page.is_closed.return_value = False
    crawler.context_page.locator.side_effect = [
        missing_layout,
        missing_layout,
        current_layout,
    ]

    assert await crawler._profile_ui_visible() is True
    assert crawler.context_page.locator.call_count == 3


# 以下 enrich_note_creator 用例覆盖 repair 过渡路径（无 token API → 直开作者页），提交 6 随之删除。


@pytest.mark.asyncio
async def test_creator_enrichment_uses_signed_in_profile_without_note_token(crawler):
    creator = {"interactions": [{"type": "fans", "count": "123"}]}
    crawler.xhs_client = _CreatorClient(creator)
    crawler._repair_creator_info_by_url = AsyncMock()
    note = {
        "user": {"user_id": "author-1"},
        "xsec_token": "note-token-must-not-be-used-for-author-profile",
        "xsec_source": "pc_search",
    }

    await crawler.enrich_note_creator(note)

    assert crawler.xhs_client.calls == [{"user_id": "author-1"}]
    assert note["creator_profile"] == creator
    crawler._repair_creator_info_by_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_enrichment_falls_back_to_signed_in_browser(crawler):
    creator = {"interactions": [{"type": "fans", "count": "456"}]}
    crawler.xhs_client = _CreatorClient(None)
    crawler._repair_creator_info_by_url = AsyncMock(return_value=creator)
    note = {"user": {"user_id": "author-2"}, "xsec_token": "note-token"}

    await crawler.enrich_note_creator(note)

    crawler._repair_creator_info_by_url.assert_awaited_once_with("author-2")
    assert note["creator_profile"] == creator


@pytest.mark.asyncio
async def test_original_page_login_prevents_clicking_the_author_and_redoes_the_note(crawler):
    primary = crawler.context_page
    crawler._popup_checkpoint_state.return_value = {"manual_markers": ["扫码登录"]}
    crawler._open_author_tab = AsyncMock()
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=True)

    with pytest.raises(XhsNoteRedo):
        await crawler._get_creator_info_from_browser("author-login", note=NOTE)
    assert crawler.context_page is primary
    crawler._open_author_tab.assert_not_awaited()
    crawler._wait_for_midrun_login_recovery.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_failed_login_recovery_on_the_original_page_is_login_required(crawler):
    crawler._popup_checkpoint_state.return_value = {"manual_markers": ["扫码登录"]}
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=False)

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await crawler._get_creator_info_from_browser("author-login", note=NOTE)
    assert exc_info.value.code == "login_required"


@pytest.mark.asyncio
async def test_creator_enrichment_reports_empty_profile_after_all_fallbacks(crawler):
    crawler.xhs_client = _CreatorClient(None)
    crawler._repair_creator_info_by_url = AsyncMock(return_value=None)
    note = {"user": {"user_id": "author-empty"}}

    with pytest.raises(XHSCreatorProfileUnavailable) as exc_info:
        await crawler.enrich_note_creator(note)

    assert exc_info.value.attempts == 2
    crawler._repair_creator_info_by_url.assert_awaited_once_with("author-empty")


@pytest.mark.asyncio
async def test_creator_enrichment_does_not_hide_ip_block(crawler):
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_creator_info.side_effect = IPBlockError(
        "Network connection error, code 300012"
    )
    crawler._repair_creator_info_by_url = AsyncMock()
    note = {"user": {"user_id": "author-blocked"}}

    with pytest.raises(IPBlockError):
        await crawler.enrich_note_creator(note)

    crawler._repair_creator_info_by_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_enrichment_does_not_hide_visible_browser_block(crawler):
    crawler.xhs_client = _CreatorClient(None)
    crawler._repair_creator_info_by_url = AsyncMock(
        side_effect=RuntimeError("xhs_creator_profile_visible_block:captcha_or_verify")
    )
    note = {"user": {"user_id": "author-3"}}

    with pytest.raises(RuntimeError, match="xhs_creator_profile_visible_block"):
        await crawler.enrich_note_creator(note)


@pytest.mark.asyncio
async def test_creator_browser_fallback_stops_on_platform_security_limit(crawler, monkeypatch):
    class SecurityLimitPage:
        async def wait_for_timeout(self, milliseconds):
            assert milliseconds >= 0

        def on(self, *args):
            return None

    page = SecurityLimitPage()
    crawler._open_author_tab = AsyncMock(return_value=page)
    crawler._close_author_tab = AsyncMock()
    crawler._wait_for_creator_profile_verification = AsyncMock()
    record_limit = AsyncMock(return_value={"observed_error_code": "300011"})

    async def inspect_state(current_page):
        assert current_page is page
        return "安全限制 Account exception, please retry later 300011", {
            "platform_security_limit": True,
            "captcha_or_verify": True,
            "rate_limited": False,
            "blocked": False,
            "login_required": False,
        }

    monkeypatch.setattr(
        xhs_author,
        "inspect_visible_page_state",
        inspect_state,
    )
    monkeypatch.setattr(
        crawler.ports,
        "record_platform_security_limit",
        record_limit,
    )

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await crawler._get_creator_info_from_browser("author-security-limit", note=NOTE)

    assert exc_info.value.code == "platform_security_limit_300011"

    crawler._close_author_tab.assert_awaited_once_with(page, NOTE)
    crawler._wait_for_creator_profile_verification.assert_not_awaited()
    record_limit.assert_awaited_once_with(
        page,
        stage="creator_profile:author-security-limit:arrival",
        visible_text_sample="安全限制 Account exception, please retry later 300011",
        visible_markers={
            "platform_security_limit": True,
            "captcha_or_verify": True,
            "rate_limited": False,
            "blocked": False,
            "login_required": False,
        },
    )


@pytest.mark.asyncio
async def test_creator_tab_login_recovers_on_original_page_then_redoes_note(crawler, monkeypatch):
    class LoginPage:
        async def wait_for_timeout(self, milliseconds):
            assert milliseconds >= 0

        def on(self, *args):
            return None

    page = LoginPage()
    crawler._open_author_tab = AsyncMock(return_value=page)
    crawler._close_author_tab = AsyncMock()
    crawler._wait_for_creator_profile_verification = AsyncMock()
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=True)

    async def inspect_state(current_page):
        assert current_page is page
        return "", {"login_required": True, "captcha_or_verify": False}

    monkeypatch.setattr(
        xhs_author,
        "inspect_visible_page_state",
        inspect_state,
    )

    with pytest.raises(XhsNoteRedo):
        await crawler._get_creator_info_from_browser("author-login", note=NOTE)

    crawler._wait_for_creator_profile_verification.assert_not_awaited()
    crawler._wait_for_midrun_login_recovery.assert_awaited_once_with()
    crawler._close_author_tab.assert_awaited_once_with(page, NOTE)


@pytest.mark.asyncio
async def test_creator_tab_stays_open_until_verified_then_reads_projection(
    crawler,
    monkeypatch,
):
    class Mouse:
        move_calls = 0
        wheel_calls = 0

        async def move(self, *args, **kwargs):
            self.move_calls += 1

        async def wheel(self, *args, **kwargs):
            self.wheel_calls += 1

    class VerificationPage:
        viewport_size = {"width": 1280, "height": 800}
        mouse = Mouse()
        url = "https://www.xiaohongshu.com/user/profile/author-qr"

        def __init__(self):
            self.brought_to_front = False
            self.closed = False
            self.projection_calls = 0
            self.inspection_calls = 0

        def on(self, *args):
            return None

        def is_closed(self):
            return self.closed

        async def wait_for_timeout(self, milliseconds):
            assert not self.closed

        async def bring_to_front(self):
            self.brought_to_front = True

        async def evaluate(self, script, arg=None):
            if arg is None:
                return {}
            # 验证完成后经通道读取作者页投影；读取发生在复查可见状态之后。
            self.projection_calls += 1
            assert self.inspection_calls >= 3
            return projection("author-qr", "654")

        async def close(self):
            self.closed = True

    page = VerificationPage()
    marker_sequence = iter(
        [
            {"captcha_or_verify": True},
            {"captcha_or_verify": True},
            {"captcha_or_verify": False},
            # 接受验证后读到的结果前复查一次可见状态。
            {"captcha_or_verify": False},
        ]
    )

    async def inspect_state(current_page):
        assert current_page is page
        assert not page.closed
        page.inspection_calls += 1
        return "", next(marker_sequence)

    crawler._open_author_tab = AsyncMock(return_value=page)
    monkeypatch.setattr(
        xhs_author,
        "inspect_visible_page_state",
        inspect_state,
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_POLL_SECONDS", "0")

    result = await crawler._get_creator_info_from_browser("author-qr", note=NOTE)

    assert result["interactions"][0]["count"] == "654"
    assert crawler._creator_visit_id is not None
    assert page.brought_to_front is True
    assert page.closed is True
    assert page.projection_calls == 1
    assert page.mouse.move_calls == 0
    assert page.mouse.wheel_calls == 0


@pytest.mark.asyncio
async def test_creator_verification_popups_share_one_remaining_budget(
    crawler,
    monkeypatch,
) -> None:
    clock = {"now": 0.0}

    def monotonic() -> float:
        return clock["now"]

    async def sleep(seconds: float) -> None:
        clock["now"] += seconds

    class VerificationPage:
        def __init__(self, name: str) -> None:
            self.name = name
            self.url = f"https://www.xiaohongshu.com/user/profile/{name}"
            self.inspections = 0
            self.bring_to_front = AsyncMock()

        def on(self, *args):
            return None

        def is_closed(self):
            return False

        async def evaluate(self, script, arg=None):
            if arg is None:
                return {}
            return projection(self.name, "1") if self.name == "first" else {"status": "missing"}

    first = VerificationPage("first")
    second = VerificationPage("second")
    for page in (first, second):
        crawler.capture.attach(page, "author_tab")

    async def inspect_state(page: VerificationPage):
        page.inspections += 1
        verified = page is first and page.inspections >= 2
        return "", {"captcha_or_verify": not verified}

    crawler._popup_monotonic = monotonic
    crawler._popup_sleep = sleep
    crawler._manual_wait_budget = XHSManualWaitBudget(
        limit_seconds=5.0,
        monotonic=monotonic,
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_POLL_SECONDS", "1")
    monkeypatch.setattr(
        xhs_author,
        "inspect_visible_page_state",
        inspect_state,
    )

    result = await crawler._wait_for_creator_profile_verification(
        first,
        "first",
    )
    assert result["basicInfo"] == {"nickname": "first"}
    assert crawler._manual_wait_budget.manual_elapsed_seconds == 1.0

    clock["now"] += 1000.0
    with pytest.raises(XHSManualWaitBudgetExhausted):
        await crawler._wait_for_creator_profile_verification(
            second,
            "second",
        )

    assert crawler._manual_wait_budget.manual_elapsed_seconds == 5.0
    assert first.bring_to_front.await_count == 1
    assert second.bring_to_front.await_count == 1


@pytest.mark.asyncio
async def test_creator_enrichment_caches_successful_author_profile(crawler):
    creator = {"interactions": [{"type": "fans", "count": "789"}]}
    crawler.xhs_client = _CreatorClient(creator)
    first = {"user": {"user_id": "author-4"}}
    second = {"user": {"user_id": "author-4"}}

    await crawler.enrich_note_creator(first)
    await crawler.enrich_note_creator(second)

    assert crawler.xhs_client.calls == [{"user_id": "author-4"}]
    assert first["creator_profile"] == creator
    assert second["creator_profile"] == creator


@pytest.mark.asyncio
async def test_browser_identity_headers_follow_current_chromium_version(crawler):
    class IdentityPage:
        async def evaluate(self, script):
            return {
                "webdriver": None,
                "user_agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/149.0.0.0 Safari/537.36"
                ),
                "language": "zh-CN",
                "platform": "macOS",
                "mobile": False,
                "brands": [
                    {"brand": "Chromium", "version": "149"},
                    {"brand": "Not)A;Brand", "version": "24"},
                ],
            }

    crawler.context_page = IdentityPage()

    headers = await crawler._browser_identity_headers()

    assert "Chrome/149.0.0.0" in headers["user-agent"]
    assert '"Chromium";v="149"' in headers["sec-ch-ua"]
    assert "126" not in str(headers)
    assert headers["accept-language"] == "zh-CN"


@pytest.mark.asyncio
async def test_browser_identity_headers_reject_version_mismatch(crawler):
    class IdentityPage:
        async def evaluate(self, script):
            return {
                "webdriver": None,
                "user_agent": "Mozilla/5.0 Chrome/149.0.0.0 Safari/537.36",
                "language": "zh-CN",
                "platform": "macOS",
                "mobile": False,
                "brands": [{"brand": "Chromium", "version": "126"}],
            }

    crawler.context_page = IdentityPage()

    with pytest.raises(RuntimeError, match="version_mismatch"):
        await crawler._browser_identity_headers()


def test_creator_html_extractor_stops_at_end_of_initial_state_object():
    html = (
        '<script>window.__INITIAL_STATE__={"user":{"userPageData":'
        '{"userId":"author-8","interactions":[{"type":"fans","count":"88"}]}}}'
        '</script><script>window.__NEXT_STATE__={"extra":true}</script>'
    )

    creator = XiaoHongShuExtractor().extract_creator_info_from_html(html)

    assert creator == {
        "userId": "author-8",
        "interactions": [{"type": "fans", "count": "88"}],
    }


def test_creator_html_extractor_preserves_undefined_fallback():
    html = (
        '<script nonce="test"> window.__INITIAL_STATE__ = '
        '{"user":{"userPageData":{"userId":"author-9","ipLocation":undefined}}}'
        ";</script>"
    )

    creator = XiaoHongShuExtractor().extract_creator_info_from_html(html)

    assert creator == {"userId": "author-9", "ipLocation": None}
