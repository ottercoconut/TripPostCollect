"""登录态改为截获 user/me：规则定义、规则端点分类、user/me 判定、登录确认与二维码 DOM 就绪。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import trippostcollect.platforms.xhs.session as xhs_session
from support.page_events import FakeContext, FakePage
from trippostcollect.application.visit_gate import VISIT_POLICIES
from trippostcollect.platforms.xhs import capture, page_scripts
from trippostcollect.platforms.xhs.errors import IPBlockError, PlatformRuntimeError, XHSLoginExpired
from trippostcollect.runtime.page_capture import CapturedResponse
from trippostcollect.runtime.visit_ledger import load_ledger

from .support import XiaoHongShuCrawler, make_capture


USER_ME = "https://edith.xiaohongshu.com/api/sns/web/v2/user/me"


def captured(status: int = 200, payload=None, *, rule=capture.XHS_USER_ME_RULE) -> CapturedResponse:
    return CapturedResponse(
        rule=rule, seq=1, page_role="primary", route="/explore", query_keys=(), endpoint="x",
        status=status, verify_headers_present=False, request_json=None, json=payload,
        observed_at="t",
    )


def test_rules_match_the_frontend_endpoints_only():
    assert capture.XHS_SEARCH_RULE.matches(
        "POST", "https://so.xiaohongshu.com/api/sns/web/v2/search/notes", "fetch")
    assert not capture.XHS_SEARCH_RULE.matches(
        "POST", "https://edith.xiaohongshu.com/api/sns/web/v1/search/notes", "xhr")
    assert capture.XHS_FEED_RULE.matches("POST", "https://edith.xiaohongshu.com/api/sns/web/v1/feed", "xhr")
    assert capture.XHS_USER_ME_RULE.matches("GET", USER_ME, "xhr")
    assert not capture.XHS_USER_ME_RULE.matches(
        "GET", "https://edith.xiaohongshu.com/api/sns/web/v1/user/selfinfo", "xhr")
    assert capture.XHS_IMAGE_RULE.matches("GET", "https://sns-webpic-qc.xhscdn.com/a/b!nd_dft", "image")
    assert not capture.XHS_IMAGE_RULE.matches("GET", "https://sns-avatar-qc.xhscdn.com/avatar/x", "image")
    assert not capture.XHS_IMAGE_RULE.matches("GET", "https://sns-webpic-qc.xhscdn.com/a", "xhr")
    assert capture.XHS_SENTINEL.matches("https://t2.xiaohongshu.com/api/v2/collect", "xhr", 461)
    assert not capture.XHS_SENTINEL.matches("https://t2.xiaohongshu.com/api/v2/collect", "xhr", 500)


def test_rule_names_match_the_visit_gate_policy():
    policy = VISIT_POLICIES["xhs"]
    discovery = policy.stages["discovery"]
    assert discovery["search_results"].rule == capture.XHS_SEARCH_RULE.name
    assert discovery["note_detail"].rule == capture.XHS_FEED_RULE.name
    assert discovery["note_image"].rule == capture.XHS_IMAGE_RULE.name
    for stage in ("search_results", "note_detail", "note_image"):
        assert discovery[stage].source == "page_response"


@pytest.mark.parametrize(
    "status,payload,kind",
    [
        (461, None, "verification"),
        (471, {"success": True}, "verification"),
        (429, None, "rate_limited"),
        (401, None, "auth"),
        (403, {"success": True}, "auth"),
        (200, {"code": 300012, "success": False}, "ip_blocked_300012"),
        (200, {"code": "300011", "success": False}, "security_limit_300011"),
        (200, {"code": 0, "success": True, "data": {}}, "ok"),
        (200, {"code": -510000, "success": False}, "note_not_found"),
        (200, {"code": -510001, "success": False}, "note_not_found"),
        (200, {"code": -101, "success": False}, "login_expired"),
        (200, {"code": 1, "success": False, "msg": "登录已过期"}, "login_expired"),
        (200, {"code": 1, "success": False, "msg": "Session expired"}, "login_expired"),
        (200, {"code": 1001, "success": False, "msg": "其他"}, "business_error"),
        (200, None, "business_error"),
    ],
)
def test_rule_endpoint_classification_keeps_the_client_code_table(status, payload, kind):
    assert capture.classify_xhs_api_response(captured(status, payload)).kind == kind


def test_verdict_details_and_runtime_raises():
    assert capture.classify_xhs_api_response(captured(200, {"code": 1001})).detail == "business_error:1001"
    assert capture.classify_xhs_api_response(captured(200, None)).detail == "business_error:unreadable"
    assert capture.classify_xhs_api_response(captured(502, None)).detail == "business_error:http_502"
    for status, payload, code in (
        (429, None, "rate_limited"),
        (401, None, "login_required"),
        (200, {"code": 300011}, "platform_security_limit_300011"),
    ):
        with pytest.raises(PlatformRuntimeError) as error:
            capture.raise_for_runtime_verdict(capture.classify_xhs_api_response(captured(status, payload)), stage="x")
        assert error.value.code == code
    with pytest.raises(IPBlockError):
        capture.raise_for_runtime_verdict(capture.classify_xhs_api_response(captured(200, {"code": 300012})), stage="x")
    with pytest.raises(XHSLoginExpired) as expired:
        capture.raise_for_runtime_verdict(capture.classify_xhs_api_response(captured(200, {"code": -101})), stage="feed")
    assert expired.value.code == "login_required"
    for kind_payload in ((461, None), (200, {"success": True}), (200, {"code": -510000})):
        capture.raise_for_runtime_verdict(capture.classify_xhs_api_response(captured(*kind_payload)), stage="x")


@pytest.mark.parametrize(
    "payload,state",
    [
        ({"code": -101, "success": False}, "guest"),
        ({"success": True, "data": {"guest": True}}, "guest"),
        ({"success": True, "data": {"guest": False, "user_id": "u1"}}, "logged_in"),
        ({"success": True, "data": {"guest": False, "user_id": ""}}, "unknown"),
        ({"success": True, "data": {}}, "unknown"),
        (None, "unknown"),
    ],
)
def test_user_me_states(payload, state):
    assert capture.user_me_state(captured(200, payload)) == state
    assert capture.user_me_logged_in(captured(200, payload)) is (state == "logged_in")


def test_failures_are_never_logged_in():
    assert capture.user_me_logged_in(SimpleNamespace(json={"success": True})) is False


def make_session(monkeypatch, tmp_path):
    monkeypatch.setattr(xhs_session, "XHS_LOGIN_ME_CONFIRM_SECONDS", 0.05)
    context = FakeContext()
    page = FakePage("https://www.xiaohongshu.com/explore")
    page.reloads = 0
    page.on_reload = None

    async def reload(*, wait_until: str, timeout: int) -> None:
        page.reloads += 1
        if page.on_reload is not None:
            page.on_reload()

    page.reload = reload
    context.pages.append(page)
    crawler = XiaoHongShuCrawler()
    crawler.browser_context = context
    crawler.capture = make_capture(tmp_path, context=context)
    crawler.capture.install(page)
    crawler.context_page = page
    return crawler, page


def emit_user_me(page, payload):
    page.respond(page.request("GET", USER_ME), body=json.dumps(payload).encode())


def visits(crawler):
    return [line for line in load_ledger(crawler.capture.ledger.path).lines if line["kind"] == "visit"]


@pytest.mark.asyncio
async def test_confirm_login_state_ignores_user_me_before_login_and_guest_responses(monkeypatch, tmp_path):
    crawler, page = make_session(monkeypatch, tmp_path)
    emit_user_me(page, {"success": True, "data": {"guest": False, "user_id": "stale"}})
    await asyncio.sleep(0)
    login_started = crawler.capture.ledger.last_seq
    emit_user_me(page, {"code": -101, "success": False})
    emit_user_me(page, {"success": True, "data": {"guest": True}})
    page.on_reload = lambda: emit_user_me(page, {"success": True, "data": {"guest": False, "user_id": "u"}})

    visit_id = await crawler._confirm_login_state(after_seq=login_started)

    assert page.reloads == 1
    (visit,) = visits(crawler)
    assert visit["id"] == visit_id and visit["stage"] == "login_state" and visit["guest"] is False
    assert visit["seq"] > login_started and visit["route"] == "/explore"
    actions = [line for line in load_ledger(crawler.capture.ledger.path).lines if line["kind"] == "action"]
    assert [(line["action"], line["target"]) for line in actions] == [("reload", {"route": "/explore"})]


@pytest.mark.asyncio
async def test_confirm_login_state_without_user_me_fails_after_one_reload(monkeypatch, tmp_path):
    crawler, page = make_session(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match=r"^xhs_login_state_unconfirmed$"):
        await crawler._confirm_login_state(after_seq=0)

    assert page.reloads == 1
    assert visits(crawler) == []


@pytest.mark.asyncio
async def test_confirm_login_state_without_reload_when_frontend_already_requested(monkeypatch, tmp_path):
    crawler, page = make_session(monkeypatch, tmp_path)
    emit_user_me(page, {"success": True, "data": {"guest": False, "user_id": "u"}})

    await crawler._confirm_login_state(after_seq=0)

    assert page.reloads == 0 and len(visits(crawler)) == 1


@pytest.mark.asyncio
async def test_qrcode_readiness_is_a_dom_boolean():
    class Page:
        def __init__(self, value):
            self.value = value
            self.scripts = []

        async def evaluate(self, script):
            self.scripts.append(script)
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    ready = Page(True)
    assert await page_scripts.dom_qr_ready(ready) is True
    assert ready.scripts == [page_scripts.DOM_SCRIPTS["qr_ready"]]
    assert "img.qrcode-img" in ready.scripts[0] and "getAttribute('src')" in ready.scripts[0]
    assert await page_scripts.dom_qr_ready(Page(None)) is False
    assert await page_scripts.dom_qr_ready(Page(RuntimeError("detached"))) is False


def test_session_has_no_self_info_probe():
    assert not hasattr(XiaoHongShuCrawler, "_pong_with_network_recovery")
    assert "selfinfo" not in Path(xhs_session.__file__).read_text(encoding="utf-8")
