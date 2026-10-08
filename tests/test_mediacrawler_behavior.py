"""TripPostCollect tests for mediacrawler behavior."""

from __future__ import annotations

from trippostcollect.application import collection as t11_collection

import asyncio
import json
import sys
from contextlib import contextmanager
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import legacy_expectations as expectations


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_behavior = import_module("mediacrawler_behavior")
mediacrawler_crawl = import_module("mediacrawler_crawl")
xhs_behavior = import_module("trippostcollect.platforms.xhs.behavior")
runtime_behavior = import_module("trippostcollect.runtime.behavior")
human_flow = import_module("trippostcollect.runtime.human_flow")


def _fork_behavior_adapter():
    spec = spec_from_file_location(
        "t04_behavior_adapter", ROOT / "tools/MediaCrawler/tools/trippostcollect_behavior.py",
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@expectations.legacy_only
def test_fork_behavior_injects_original_xhs_dependencies_at_call_time(monkeypatch, tmp_path):
    adapter = _fork_behavior_adapter()
    page = object()
    calls = []

    async def run(borrowed, **kwargs):
        assert borrowed is page
        calls.append(kwargs)
        return {"status": "completed"}

    monkeypatch.setattr(runtime_behavior, "run_page_behavior", run)
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_ENABLED", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", str(tmp_path / "missing"))
    assert asyncio.run(adapter.run_required_human_behavior(page, "xhs")) == {
        "status": "disabled", "platform": "xhs",
    }
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_ENABLED", "1")
    with pytest.raises(RuntimeError, match="human behavior configuration is incomplete"):
        asyncio.run(adapter.run_required_human_behavior(page, "xhs"))
    assert calls == []
    monkeypatch.setenv("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", str(SCRIPTS))
    evidence = str(tmp_path / "behavior.json")
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", evidence)
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "xhs_guarded")
    assert asyncio.run(adapter.run_required_human_behavior(page, "xhs")) == {"status": "completed"}
    assert calls == [{
        "platform_key": "xhs", "evidence_path": evidence, "profile_name": "xhs_guarded",
        "xhs_search_ready": mediacrawler_behavior.wait_for_xhs_search_ready,
        "write_evidence": mediacrawler_behavior.write_evidence,
    }]


@expectations.legacy_only
def test_fork_runtime_hints_keep_guard_without_injecting_scripts_path(monkeypatch, tmp_path):
    adapter = _fork_behavior_adapter()
    context = object()
    calls = []

    async def install(borrowed):
        calls.append(borrowed)

    monkeypatch.setattr(runtime_behavior, "install_runtime_hints", install)
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_ENABLED", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", str(tmp_path / "missing"))
    asyncio.run(adapter.install_project_runtime_hints(context))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_ENABLED", "1")
    with pytest.raises(RuntimeError, match="runtime hint configuration is incomplete"):
        asyncio.run(adapter.install_project_runtime_hints(context))
    assert calls == []
    monkeypatch.setenv("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", str(tmp_path))
    before = list(sys.path)
    asyncio.run(adapter.install_project_runtime_hints(context))
    assert sys.path == before
    assert calls == [context]


def valid_fingerprint(*, webdriver=None) -> dict:
    return {
        "webdriver": webdriver,
        "languages": ["zh-CN", "zh"],
        "platform": "MacIntel",
        "user_agent": "test-agent",
        "hardware_concurrency": 8,
        "device_memory": 8,
        "max_touch_points": 0,
        "viewport": {"width": 1440, "height": 900, "device_pixel_ratio": 2},
        "visibility_state": "visible",
    }


def valid_xhs_events() -> list[dict]:
    return [
        {"event": "pause"},
        {"event": "mouse_moves"},
        {"event": "wheel_scroll", "effect_observed": True},
        {
            "event": "human_scroll_complete",
            "intent": "list",
            "passes": 1,
            "effective_passes": 1,
        },
    ]


def valid_xhs_evidence() -> dict:
    return {
        "status": "completed",
        "profile": "xhs_guarded",
        "events": valid_xhs_events(),
        "runtime_fingerprint": valid_fingerprint(),
        "page_readiness": {"ready": True},
        "initial_visible_markers": {},
        "visible_markers": {},
    }


def test_latest_platform_result_counts_ignore_prior_failed_resume_record() -> None:
    counts = mediacrawler_crawl.latest_platform_result_counts(
        [
            {"platform": "bilibili", "status": "failed", "ok": False},
            {"platform": "bilibili", "status": "completed", "ok": True},
            {"platform": "weibo", "status": "skipped_video_only", "ok": True},
        ],
        ["bilibili", "weibo"],
    )

    assert counts == {
        "ok_count": 2,
        "skipped_video_only_count": 1,
        "failed_count": 0,
    }


def test_failed_process_cannot_be_classified_as_success_when_output_exists(
) -> None:
    exit_code = mediacrawler_crawl.effective_attempt_exit_code(
        {
            "status": "completed",
            "ok": True,
            "run": {"returncode": 1},
        }
    )

    assert exit_code == 1


def test_xhs_sms_terminal_failure_runs_one_platform_session_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    @contextmanager
    def fake_guard(*args, **kwargs):
        yield {}

    def fake_run(
        platform_key,
        args,
        batch_dir,
        *,
        runtime_reporter=None,
        ports,
    ):
        nonlocal calls
        assert runtime_reporter is None
        calls += 1
        return {
            "platform": platform_key,
            "label": "小红书",
            "status": "failed",
            "ok": False,
            "behavior_evidence": {
                "visible_markers": {"sms_verification_terminal": True}
            },
            "run": {
                "returncode": 1,
                "stdout_tail": "",
                "stderr_tail": "Navigation timeout",
            },
            "output": {},
        }

    monkeypatch.setattr(t11_collection, "site_request_guard", fake_guard)
    monkeypatch.setattr(
        t11_collection,
        "clear_site_policy_state",
        lambda site_key: {},
    )
    monkeypatch.setattr(t11_collection, "_run_platform_without_policy", fake_run)

    record = mediacrawler_crawl.run_platform(
        "xhs",
        SimpleNamespace(keyword="青岛旅行"),
        tmp_path,
    )

    assert calls == 1
    assert record["failure_classification"] == {
        "status": "blocked",
        "failure_type": "sms_verification_terminal",
        "retryable": False,
        "wait_seconds": 0,
        "reason": "xhs_sms_verification_terminal",
    }
    assert "cooldown_event" not in record


class FakeLocator:
    def __init__(self, text: str) -> None:
        self.text = text

    async def inner_text(self, timeout: int) -> str:
        return self.text


class FakeFrame:
    def __init__(self, text: str) -> None:
        self.text = text

    def locator(self, selector: str) -> FakeLocator:
        assert selector == "body"
        return FakeLocator(self.text)


class FakePage:
    def __init__(
        self,
        text: str = "正常搜索内容",
        *,
        card_count: int = 1,
        profile_count: int = 1,
        url: str = "https://example.test/search",
        login_control_visible: bool = False,
    ) -> None:
        self.url = url
        self.text = text
        self.card_count = card_count
        self.profile_count = profile_count
        self.login_control_visible = login_control_visible
        self.brought_to_front = 0
        self.goto_calls: list[str] = []
        self.main_frame = FakeFrame(text)
        self.frames = [self.main_frame]

    def locator(self, selector: str) -> FakeLocator:
        assert selector == "body"
        return FakeLocator(self.text)

    async def evaluate(self, script: str) -> dict:
        if 'querySelectorAll("a, button' in script:
            return self.login_control_visible
        if "card_count:" in script:
            return {
                "card_count": self.card_count,
                "profile_count": self.profile_count,
            }
        return valid_fingerprint()

    async def screenshot(self, path: str, full_page: bool, timeout: int, animations: str) -> None:
        Path(path).write_bytes(b"png")

    async def bring_to_front(self) -> None:
        self.brought_to_front += 1

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append(url)
        self.url = url


@pytest.mark.asyncio
async def test_xhs_bare_visible_login_control_blocks_search_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xhs_behavior, "XHS_CONTINUITY_VERIFY_POLL_SECONDS", 0.001)
    monkeypatch.setattr(xhs_behavior, "XHS_CONTINUITY_VERIFY_WAIT_SECONDS", 0.01)
    page = FakePage(
        "创作中心 发现 通知 消息 登录 热门内容",
        card_count=90,
        profile_count=30,
        url="https://www.xiaohongshu.com/search_result?keyword=test",
        login_control_visible=True,
    )

    visible_text, markers = await mediacrawler_behavior.visible_page_state(page)
    readiness = await mediacrawler_behavior.wait_for_xhs_search_ready(page, [])

    assert "登录" in visible_text
    assert markers["login_required"] is True
    assert readiness["ready"] is False
    assert readiness["reason"] == "login_required"


async def fake_dwell_on_list(page, profile, log) -> None:
    log.extend(
        [
            {"event": "pause", "reason": "list_dwell_initial", "seconds": 1.0},
            {"event": "mouse_moves", "count": 3},
            {"event": "wheel_scroll", "delta_y": 500, "effect_observed": True},
            {
                "event": "human_scroll_complete",
                "intent": "list",
                "passes": 1,
                "effective_passes": 1,
            },
        ]
    )


@pytest.mark.asyncio
async def test_behavior_stage_writes_complete_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_behavior, "dwell_on_list", fake_dwell_on_list)
    evidence_path = tmp_path / "behavior.json"

    evidence = await mediacrawler_behavior.run_page_behavior(
        FakePage(),
        platform_key="weibo",
        evidence_path=evidence_path,
    )

    assert mediacrawler_behavior.behavior_evidence_valid(evidence) is True
    assert evidence["runtime_fingerprint"]["webdriver"] is None
    assert json.loads(evidence_path.read_text())["status"] == "completed"
    assert evidence_path.with_suffix(".png").is_file()


@pytest.mark.asyncio
async def test_xhs_search_verification_wait_keeps_page_open_until_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = iter(
        [
            (
                "Scan with logged-in REDnote App",
                {
                    "platform_security_limit": False,
                    "captcha_or_verify": True,
                    "rate_limited": False,
                    "blocked": False,
                    "login_required": False,
                },
            ),
            (
                "正常搜索内容",
                {
                    "platform_security_limit": False,
                    "captcha_or_verify": False,
                    "rate_limited": False,
                    "blocked": False,
                    "login_required": False,
                },
            ),
        ]
    )

    async def changing_page_state(page):
        return next(states)

    monkeypatch.setattr(xhs_behavior, "visible_page_state", changing_page_state)
    monkeypatch.setattr(xhs_behavior, "XHS_CONTINUITY_VERIFY_POLL_SECONDS", 0.001)
    page = FakePage(card_count=1, profile_count=1)
    events: list[dict] = []

    readiness = await mediacrawler_behavior.wait_for_xhs_search_ready(page, events)

    assert readiness["ready"] is True
    assert page.brought_to_front == 1
    assert readiness["operator_verification_events"][0]["status"] == "completed"
    assert readiness["operator_verification_events"][0]["initial_challenge"] == "captcha_or_verify"


@pytest.mark.asyncio
async def test_xhs_search_login_wait_stops_on_sms_terminal_without_reopening_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = iter(
        [
            (
                "手机号登录 获取验证码",
                {
                    "sms_verification_terminal": False,
                    "captcha_or_verify": False,
                    "rate_limited": False,
                    "blocked": False,
                    "login_required": True,
                },
            ),
            (
                "SMS Verification Parameter error",
                {
                    "sms_verification_terminal": True,
                    "captcha_or_verify": False,
                    "rate_limited": False,
                    "blocked": False,
                    "login_required": False,
                },
            ),
        ]
    )

    async def changing_page_state(page):
        return next(states)

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(xhs_behavior, "visible_page_state", changing_page_state)
    monkeypatch.setattr(xhs_behavior.asyncio, "sleep", no_sleep)
    page = FakePage(url="https://www.xiaohongshu.com/login")

    readiness = await mediacrawler_behavior.wait_for_xhs_search_ready(page, [])

    verification = readiness["operator_verification_events"][0]
    assert readiness["ready"] is False
    assert readiness["reason"] == "sms_verification_terminal"
    assert verification["status"] == "failed"
    assert verification["error"] == (
        "sms_verification_terminal_during_operator_verification"
    )
    assert page.brought_to_front == 1
    assert page.goto_calls == []


@pytest.mark.asyncio
async def test_visible_challenge_fails_behavior_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_behavior, "dwell_on_list", fake_dwell_on_list)
    monkeypatch.setattr(xhs_behavior, "XHS_CONTINUITY_VERIFY_POLL_SECONDS", 0.001)
    monkeypatch.setattr(xhs_behavior, "XHS_CONTINUITY_VERIFY_WAIT_SECONDS", 0.1)
    evidence_path = tmp_path / "behavior.json"

    with pytest.raises(RuntimeError, match="captcha_or_verify_detected"):
        await mediacrawler_behavior.run_page_behavior(
            FakePage("请完成安全验证"),
            platform_key="xhs",
            evidence_path=evidence_path,
            profile_name="xhs_guarded",
        )

    evidence = json.loads(evidence_path.read_text())
    assert evidence["status"] == "failed"
    assert any(event["event"] == "page_readiness_check" for event in evidence["events"])
    assert evidence["initial_visible_markers"]["captcha_or_verify"] is True
    assert evidence["visible_markers"]["captcha_or_verify"] is True
    assert evidence["operator_verification_events"][0]["status"] == "failed"
    assert mediacrawler_behavior.behavior_evidence_valid(evidence) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "url"),
    [
        ("安全限制 Account exception, please retry later 300011", "https://www.xiaohongshu.com/explore"),
        ("", "https://www.xiaohongshu.com/website-login/error?redirectPath=%2Fuser%2Fprofile%2Fabc"),
    ],
)
async def test_xhs_platform_security_limit_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    url: str,
) -> None:
    monkeypatch.setattr(runtime_behavior, "dwell_on_list", fake_dwell_on_list)
    evidence_path = tmp_path / "behavior.json"
    page = FakePage(text)
    page.url = url

    with pytest.raises(RuntimeError, match="platform_security_limit_detected"):
        await mediacrawler_behavior.run_page_behavior(
            page,
            platform_key="xhs",
            evidence_path=evidence_path,
            profile_name="xhs_guarded",
        )

    evidence = json.loads(evidence_path.read_text())
    assert evidence["status"] == "failed"
    assert evidence["challenge"] == "platform_security_limit"
    assert evidence["visible_markers"]["platform_security_limit"] is True
    assert mediacrawler_behavior.behavior_evidence_valid(evidence) is False


@pytest.mark.asyncio
async def test_non_xhs_retry_text_does_not_set_xhs_security_limit() -> None:
    page = FakePage("Account exception, please retry later")
    page.url = "https://example.test/search"

    _, markers = await mediacrawler_behavior.visible_page_state(page)

    assert markers["platform_security_limit"] is False


@pytest.mark.asyncio
async def test_xhs_captcha_url_is_a_visible_verification_challenge() -> None:
    page = FakePage("Scan with logged-in REDnote App", card_count=0, profile_count=0)
    page.url = "https://www.xiaohongshu.com/website-login/captcha?verifyUuid=test"

    _, markers = await mediacrawler_behavior.visible_page_state(page)

    assert markers["captcha_or_verify"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("main_text", "frame_text"),
    [
        ("SMS Verification Parameter error Refresh", ""),
        ("短信验证：参数错误，请刷新", ""),
        ("今日短信验证码次数已达上限", ""),
        ("Daily SMS quota exhausted", ""),
        ("正常搜索内容", "SMS verification requests are too frequent"),
    ],
)
async def test_xhs_sms_terminal_challenge_is_detected_in_page_or_frame(
    main_text: str,
    frame_text: str,
) -> None:
    page = FakePage(main_text, url="https://www.xiaohongshu.com/explore")
    if frame_text:
        page.frames.append(FakeFrame(frame_text))

    text, markers = await mediacrawler_behavior.visible_page_state(page)

    assert "SMS" in text or "短信" in text
    assert markers["sms_verification_terminal"] is True
    assert (
        mediacrawler_behavior.visible_challenge(markers)
        == "sms_verification_terminal"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "SMS Verification Enter verification code",
        "Parameter error while parsing an unrelated query",
        "今天可以获取验证码",
        "频繁旅行不代表请求验证码受限",
    ],
)
async def test_similar_xhs_sms_text_is_not_a_terminal_challenge(text: str) -> None:
    page = FakePage(text, url="https://www.xiaohongshu.com/explore")

    _, markers = await mediacrawler_behavior.visible_page_state(page)

    assert markers["sms_verification_terminal"] is False


@pytest.mark.asyncio
async def test_xhs_nested_security_frame_prioritizes_rate_limit() -> None:
    page = FakePage("正常搜索内容")
    page.url = "https://www.xiaohongshu.com/search_result?keyword=青岛八大关"
    page.frames.append(
        FakeFrame("Security Verification Requests too frequent. Try again after 1 minute.")
    )

    text, markers = await mediacrawler_behavior.visible_page_state(page)

    assert "Requests too frequent" in text
    assert markers["captcha_or_verify"] is True
    assert markers["rate_limited"] is True
    assert mediacrawler_behavior.visible_challenge(markers) == "rate_limited"


@pytest.mark.asyncio
async def test_xhs_login_url_is_a_visible_login_challenge() -> None:
    page = FakePage("", card_count=0, profile_count=0)
    page.url = "https://www.xiaohongshu.com/login?redirectPath=%2Fuser%2Fprofile%2Fauthor"

    _, markers = await mediacrawler_behavior.visible_page_state(page)

    assert markers["login_required"] is True


@pytest.mark.asyncio
async def test_xhs_retry_later_without_account_exception_is_not_security_limit() -> None:
    page = FakePage("Please retry later")
    page.url = "https://www.xiaohongshu.com/explore"

    _, markers = await mediacrawler_behavior.visible_page_state(page)

    assert markers["platform_security_limit"] is False


@pytest.mark.asyncio
async def test_xhs_platform_security_limit_writer_preserves_page_evidence(tmp_path: Path) -> None:
    evidence_path = tmp_path / "behavior.json"
    evidence_path.write_text(json.dumps(valid_xhs_evidence()), encoding="utf-8")
    page = FakePage("安全限制 Account exception, please retry later 300011")
    page.url = "https://www.xiaohongshu.com/website-login/error?redirectPath=%2Fuser%2Fprofile%2Fabc"
    markers = {
        "platform_security_limit": True,
        "captcha_or_verify": False,
        "rate_limited": False,
        "blocked": False,
        "login_required": False,
    }

    event = await mediacrawler_behavior.record_xhs_platform_security_limit(
        page,
        evidence_path=evidence_path,
        stage="creator_profile:abc:arrival",
        visible_text_sample=page.text,
        visible_markers=markers,
    )

    evidence = json.loads(evidence_path.read_text())
    assert evidence["status"] == "failed"
    assert evidence["challenge"] == "platform_security_limit"
    assert evidence["platform_security_limit_events"] == [event]
    assert event["classification"] == "platform_security_limit"
    assert event["observed_error_code"] == "300011"
    assert event["url"] == page.url
    assert Path(event["screenshot"]).is_file()


@pytest.mark.asyncio
async def test_xhs_url_only_security_limit_does_not_invent_error_code(tmp_path: Path) -> None:
    evidence_path = tmp_path / "behavior.json"
    evidence_path.write_text(json.dumps(valid_xhs_evidence()), encoding="utf-8")
    page = FakePage("")
    page.url = "https://www.xiaohongshu.com/website-login/error?redirectPath=%2Fuser%2Fprofile%2Fabc"
    markers = {
        "platform_security_limit": True,
        "captcha_or_verify": False,
        "rate_limited": False,
        "blocked": False,
        "login_required": False,
    }

    event = await mediacrawler_behavior.record_xhs_platform_security_limit(
        page,
        evidence_path=evidence_path,
        stage="creator_profile:abc:arrival",
        visible_text_sample="",
        visible_markers=markers,
    )

    assert event["classification"] == "platform_security_limit"
    assert event["observed_error_code"] == ""


@pytest.mark.asyncio
async def test_xhs_dwell_stops_when_rate_limit_appears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = 0

    async def changing_page_state(page):
        nonlocal checks
        checks += 1
        if checks <= 2:
            return "正常搜索内容", {
                "captcha_or_verify": False,
                "rate_limited": False,
                "blocked": False,
                "login_required": False,
            }
        return "请求过于频繁", {
            "captcha_or_verify": False,
            "rate_limited": True,
            "blocked": False,
            "login_required": False,
        }

    async def blocking_dwell(page, profile, log) -> None:
        log.append({"event": "pause", "reason": "list_dwell_initial", "seconds": 60.0})
        await asyncio.Event().wait()

    monkeypatch.setattr(xhs_behavior, "visible_page_state", changing_page_state)
    monkeypatch.setattr(runtime_behavior, "visible_page_state", changing_page_state)
    monkeypatch.setattr(runtime_behavior, "dwell_on_list", blocking_dwell)
    monkeypatch.setattr(runtime_behavior, "XHS_VISIBLE_CHECK_INTERVAL_SECONDS", 0.001)
    evidence_path = tmp_path / "behavior.json"

    with pytest.raises(RuntimeError, match="rate_limited_detected"):
        await mediacrawler_behavior.run_page_behavior(
            FakePage(),
            platform_key="xhs",
            evidence_path=evidence_path,
            profile_name="xhs_guarded",
        )

    evidence = json.loads(evidence_path.read_text())
    assert evidence["visible_markers"]["rate_limited"] is True
    assert evidence["challenge"] == "rate_limited"
    assert any(event["event"] == "visible_state_check" for event in evidence["events"])


@pytest.mark.asyncio
async def test_sms_login_code_text_is_not_a_security_challenge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_behavior, "dwell_on_list", fake_dwell_on_list)
    evidence = await mediacrawler_behavior.run_page_behavior(
        FakePage("手机号登录 获取验证码"),
        platform_key="xhs",
        evidence_path=tmp_path / "behavior.json",
    )

    assert evidence["visible_markers"]["captcha_or_verify"] is False
    assert mediacrawler_behavior.behavior_evidence_valid(evidence) is True


@pytest.mark.asyncio
async def test_xhs_login_required_never_runs_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def not_ready(page, events):
        return {
            "ready": False,
            "reason": "login_required",
            "markers": {
                "captcha_or_verify": False,
                "rate_limited": False,
                "blocked": False,
                "login_required": True,
            },
            "visible_text_sample": "登录后查看",
            "url": page.url,
        }

    monkeypatch.setattr(mediacrawler_behavior, "wait_for_xhs_search_ready", not_ready)
    monkeypatch.setattr(runtime_behavior, "dwell_on_list", fake_dwell_on_list)
    evidence_path = tmp_path / "behavior.json"

    with pytest.raises(RuntimeError, match="login_required_detected"):
        await mediacrawler_behavior.run_page_behavior(
            FakePage("登录后查看", card_count=0, profile_count=0),
            platform_key="xhs",
            evidence_path=evidence_path,
            profile_name="xhs_guarded",
        )

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["events"] == []
    assert evidence["visible_markers"]["login_required"] is True


def test_xhs_behavior_rejects_exposed_webdriver_and_ineffective_scroll() -> None:
    exposed = valid_xhs_evidence()
    exposed["runtime_fingerprint"] = valid_fingerprint(webdriver=True)
    ineffective = valid_xhs_evidence()
    ineffective["events"][-1]["effective_passes"] = 0

    assert mediacrawler_behavior.behavior_evidence_valid(exposed) is False
    assert mediacrawler_behavior.behavior_evidence_valid(ineffective) is False


@pytest.mark.asyncio
async def test_xhs_guarded_request_pause_is_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedRandom:
        @staticmethod
        def uniform(low: float, high: float) -> float:
            assert (low, high) == (4.0, 10.0)
            return 6.5

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    evidence_path = tmp_path / "behavior.json"
    evidence_path.write_text(json.dumps(valid_xhs_evidence()), encoding="utf-8")
    monkeypatch.setattr(runtime_behavior, "REQUEST_RANDOM", FixedRandom())
    monkeypatch.setattr(xhs_behavior.asyncio, "sleep", fake_sleep)

    event = await mediacrawler_behavior.run_guarded_request_pause(
        evidence_path=evidence_path,
        profile_name="xhs_guarded",
        stage="note_detail",
        minimum=4.0,
        maximum=10.0,
    )

    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert slept == [6.5]
    assert event["seconds"] == 6.5
    assert persisted["request_pacing_events"][0]["stage"] == "note_detail"


@pytest.mark.asyncio
async def test_xhs_continuity_behavior_is_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_pause(page, seconds_range, *, reason, log):
        log.append({"event": "pause", "reason": reason, "seconds": 2.0})

    async def fake_mouse_moves(page, profile, log):
        log.append({"event": "mouse_moves", "count": 1})

    evidence_path = tmp_path / "behavior.json"
    evidence_path.write_text(json.dumps(valid_xhs_evidence()), encoding="utf-8")
    monkeypatch.setattr(xhs_behavior, "human_pause", fake_pause)
    monkeypatch.setattr(xhs_behavior, "random_mouse_moves", fake_mouse_moves)

    continuity = await mediacrawler_behavior.run_xhs_continuity_behavior(
        FakePage(),
        evidence_path=evidence_path,
        stage="search_results",
    )

    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert continuity["status"] == "completed"
    assert persisted["continuity_events"][0]["stage"] == "search_results"
    assert [item["event"] for item in continuity["events"]] == ["pause", "mouse_moves"]


@pytest.mark.asyncio
async def test_xhs_continuity_waits_for_operator_login_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal_markers = {
        "captcha_or_verify": False,
        "rate_limited": False,
        "blocked": False,
        "login_required": False,
    }
    login_markers = {**normal_markers, "login_required": True}
    states = iter(
        [
            ("登录后查看", login_markers),
            ("正常搜索内容", normal_markers),
            ("正常搜索内容", normal_markers),
        ]
    )

    async def changing_page_state(page):
        return next(states)

    async def no_sleep(seconds):
        return None

    async def fake_pause(page, seconds_range, *, reason, log):
        log.append({"event": "pause", "reason": reason, "seconds": 2.0})

    async def fake_mouse_moves(page, profile, log):
        log.append({"event": "mouse_moves", "count": 1})

    page = FakePage()
    evidence_path = tmp_path / "behavior.json"
    evidence_path.write_text(json.dumps(valid_xhs_evidence()), encoding="utf-8")
    monkeypatch.setattr(xhs_behavior, "visible_page_state", changing_page_state)
    monkeypatch.setattr(xhs_behavior.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(xhs_behavior, "human_pause", fake_pause)
    monkeypatch.setattr(xhs_behavior, "random_mouse_moves", fake_mouse_moves)

    continuity = await mediacrawler_behavior.run_xhs_continuity_behavior(
        page,
        evidence_path=evidence_path,
        stage="search_results",
    )

    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    verification = persisted["operator_verification_events"][0]
    assert page.brought_to_front == 1
    assert verification["initial_challenge"] == "login_required"
    assert verification["status"] == "completed"
    assert continuity["status"] == "completed"
    assert persisted["status"] == "completed"


@pytest.mark.asyncio
async def test_xhs_manual_login_stops_on_sms_terminal_without_page_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal_markers = {
        "platform_security_limit": False,
        "sms_verification_terminal": False,
        "captcha_or_verify": False,
        "rate_limited": False,
        "blocked": False,
        "login_required": False,
    }
    states = iter(
        [
            (
                "手机号登录 获取验证码",
                {**normal_markers, "login_required": True},
            ),
            (
                "SMS Verification Parameter error",
                {**normal_markers, "sms_verification_terminal": True},
            ),
        ]
    )

    async def changing_page_state(page):
        return next(states)

    async def no_sleep(seconds):
        return None

    page = FakePage(url="https://www.xiaohongshu.com/login")
    evidence_path = tmp_path / "behavior.json"
    evidence_path.write_text(json.dumps(valid_xhs_evidence()), encoding="utf-8")
    monkeypatch.setattr(xhs_behavior, "visible_page_state", changing_page_state)
    monkeypatch.setattr(xhs_behavior.asyncio, "sleep", no_sleep)

    with pytest.raises(RuntimeError, match="sms_verification_terminal_detected"):
        await mediacrawler_behavior.run_xhs_continuity_behavior(
            page,
            evidence_path=evidence_path,
            stage="search_results",
        )

    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    verification = persisted["operator_verification_events"][0]
    assert page.brought_to_front == 1
    assert page.goto_calls == []
    assert verification["status"] == "failed"
    assert verification["challenge"] == "sms_verification_terminal"
    assert verification["error"] == (
        "sms_verification_terminal_during_operator_verification"
    )
    assert persisted["status"] == "failed"
    assert persisted["challenge"] == "sms_verification_terminal"


@pytest.mark.asyncio
async def test_xhs_continuity_verification_timeout_is_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_path = tmp_path / "behavior.json"
    evidence_path.write_text(json.dumps(valid_xhs_evidence()), encoding="utf-8")
    monkeypatch.setattr(xhs_behavior, "XHS_CONTINUITY_VERIFY_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(xhs_behavior, "XHS_CONTINUITY_VERIFY_POLL_SECONDS", 0.01)

    with pytest.raises(RuntimeError, match="xhs_continuity_verification_timeout"):
        await mediacrawler_behavior.run_xhs_continuity_behavior(
            FakePage("登录后查看"),
            evidence_path=evidence_path,
            stage="search_results",
        )

    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    verification = persisted["operator_verification_events"][0]
    assert verification["status"] == "failed"
    assert verification["error"] == "operator_verification_timeout"
    assert persisted["status"] == "failed"
    assert persisted["challenge"] == "login_required"


@pytest.mark.asyncio
async def test_xhs_continuity_rate_limit_fails_and_is_persisted(tmp_path: Path) -> None:
    evidence_path = tmp_path / "behavior.json"
    evidence_path.write_text(json.dumps(valid_xhs_evidence()), encoding="utf-8")

    with pytest.raises(RuntimeError, match="rate_limited_detected"):
        await mediacrawler_behavior.run_xhs_continuity_behavior(
            FakePage("请求过于频繁"),
            evidence_path=evidence_path,
            stage="search_results",
        )

    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "failed"
    assert persisted["challenge"] == "rate_limited"
    assert persisted["continuity_events"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_xhs_api_captcha_opens_operator_page_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage()
    redirect_url = page.url
    normal_markers = {
        "captcha_or_verify": False,
        "rate_limited": False,
        "blocked": False,
        "login_required": False,
    }

    async def completed_verification(current_page):
        current_page.url = redirect_url
        return "正常搜索内容", normal_markers

    async def no_sleep(seconds):
        return None

    evidence_path = tmp_path / "behavior.json"
    evidence_path.write_text(json.dumps(valid_xhs_evidence()), encoding="utf-8")
    monkeypatch.setattr(xhs_behavior, "visible_page_state", completed_verification)
    monkeypatch.setattr(xhs_behavior.asyncio, "sleep", no_sleep)

    event = await mediacrawler_behavior.run_xhs_api_captcha_verification(
        page,
        evidence_path=evidence_path,
        verify_type="216",
        verify_uuid="test-uuid",
        verify_biz=461,
    )

    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert page.brought_to_front == 1
    assert event["status"] == "completed"
    assert event["redirect_url"] == redirect_url
    assert event["verify_type"] == "216"
    assert "verifyUuid=test-uuid" in event["captcha_url"]
    assert persisted["operator_verification_events"][0]["status"] == "completed"
    assert persisted["status"] == "completed"


@pytest.mark.asyncio
async def test_xhs_api_captcha_does_not_complete_on_blank_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage()
    redirect_url = page.url
    observations = iter(("", "正常搜索内容", "正常搜索内容"))
    normal_markers = {
        "captcha_or_verify": False,
        "rate_limited": False,
        "blocked": False,
        "login_required": False,
    }

    async def redirecting_page_state(current_page):
        current_page.url = redirect_url
        return next(observations), normal_markers

    async def no_sleep(seconds):
        return None

    evidence_path = tmp_path / "behavior.json"
    evidence_path.write_text(json.dumps(valid_xhs_evidence()), encoding="utf-8")
    monkeypatch.setattr(xhs_behavior, "visible_page_state", redirecting_page_state)
    monkeypatch.setattr(xhs_behavior.asyncio, "sleep", no_sleep)

    event = await mediacrawler_behavior.run_xhs_api_captcha_verification(
        page,
        evidence_path=evidence_path,
        verify_type="216",
        verify_uuid="test-uuid",
        verify_biz=461,
    )

    assert event["status"] == "completed"
    assert event["ready_observations"] == 2


@pytest.mark.asyncio
async def test_xhs_requested_comment_scroll_is_recorded_without_changing_base_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_pause(page, seconds_range, *, reason, log):
        log.append({"event": "pause", "reason": reason, "seconds": 1.0})

    async def fake_comment_scroll(page, profile, events):
        events.append({"event": "human_scroll_complete", "intent": "comments", "passes": 2})
        return {"status": "completed", "action": "comment-scroll", "selector": "#comments"}

    evidence_path = tmp_path / "behavior.json"
    evidence_path.write_text(json.dumps(valid_xhs_evidence()), encoding="utf-8")
    monkeypatch.setattr(xhs_behavior, "human_pause", fake_pause)
    monkeypatch.setattr(xhs_behavior, "_run_xhs_comment_scroll", fake_comment_scroll)

    interaction = await mediacrawler_behavior.run_xhs_post_interaction(
        FakePage(),
        evidence_path=evidence_path,
        requested_mode="comment-scroll",
        note_id="note-1",
    )

    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert interaction["status"] == "completed"
    assert persisted["status"] == "completed"
    assert persisted["post_interactions"][0]["selected_mode"] == "comment-scroll"


@pytest.mark.asyncio
async def test_xhs_like_does_not_click_an_already_liked_control(monkeypatch: pytest.MonkeyPatch) -> None:
    class LikeLocator:
        clicked = False

        async def count(self) -> int:
            return 1

        def nth(self, index: int):
            assert index == 0
            return self

        async def is_visible(self, timeout: int) -> bool:
            return True

        async def evaluate(self, script: str) -> dict:
            return {
                "aria_pressed": "true",
                "aria_label": "取消点赞",
                "title": "",
                "data_state": "active",
                "class_name": "like-wrapper active",
                "text": "10",
                "icon_ref": "#liked",
                "active_descendant": True,
            }

        async def click(self, **kwargs) -> None:
            self.clicked = True

    class LikePage:
        def __init__(self) -> None:
            self.like = LikeLocator()

        def locator(self, selector: str) -> LikeLocator:
            return self.like

    async def no_mouse_moves(page, profile, log) -> None:
        return None

    page = LikePage()
    monkeypatch.setattr(xhs_behavior, "random_mouse_moves", no_mouse_moves)

    result = await mediacrawler_behavior._run_xhs_like_once(page, object(), [])

    assert result["status"] == "skipped_already_liked"
    assert page.like.clicked is False


@pytest.mark.asyncio
async def test_xhs_like_clicks_once_and_verifies_state_change(monkeypatch: pytest.MonkeyPatch) -> None:
    class LikeLocator:
        click_count = 0

        async def count(self) -> int:
            return 1

        def nth(self, index: int):
            assert index == 0
            return self

        async def is_visible(self, timeout: int) -> bool:
            return True

        async def evaluate(self, script: str) -> dict:
            liked = self.click_count == 1
            return {
                "aria_pressed": "true" if liked else "false",
                "aria_label": "取消点赞" if liked else "点赞",
                "title": "",
                "data_state": "active" if liked else "inactive",
                "class_name": "like-wrapper active" if liked else "like-wrapper",
                "text": "11" if liked else "10",
                "icon_ref": "#liked" if liked else "#like",
                "active_descendant": liked,
            }

        async def scroll_into_view_if_needed(self, timeout: int) -> None:
            return None

        async def hover(self, timeout: int) -> None:
            return None

        async def click(self, **kwargs) -> None:
            self.click_count += 1

    class LikePage:
        def __init__(self) -> None:
            self.like = LikeLocator()

        def locator(self, selector: str) -> LikeLocator:
            return self.like

    async def no_mouse_moves(page, profile, log) -> None:
        return None

    async def no_pause(page, seconds_range, *, reason, log) -> None:
        return None

    page = LikePage()
    monkeypatch.setattr(xhs_behavior, "random_mouse_moves", no_mouse_moves)
    monkeypatch.setattr(xhs_behavior, "human_pause", no_pause)

    result = await mediacrawler_behavior._run_xhs_like_once(page, object(), [])

    assert result["status"] == "completed"
    assert page.like.click_count == 1


@pytest.mark.asyncio
async def test_human_scroll_records_observed_page_movement(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedRandom:
        @staticmethod
        def randint(low: int, high: int) -> int:
            return low

        @staticmethod
        def random() -> float:
            return 1.0

    class Mouse:
        def __init__(self, page) -> None:
            self.page = page

        async def move(self, x: int, y: int, steps: int) -> None:
            return None

        async def wheel(self, delta_x: int, delta_y: int) -> None:
            self.page.scroll_top = max(0, self.page.scroll_top + delta_y)

    class ScrollPage:
        def __init__(self) -> None:
            self.scroll_top = 0
            self.mouse = Mouse(self)

        async def evaluate(self, script: str) -> dict:
            if "window.innerWidth" in script:
                return {"width": 1280, "height": 900}
            return {
                "window_y": self.scroll_top,
                "scrollable_count": 1,
                "scroll_top_sum": self.scroll_top,
                "max_scroll_top": self.scroll_top,
            }

        async def wait_for_timeout(self, milliseconds: int) -> None:
            return None

    monkeypatch.setattr(human_flow, "RANDOM", FixedRandom())
    events: list[dict] = []
    page = ScrollPage()

    await human_flow.human_scroll(
        page,
        human_flow.load_behavior_profile("xhs_guarded", strict=True),
        intent="list",
        max_passes=1,
        log=events,
    )

    completion = next(item for item in events if item["event"] == "human_scroll_complete")
    assert completion["effective_passes"] == 1
    assert any(item.get("effect_observed") is True for item in events)


def test_all_selected_platforms_require_behavior_and_policy_evidence() -> None:
    events = [
        {"event": "pause"},
        {"event": "mouse_moves"},
        {"event": "human_scroll_complete"},
    ]
    records = [
        {
            "platform": platform,
            "behavior_evidence": {
                "status": "completed",
                "profile": "social_high_risk",
                "events": events,
                "visible_markers": {},
                "url": "https://example.test/search?keyword=青岛旅游",
            },
            "policy_events": [{"allowed": True, "disabled": False}],
        }
        for platform in ("bilibili", "weibo")
    ]

    complete = mediacrawler_crawl.collect_behavior_validation(
        records,
        ["bilibili", "weibo"],
        "青岛旅游",
    )
    missing = mediacrawler_crawl.collect_behavior_validation(
        records[:1],
        ["bilibili", "weibo"],
        "青岛旅游",
    )

    assert complete["ok"] is True
    assert missing["ok"] is False
    assert missing["platforms"]["weibo"]["behavior_status"] == "missing"


def test_behavior_validation_rejects_wrong_keyword_url() -> None:
    records = [
        {
            "platform": "zhihu",
            "behavior_evidence": {
                "status": "completed",
                "profile": "social_high_risk",
                "events": [
                    {"event": "pause"},
                    {"event": "mouse_moves"},
                    {"event": "human_scroll_complete"},
                ],
                "visible_markers": {},
                "url": "https://www.zhihu.com/search?q=python&type=content",
            },
            "policy_events": [{"allowed": True, "disabled": False}],
        }
    ]

    validation = mediacrawler_crawl.collect_behavior_validation(
        records,
        ["zhihu"],
        "青岛旅游",
    )

    assert validation["ok"] is False
    assert validation["platforms"]["zhihu"]["target_url_ok"] is False


def test_xhs_behavior_validation_requires_guarded_request_pacing() -> None:
    evidence = valid_xhs_evidence()
    evidence.update(
        {
            "request_pacing_events": [
                {"stage": "search_results", "seconds": 9.0},
                {"stage": "note_detail", "seconds": 6.0},
                {"stage": "creator_profile", "seconds": 12.0},
            ],
            "continuity_events": [
                {"stage": "search_results", "status": "completed"},
            ],
            "url": "https://www.xiaohongshu.com/search_result?keyword=青岛旅游",
        }
    )
    record = {
        "platform": "xhs",
        "behavior_evidence": evidence,
        "policy_events": [{"allowed": True, "disabled": False}],
    }

    complete = mediacrawler_crawl.collect_behavior_validation([record], ["xhs"], "青岛旅游")
    record["behavior_evidence"]["request_pacing_events"].pop()
    missing = mediacrawler_crawl.collect_behavior_validation([record], ["xhs"], "青岛旅游")

    assert complete["ok"] is True
    assert complete["platforms"]["xhs"]["request_pacing_ok"] is True
    assert missing["ok"] is False
    assert missing["platforms"]["xhs"]["request_pacing_ok"] is False


def test_xhs_behavior_validation_requires_continuity_behavior() -> None:
    evidence = valid_xhs_evidence()
    evidence.update(
        {
            "request_pacing_events": [
                {"stage": "search_results"},
                {"stage": "note_detail"},
                {"stage": "creator_profile"},
            ],
            "continuity_events": [],
            "url": "https://www.xiaohongshu.com/search_result?keyword=青岛旅游",
        }
    )
    record = {
        "platform": "xhs",
        "behavior_evidence": evidence,
        "policy_events": [{"allowed": True, "disabled": False}],
    }

    validation = mediacrawler_crawl.collect_behavior_validation([record], ["xhs"], "青岛旅游")

    assert validation["ok"] is False
    assert validation["platforms"]["xhs"]["continuity_ok"] is False


def test_xhs_interaction_is_reported_but_does_not_invalidate_crawl() -> None:
    evidence = valid_xhs_evidence()
    evidence.update(
        {
            "request_pacing_events": [
                {"stage": "search_results"},
                {"stage": "note_detail"},
                {"stage": "creator_profile"},
            ],
            "continuity_events": [
                {"stage": "search_results", "status": "completed"},
            ],
            "post_interactions": [
                {"requested_mode": "like-one", "selected_mode": "like-one", "status": "failed"}
            ],
            "url": "https://www.xiaohongshu.com/search_result?keyword=青岛旅游",
        }
    )
    record = {
        "platform": "xhs",
        "behavior_evidence": evidence,
        "policy_events": [{"allowed": True, "disabled": False}],
    }

    validation = mediacrawler_crawl.collect_behavior_validation(
        [record],
        ["xhs"],
        "青岛旅游",
        "like-one",
    )

    assert validation["ok"] is True
    assert validation["platforms"]["xhs"]["post_interaction_requested"] is True
    assert validation["platforms"]["xhs"]["post_interaction_ok"] is False


GENERIC_BEHAVIOR_PLATFORMS = ("bilibili", "weibo", "douyin", "zhihu")


@pytest.mark.parametrize("missing_platform", GENERIC_BEHAVIOR_PLATFORMS)
def test_behavior_validation_requires_evidence_for_each_generic_platform(missing_platform: str) -> None:
    """T13：四个通用站共用非 XHS 行为门禁分支，逐站证明缺证据即失败、其余站不受影响。"""
    events = [{"event": "pause"}, {"event": "mouse_moves"}, {"event": "human_scroll_complete"}]
    records = [
        {
            "platform": platform,
            "behavior_evidence": {
                "status": "completed",
                "profile": "social_high_risk",
                "events": events,
                "visible_markers": {},
                "url": "https://example.test/search?keyword=青岛旅游",
            },
            "policy_events": [{"allowed": True, "disabled": False}],
        }
        for platform in GENERIC_BEHAVIOR_PLATFORMS
    ]

    complete = mediacrawler_crawl.collect_behavior_validation(records, list(GENERIC_BEHAVIOR_PLATFORMS), "青岛旅游")
    partial = mediacrawler_crawl.collect_behavior_validation(
        [record for record in records if record["platform"] != missing_platform],
        list(GENERIC_BEHAVIOR_PLATFORMS),
        "青岛旅游",
    )

    assert complete["ok"] is True
    assert partial["ok"] is False
    assert partial["platforms"][missing_platform]["behavior_status"] == "missing"
    for platform in GENERIC_BEHAVIOR_PLATFORMS:
        if platform != missing_platform:
            assert partial["platforms"][platform]["behavior_status"] != "missing"
