from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path

import pytest

from trippostcollect.platforms.registry import WebSite


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

crawl_policy = import_module("crawl_policy")


def policy_site() -> WebSite:
    return WebSite(
        key="test",
        name="Test",
        default_url="https://example.test/",
        login_url=None,
        cookie_domains=(),
        login_hosts=(),
        min_delay_seconds=10,
        max_requests_per_session=8,
        daily_request_budget=20,
        cooldown_minutes=60,
    )


def entry_at(now: datetime, *, idle: timedelta, session_count: int = 8) -> dict:
    last_request = now - idle
    return {
        "daily_date": now.strftime("%Y-%m-%d"),
        "daily_count": 4,
        "session_count": session_count,
        "last_request_finished_at": crawl_policy.isoformat(last_request),
        "last_request_at": crawl_policy.isoformat(last_request),
    }


def test_idle_session_resets_after_cooldown_window() -> None:
    now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
    normalized = crawl_policy._normalize_entry(entry_at(now, idle=timedelta(hours=2)), policy_site(), now)

    assert normalized["session_count"] == 0
    assert "cooldown_until" not in normalized


def test_recent_session_does_not_reset() -> None:
    now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
    normalized = crawl_policy._normalize_entry(entry_at(now, idle=timedelta(minutes=30)), policy_site(), now)

    assert normalized["session_count"] == 8


def test_active_explicit_cooldown_is_not_reset_by_idle_time() -> None:
    now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
    entry = entry_at(now, idle=timedelta(hours=3))
    entry.update(
        {
            "cooldown_until": crawl_policy.isoformat(now + timedelta(hours=2)),
            "cooldown_reason": "captcha_detected",
        }
    )

    normalized = crawl_policy._normalize_entry(entry, policy_site(), now)

    assert normalized["session_count"] == 8
    assert normalized["cooldown_reason"] == "captcha_detected"
    assert crawl_policy.parse_iso_timestamp(normalized["cooldown_until"]) > now


def test_utc_day_change_starts_a_new_session() -> None:
    now = datetime(2026, 7, 15, 0, 5, tzinfo=timezone.utc)
    entry = entry_at(now, idle=timedelta(minutes=10))
    entry["daily_date"] = "2026-07-14"
    entry["daily_count"] = 20

    normalized = crawl_policy._normalize_entry(entry, policy_site(), now)

    assert normalized["daily_date"] == "2026-07-15"
    assert normalized["daily_count"] == 0
    assert normalized["session_count"] == 0


def test_stale_automatic_cooldown_is_cleared_even_if_until_is_in_future() -> None:
    now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
    entry = entry_at(now, idle=timedelta(hours=3))
    entry.update(
        {
            "cooldown_until": crawl_policy.isoformat(now + timedelta(hours=2)),
            "cooldown_reason": "max_requests_per_session",
        }
    )

    normalized = crawl_policy._normalize_entry(entry, policy_site(), now)

    assert normalized["session_count"] == 0
    assert "cooldown_until" not in normalized
    assert "cooldown_reason" not in normalized


def test_overlong_automatic_cooldown_is_reanchored_to_last_finished() -> None:
    now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
    entry = entry_at(now, idle=timedelta(minutes=30))
    entry.update(
        {
            "cooldown_until": crawl_policy.isoformat(now + timedelta(hours=1)),
            "cooldown_reason": "max_requests_per_session",
        }
    )

    normalized = crawl_policy._normalize_entry(entry, policy_site(), now)

    assert normalized["session_count"] == 8
    assert normalized["cooldown_reason"] == "max_requests_per_session"
    assert normalized["cooldown_until"] == crawl_policy.isoformat(
        now + timedelta(minutes=30)
    )


def test_session_limit_cooldown_uses_last_finished_time(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "policy.json"
    monkeypatch.setattr(crawl_policy, "POLICY_STATE", state_path)
    monkeypatch.setattr(crawl_policy, "utc_now", lambda: now)
    crawl_policy.save_policy_state(
        {"test": entry_at(now, idle=timedelta(minutes=30))}
    )

    with pytest.raises(crawl_policy.CrawlPolicyBlocked) as caught:
        with crawl_policy.site_request_guard(policy_site(), label="test-session"):
            raise AssertionError("session limit must block before yielding")

    event = caught.value.event
    assert event["reason"] == "max_requests_per_session"
    assert event["wait_seconds"] == 30 * 60
    assert event["cooldown_until"] == crawl_policy.isoformat(
        now + timedelta(minutes=30)
    )


def test_clear_site_policy_state_removes_only_requested_platform(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "policy.json"
    monkeypatch.setattr(crawl_policy, "POLICY_STATE", state_path)
    crawl_policy.save_policy_state(
        {
            "xhs": {
                "cooldown_reason": "captcha_detected",
                "cooldown_until": "2026-07-21T17:42:46+00:00",
            },
            "weibo": {"daily_count": 2},
        }
    )

    event = crawl_policy.clear_site_policy_state("xhs")

    persisted = crawl_policy.load_policy_state()
    assert event is not None
    assert event["prior_cooldown_reason"] == "captcha_detected"
    assert "xhs" not in persisted
    assert persisted["weibo"] == {"daily_count": 2}
