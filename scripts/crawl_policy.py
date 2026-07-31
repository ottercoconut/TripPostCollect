"""Shared crawl pacing, budget, and cooldown enforcement."""

from __future__ import annotations

import contextlib
import fcntl
import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from trippostcollect.core.paths import CRAWL_POLICY_STATE, LOCK_DIR, PROJECT_ROOT, ensure_dir, ensure_parent
from trippostcollect.platforms.registry import WebSite


ROOT = PROJECT_ROOT
POLICY_STATE = CRAWL_POLICY_STATE
RANDOM = random.SystemRandom()


class CrawlPolicyBlocked(RuntimeError):
    """Raised when a site request is blocked by budget or cooldown policy."""

    def __init__(self, event: dict[str, Any]) -> None:
        self.event = event
        reason = event.get("reason", "crawl_policy_blocked")
        wait_seconds = event.get("wait_seconds", 0)
        super().__init__(f"{reason}: wait {wait_seconds}s before requesting {event.get('site', '')}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def parse_iso_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_policy_state(path: Path | None = None) -> dict[str, Any]:
    path = path or POLICY_STATE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_policy_state(state: dict[str, Any], path: Path | None = None) -> None:
    path = path or POLICY_STATE
    path = ensure_parent(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def clear_site_policy_state(site_key: str) -> dict[str, Any] | None:
    """Remove obsolete shared-policy state for a platform with an independent scheduler."""
    with site_policy_lock(site_key):
        state = load_policy_state()
        removed = state.pop(site_key, None)
        if removed is None:
            return None
        save_policy_state(state)
        return {
            "site": site_key,
            "cleared": True,
            "prior_cooldown_reason": str(removed.get("cooldown_reason") or ""),
            "prior_cooldown_until": str(removed.get("cooldown_until") or ""),
            "state_path": str(POLICY_STATE),
        }


@contextlib.contextmanager
def site_policy_lock(site_key: str) -> Iterator[None]:
    lock_path = ensure_dir(LOCK_DIR) / f"{site_key}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def jitter_window_seconds(site: WebSite) -> float:
    ratio = float(getattr(site, "jitter_ratio", 0.25) or 0.25)
    minimum = float(getattr(site, "min_jitter_seconds", 2.0) or 2.0)
    maximum = float(getattr(site, "max_jitter_seconds", 45.0) or 45.0)
    return max(0.0, min(maximum, max(minimum, site.min_delay_seconds * ratio)))


def positive_jitter_seconds(site: WebSite) -> float:
    window = jitter_window_seconds(site)
    return RANDOM.uniform(0.0, window) if window > 0 else 0.0


def varied_wait_seconds(
    base_seconds: float,
    *,
    ratio: float = 0.35,
    floor_seconds: float = 0.1,
    ceiling_seconds: float | None = None,
) -> float:
    if base_seconds <= 0:
        return 0.0
    delta = max(0.0, base_seconds * ratio)
    value = RANDOM.uniform(max(floor_seconds, base_seconds - delta), base_seconds + delta)
    if ceiling_seconds is not None:
        value = min(value, ceiling_seconds)
    return max(floor_seconds, value)


def _current_daily_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _seconds_until_next_utc_day(now: datetime) -> int:
    next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((next_day - now).total_seconds()))


def _automatic_session_cooldown_until(
    site: WebSite,
    entry: dict[str, Any],
    now: datetime,
) -> datetime:
    last_at = parse_iso_timestamp(
        entry.get("last_request_finished_at") or entry.get("last_request_at")
    )
    cooldown_origin = last_at or now
    return cooldown_origin + timedelta(minutes=site.cooldown_minutes)


def _normalize_entry(entry: dict[str, Any], site: WebSite, now: datetime) -> dict[str, Any]:
    daily_key = _current_daily_key(now)
    daily_reset = entry.get("daily_date") != daily_key
    if daily_reset:
        entry["daily_date"] = daily_key
        entry["daily_count"] = 0
        entry["session_count"] = 0

    cooldown_until = parse_iso_timestamp(entry.get("cooldown_until"))
    cooldown_reason = str(entry.get("cooldown_reason") or "")
    last_request_at = parse_iso_timestamp(entry.get("last_request_finished_at") or entry.get("last_request_at"))
    session_idle = bool(
        last_request_at
        and (now - last_request_at).total_seconds() >= max(0, site.cooldown_minutes) * 60
    )
    automatic_cooldown = cooldown_reason == "max_requests_per_session"
    if automatic_cooldown and last_request_at:
        cooldown_until = _automatic_session_cooldown_until(site, entry, now)
        entry["cooldown_until"] = isoformat(cooldown_until)

    if cooldown_until and cooldown_until <= now:
        entry["session_count"] = 0
        entry.pop("cooldown_until", None)
        entry.pop("cooldown_reason", None)
    elif automatic_cooldown and (daily_reset or session_idle):
        entry["session_count"] = 0
        entry.pop("cooldown_until", None)
        entry.pop("cooldown_reason", None)
    elif cooldown_until is None and session_idle:
        entry["session_count"] = 0

    entry["min_delay_seconds"] = site.min_delay_seconds
    entry["max_requests_per_session"] = site.max_requests_per_session
    entry["daily_request_budget"] = site.daily_request_budget
    entry["cooldown_minutes"] = site.cooldown_minutes
    entry["recommended_mode"] = site.recommended_mode
    return entry


def _blocked_event(
    site: WebSite,
    *,
    reason: str,
    wait_seconds: int,
    label: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    return {
        "site": site.key,
        "label": label,
        "allowed": False,
        "reason": reason,
        "wait_seconds": wait_seconds,
        "daily_count": int(entry.get("daily_count") or 0),
        "daily_request_budget": site.daily_request_budget,
        "session_count": int(entry.get("session_count") or 0),
        "max_requests_per_session": site.max_requests_per_session,
        "cooldown_until": entry.get("cooldown_until", ""),
        "state_path": str(POLICY_STATE),
    }


def _compute_pacing_wait(site: WebSite, entry: dict[str, Any], now: datetime) -> tuple[float, float, float]:
    last_at = parse_iso_timestamp(entry.get("last_request_finished_at") or entry.get("last_request_at"))
    if not last_at:
        return 0.0, 0.0, 0.0

    elapsed = max(0.0, (now - last_at).total_seconds())
    base_wait = max(0.0, float(site.min_delay_seconds) - elapsed)
    jitter = positive_jitter_seconds(site)
    if base_wait > 0:
        return base_wait + jitter, base_wait, jitter

    idle_jitter = min(jitter, 5.0)
    return idle_jitter, 0.0, idle_jitter


@contextlib.contextmanager
def site_request_guard(
    site: WebSite | None,
    *,
    label: str,
    disabled: bool = False,
) -> Iterator[dict[str, Any]]:
    if disabled or site is None:
        yield {
            "site": getattr(site, "key", ""),
            "label": label,
            "allowed": True,
            "disabled": True,
            "wait_seconds": 0.0,
            "state_path": str(POLICY_STATE),
        }
        return

    with site_policy_lock(site.key):
        state = load_policy_state()
        now = utc_now()
        entry = _normalize_entry(dict(state.get(site.key, {})), site, now)

        cooldown_until = parse_iso_timestamp(entry.get("cooldown_until"))
        if cooldown_until and cooldown_until > now:
            event = _blocked_event(
                site,
                reason=str(entry.get("cooldown_reason") or "cooldown_active"),
                wait_seconds=max(1, int((cooldown_until - now).total_seconds())),
                label=label,
                entry=entry,
            )
            entry["last_policy_event"] = event
            state[site.key] = entry
            save_policy_state(state)
            raise CrawlPolicyBlocked(event)

        if site.daily_request_budget <= 0:
            event = _blocked_event(
                site,
                reason="daily_request_budget_disabled",
                wait_seconds=_seconds_until_next_utc_day(now),
                label=label,
                entry=entry,
            )
            entry["last_policy_event"] = event
            state[site.key] = entry
            save_policy_state(state)
            raise CrawlPolicyBlocked(event)

        if int(entry.get("daily_count") or 0) >= site.daily_request_budget:
            event = _blocked_event(
                site,
                reason="daily_request_budget_exhausted",
                wait_seconds=_seconds_until_next_utc_day(now),
                label=label,
                entry=entry,
            )
            entry["last_policy_event"] = event
            state[site.key] = entry
            save_policy_state(state)
            raise CrawlPolicyBlocked(event)

        if site.max_requests_per_session > 0 and int(entry.get("session_count") or 0) >= site.max_requests_per_session:
            cooldown_until = _automatic_session_cooldown_until(site, entry, now)
            entry["cooldown_until"] = isoformat(cooldown_until)
            entry["cooldown_reason"] = "max_requests_per_session"
            event = _blocked_event(
                site,
                reason="max_requests_per_session",
                wait_seconds=max(1, int((cooldown_until - now).total_seconds())),
                label=label,
                entry=entry,
            )
            entry["last_policy_event"] = event
            state[site.key] = entry
            save_policy_state(state)
            raise CrawlPolicyBlocked(event)

        wait_seconds, base_wait_seconds, jitter_seconds = _compute_pacing_wait(site, entry, now)
        if wait_seconds > 0:
            print(
                f"Policy pacing {site.key}: sleeping {wait_seconds:.1f}s "
                f"(base={base_wait_seconds:.1f}s jitter={jitter_seconds:.1f}s).",
                flush=True,
            )
            time.sleep(wait_seconds)

        started_at = utc_now()
        entry["daily_count"] = int(entry.get("daily_count") or 0) + 1
        entry["session_count"] = int(entry.get("session_count") or 0) + 1
        entry["last_request_started_at"] = isoformat(started_at)
        entry["last_request_at"] = isoformat(started_at)
        entry["last_request_label"] = label

        event = {
            "site": site.key,
            "label": label,
            "allowed": True,
            "disabled": False,
            "started_at": isoformat(started_at),
            "wait_seconds": round(wait_seconds, 3),
            "base_wait_seconds": round(base_wait_seconds, 3),
            "jitter_seconds": round(jitter_seconds, 3),
            "daily_count": entry["daily_count"],
            "daily_request_budget": site.daily_request_budget,
            "session_count": entry["session_count"],
            "max_requests_per_session": site.max_requests_per_session,
            "cooldown_minutes": site.cooldown_minutes,
            "state_path": str(POLICY_STATE),
        }
        entry["last_policy_event"] = event
        state[site.key] = entry
        save_policy_state(state)

        try:
            yield event
        finally:
            finished_at = utc_now()
            entry["last_request_finished_at"] = isoformat(finished_at)
            entry["last_request_at"] = isoformat(finished_at)
            event["finished_at"] = isoformat(finished_at)
            entry["last_policy_event"] = event
            state[site.key] = entry
            save_policy_state(state)


def record_site_cooldown(site: WebSite | None, *, reason: str, evidence: Any = None) -> dict[str, Any] | None:
    if site is None:
        return None
    with site_policy_lock(site.key):
        state = load_policy_state()
        now = utc_now()
        entry = _normalize_entry(dict(state.get(site.key, {})), site, now)
        cooldown_until = now + timedelta(minutes=site.cooldown_minutes)
        event = {
            "site": site.key,
            "allowed": False,
            "reason": reason,
            "evidence": evidence or [],
            "cooldown_until": isoformat(cooldown_until),
            "cooldown_minutes": site.cooldown_minutes,
            "state_path": str(POLICY_STATE),
        }
        entry["cooldown_until"] = event["cooldown_until"]
        entry["cooldown_reason"] = reason
        entry["last_policy_event"] = event
        state[site.key] = entry
        save_policy_state(state)
        return event
