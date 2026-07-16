from __future__ import annotations

import asyncio
import json
import sys
from importlib import import_module
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_behavior = import_module("mediacrawler_behavior")
mediacrawler_crawl = import_module("mediacrawler_crawl")


class FakeLocator:
    def __init__(self, text: str) -> None:
        self.text = text

    async def inner_text(self, timeout: int) -> str:
        return self.text


class FakePage:
    def __init__(self, text: str = "正常搜索内容") -> None:
        self.url = "https://example.test/search"
        self.text = text

    def locator(self, selector: str) -> FakeLocator:
        assert selector == "body"
        return FakeLocator(self.text)

    async def evaluate(self, script: str) -> dict:
        return {
            "webdriver": None,
            "languages": ["zh-CN", "zh"],
            "platform": "MacIntel",
            "user_agent": "test-agent",
            "hardware_concurrency": 8,
            "device_memory": 8,
            "max_touch_points": 0,
            "viewport": {"width": 1440, "height": 900, "device_pixel_ratio": 2},
            "visibility_state": "visible",
        }

    async def screenshot(self, path: str, full_page: bool, timeout: int, animations: str) -> None:
        Path(path).write_bytes(b"png")


async def fake_dwell_on_list(page, profile, log) -> None:
    log.extend(
        [
            {"event": "pause", "reason": "list_dwell_initial", "seconds": 1.0},
            {"event": "mouse_moves", "count": 3},
            {"event": "wheel_scroll", "delta_y": 500},
            {"event": "human_scroll_complete", "intent": "list", "passes": 1},
        ]
    )


@pytest.mark.asyncio
async def test_behavior_stage_writes_complete_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mediacrawler_behavior, "dwell_on_list", fake_dwell_on_list)
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
async def test_visible_challenge_fails_behavior_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mediacrawler_behavior, "dwell_on_list", fake_dwell_on_list)
    evidence_path = tmp_path / "behavior.json"

    with pytest.raises(RuntimeError, match="captcha_or_verify_detected"):
        await mediacrawler_behavior.run_page_behavior(
            FakePage("请完成安全验证"),
            platform_key="xhs",
            evidence_path=evidence_path,
        )

    evidence = json.loads(evidence_path.read_text())
    assert evidence["status"] == "failed"
    assert evidence["events"] == []
    assert evidence["initial_visible_markers"]["captcha_or_verify"] is True
    assert evidence["visible_markers"]["captcha_or_verify"] is True
    assert mediacrawler_behavior.behavior_evidence_valid(evidence) is False


@pytest.mark.asyncio
async def test_xhs_dwell_stops_when_rate_limit_appears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = 0

    async def changing_page_state(page):
        nonlocal checks
        checks += 1
        if checks == 1:
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

    monkeypatch.setattr(mediacrawler_behavior, "visible_page_state", changing_page_state)
    monkeypatch.setattr(mediacrawler_behavior, "dwell_on_list", blocking_dwell)
    monkeypatch.setattr(mediacrawler_behavior, "XHS_VISIBLE_CHECK_INTERVAL_SECONDS", 0.001)
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
    monkeypatch.setattr(mediacrawler_behavior, "dwell_on_list", fake_dwell_on_list)
    evidence = await mediacrawler_behavior.run_page_behavior(
        FakePage("手机号登录 获取验证码"),
        platform_key="xhs",
        evidence_path=tmp_path / "behavior.json",
    )

    assert evidence["visible_markers"]["captcha_or_verify"] is False
    assert mediacrawler_behavior.behavior_evidence_valid(evidence) is True


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
    evidence_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "events": [
                    {"event": "pause"},
                    {"event": "mouse_moves"},
                    {"event": "human_scroll_complete"},
                ],
                "visible_markers": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mediacrawler_behavior, "REQUEST_RANDOM", FixedRandom())
    monkeypatch.setattr(mediacrawler_behavior.asyncio, "sleep", fake_sleep)

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
    evidence_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "profile": "xhs_guarded",
                "events": [
                    {"event": "pause"},
                    {"event": "mouse_moves"},
                    {"event": "human_scroll_complete"},
                ],
                "visible_markers": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mediacrawler_behavior, "human_pause", fake_pause)
    monkeypatch.setattr(mediacrawler_behavior, "_run_xhs_comment_scroll", fake_comment_scroll)

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
    monkeypatch.setattr(mediacrawler_behavior, "random_mouse_moves", no_mouse_moves)

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
    monkeypatch.setattr(mediacrawler_behavior, "random_mouse_moves", no_mouse_moves)
    monkeypatch.setattr(mediacrawler_behavior, "human_pause", no_pause)

    result = await mediacrawler_behavior._run_xhs_like_once(page, object(), [])

    assert result["status"] == "completed"
    assert page.like.click_count == 1


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
    record = {
        "platform": "xhs",
        "behavior_evidence": {
            "status": "completed",
            "profile": "xhs_guarded",
            "events": [
                {"event": "pause"},
                {"event": "mouse_moves"},
                {"event": "human_scroll_complete"},
            ],
            "request_pacing_events": [
                {"stage": "search_results", "seconds": 9.0},
                {"stage": "note_detail", "seconds": 6.0},
                {"stage": "creator_profile", "seconds": 12.0},
            ],
            "visible_markers": {},
            "url": "https://www.xiaohongshu.com/search_result?keyword=青岛旅游",
        },
        "policy_events": [{"allowed": True, "disabled": False}],
    }

    complete = mediacrawler_crawl.collect_behavior_validation([record], ["xhs"], "青岛旅游")
    record["behavior_evidence"]["request_pacing_events"].pop()
    missing = mediacrawler_crawl.collect_behavior_validation([record], ["xhs"], "青岛旅游")

    assert complete["ok"] is True
    assert complete["platforms"]["xhs"]["request_pacing_ok"] is True
    assert missing["ok"] is False
    assert missing["platforms"]["xhs"]["request_pacing_ok"] is False


def test_xhs_interaction_is_reported_but_does_not_invalidate_crawl() -> None:
    record = {
        "platform": "xhs",
        "behavior_evidence": {
            "status": "completed",
            "profile": "xhs_guarded",
            "events": [
                {"event": "pause"},
                {"event": "mouse_moves"},
                {"event": "human_scroll_complete"},
            ],
            "request_pacing_events": [
                {"stage": "search_results"},
                {"stage": "note_detail"},
                {"stage": "creator_profile"},
            ],
            "post_interactions": [
                {"requested_mode": "like-one", "selected_mode": "like-one", "status": "failed"}
            ],
            "visible_markers": {},
            "url": "https://www.xiaohongshu.com/search_result?keyword=青岛旅游",
        },
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
