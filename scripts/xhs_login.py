#!/usr/bin/env python3
"""Login and persist one isolated Xiaohongshu account."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from browser_runtime import XHS_NATIVE_WINDOW_SIZE
from mediacrawler_crawl import discover_cdp_browser_path
from mediacrawler_login_warmup import launch_login_context
from trippostcollect.core.paths import (
    DEFAULT_DB,
    XHS_LOGIN_EXECUTION_STATE_ROOT,
    XHS_LOGIN_OUTPUT,
    ensure_dir,
)
from trippostcollect.xhs.accounts import (
    XhsAccountUnavailable,
    account_paths,
    bootstrap_xhs_control_database,
    ensure_xhs_schema,
    get_account,
    mark_account_verified,
    record_event,
    set_account_status,
    validate_account_id,
)
from trippostcollect.xhs.leases import (
    LeaseGuard,
    XhsLeaseProcessesAlive,
    XhsLeaseSignal,
    login_lease_budget,
)
from trippostcollect.xhs.sessions import (
    capture_context_state,
    decrypt_storage_state,
    encrypt_storage_state,
    load_snapshot_key,
    restore_context_state,
)


XHS_HOME_URL = "https://www.xiaohongshu.com"
PROFILE_ID_RE = re.compile(r"/user/profile/([^/?#]+)")
CHALLENGE_RE = re.compile(
    r"安全验证|请完成验证|请通过验证|操作频繁|环境异常|访问受限|安全限制|账号异常|"
    r"account exception(?:\s*,?\s*please retry later)?|\b300011\b",
    re.I,
)
PLATFORM_SECURITY_LIMIT_URL_RE = re.compile(r"/website-login/error(?:[?#]|$)", re.I)
SELF_INFO_PATH = "/api/sns/web/v1/user/selfinfo"
LOGIN_STABILITY_CONFIRMATIONS = 4
LOGIN_STABILITY_POLL_MS = 5_000
REQUIRED_LOGIN_COOKIES = frozenset({"a1", "webId", "web_session"})
REQUIRED_SESSION_STORAGE = frozenset({"XHS_RWP_FINGERPRINT", "XHS_TAB_DEVICE_ID"})
LOGIN_LOCAL_STORAGE_KEYS = frozenset({"RWP_LOGIN_TOKEN", "b1", "webSsk"})


class SelfInfoMonitor:
    def __init__(self) -> None:
        self.observed = 0
        self.ok = False
        self.last_status: int | None = None
        self.last_observed_at = ""
        self._tasks: set[asyncio.Task[None]] = set()

    def attach(self, context: BrowserContext) -> None:
        context.on("response", self._on_response)

    def _on_response(self, response: Any) -> None:
        if SELF_INFO_PATH not in str(getattr(response, "url", "") or ""):
            return
        task = asyncio.create_task(self._consume(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _consume(self, response: Any) -> None:
        self.observed += 1
        self.last_status = int(getattr(response, "status", 0) or 0)
        self.last_observed_at = utc_iso()
        try:
            payload = await response.json()
        except Exception:
            self.ok = False
            return
        result = ((payload or {}).get("data") or {}).get("result") or {}
        self.ok = bool(self.last_status == 200 and result.get("success"))

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "observed": self.observed,
            "last_status": self.last_status,
            "last_observed_at": self.last_observed_at,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Login one isolated Xiaohongshu account and verify persistence.")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--browser-path")
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f%z")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def xhs_page_state(page: Page) -> dict[str, Any]:
    try:
        text = await page.locator("body").inner_text(timeout=5_000)
    except Exception:
        text = ""
    links = page.locator("a[href*='/user/profile/']")
    profile_ids: list[str] = []
    me_visible = False
    for index in range(min(await links.count(), 20)):
        link = links.nth(index)
        href = str(await link.get_attribute("href") or "")
        match = PROFILE_ID_RE.search(href)
        try:
            link_text = " ".join((await link.inner_text(timeout=1_000)).split())
            if link_text == "我" and await link.is_visible():
                me_visible = True
                if match:
                    profile_ids.append(match.group(1))
        except Exception:
            continue
    profile_ids = sorted(set(profile_ids))
    normalized_text = " ".join(text.split())
    page_url = str(page.url or "")
    platform_security_limit = bool(
        re.search(
            r"安全限制|账号异常|account exception(?:\s*,?\s*please retry later)?|\b300011\b",
            normalized_text,
            re.I,
        )
        or PLATFORM_SECURITY_LIMIT_URL_RE.search(page_url)
    )
    challenge_markers = sorted(set(CHALLENGE_RE.findall(normalized_text)))
    if PLATFORM_SECURITY_LIMIT_URL_RE.search(page_url):
        challenge_markers.append("website-login/error")
        challenge_markers = sorted(set(challenge_markers))
    continuity: dict[str, Any]
    try:
        cookies = await page.context.cookies([XHS_HOME_URL])
        cookie_values = {
            str(item.get("name") or ""): str(item.get("value") or "")
            for item in cookies
            if isinstance(item, dict) and item.get("name") and item.get("value")
        }
        storage = await page.evaluate(
            """
            () => ({
              localStorage: Object.fromEntries(Object.entries(window.localStorage || {})),
              sessionStorage: Object.fromEntries(Object.entries(window.sessionStorage || {}))
            })
            """
        )
        local_storage = dict((storage or {}).get("localStorage") or {})
        session_storage = dict((storage or {}).get("sessionStorage") or {})
        present_cookies = sorted(REQUIRED_LOGIN_COOKIES.intersection(cookie_values))
        present_local = sorted(LOGIN_LOCAL_STORAGE_KEYS.intersection(local_storage))
        present_session = sorted(REQUIRED_SESSION_STORAGE.intersection(session_storage))
        continuity_ready = bool(
            len(present_cookies) == len(REQUIRED_LOGIN_COOKIES)
            and present_local
            and len(present_session) == len(REQUIRED_SESSION_STORAGE)
        )
        continuity_token = hashlib.sha256(
            json.dumps(
                {
                    "cookies": {key: cookie_values.get(key, "") for key in sorted(REQUIRED_LOGIN_COOKIES)},
                    "localStorage": {
                        key: local_storage.get(key, "")
                        for key in sorted(LOGIN_LOCAL_STORAGE_KEYS)
                    },
                    "sessionStorage": {
                        key: session_storage.get(key, "")
                        for key in sorted(REQUIRED_SESSION_STORAGE)
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        continuity = {
            "ready": continuity_ready,
            "required_cookies": present_cookies,
            "local_storage_markers": present_local,
            "session_storage_markers": present_session,
            "_token": continuity_token,
        }
    except Exception as exc:
        continuity = {
            "ready": False,
            "required_cookies": [],
            "local_storage_markers": [],
            "session_storage_markers": [],
            "error": f"{type(exc).__name__}: {exc}",
            "_token": "",
        }
    return {
        "ok": bool(me_visible and profile_ids),
        "url": page.url,
        "me_visible": me_visible,
        "profile_ids": profile_ids,
        "platform_security_limit": platform_security_limit,
        "challenge_markers": challenge_markers,
        "continuity": continuity,
        "visible_text_sample": normalized_text[:360],
    }


def _public_login_state(state: dict[str, Any]) -> dict[str, Any]:
    public = dict(state)
    continuity = dict(public.get("continuity") or {})
    continuity.pop("_token", None)
    public["continuity"] = continuity
    return public


async def latest_xhs_page(context: BrowserContext, fallback: Page) -> Page:
    try:
        pages = [page for page in context.pages if not page.is_closed()]
    except Exception:
        pages = []
    for candidate in reversed(pages):
        url = str(getattr(candidate, "url", "") or "")
        if "xiaohongshu.com" in url or "rednote.com" in url:
            return candidate
    if fallback in pages:
        return fallback
    return pages[-1] if pages else fallback


def _target_closed_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return bool(
        exc.__class__.__name__ == "TargetClosedError"
        or "target page, context or browser has been closed" in text
        or "targetclosederror" in text
    )


async def navigate_login_page(context: BrowserContext, page: Page) -> Page:
    try:
        await page.goto(XHS_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        return page
    except Exception as exc:
        if not _target_closed_error(exc):
            raise
    replacement = await latest_xhs_page(context, page)
    if replacement is page or replacement.is_closed():
        raise RuntimeError("xhs_login_target_closed_without_replacement")
    await replacement.bring_to_front()
    return replacement


async def wait_for_login(
    page: Page,
    timeout_seconds: int,
    *,
    phase: str,
    context: BrowserContext | None = None,
    self_info_monitor: SelfInfoMonitor | None = None,
) -> tuple[Page, dict[str, Any]]:
    started = time.monotonic()
    state: dict[str, Any] = {}
    observed_challenge_markers: set[str] = set()
    challenge_announced = False
    stable_key: tuple[str, str] | None = None
    stable_confirmations = 0
    page_replacements = 0
    active_page = page
    while time.monotonic() - started < timeout_seconds:
        if context is not None:
            candidate = await latest_xhs_page(context, active_page)
            if candidate is not active_page:
                page_replacements += 1
                active_page = candidate
        try:
            state = await xhs_page_state(active_page)
        except Exception as exc:
            if context is None or not _target_closed_error(exc):
                raise
            candidate = await latest_xhs_page(context, active_page)
            if candidate is active_page or candidate.is_closed():
                raise
            page_replacements += 1
            active_page = candidate
            continue
        challenge_markers = set(state.get("challenge_markers") or [])
        observed_challenge_markers.update(challenge_markers)
        state["challenge_observed"] = bool(observed_challenge_markers)
        state["observed_challenge_markers"] = sorted(observed_challenge_markers)
        state["page_replacements"] = page_replacements
        state["ui_ok"] = bool(state.get("ok"))
        self_info = self_info_monitor.public() if self_info_monitor is not None else {"ok": True}
        state["self_info"] = self_info
        continuity = state.get("continuity") or {}
        candidate_key = (
            str((state.get("profile_ids") or [""])[0]),
            str(continuity.get("_token") or ""),
        )
        session_ready = bool(
            state["ui_ok"]
            and continuity.get("ready")
            and self_info.get("ok")
            and not challenge_markers
            and all(candidate_key)
        )
        if session_ready:
            stable_confirmations = stable_confirmations + 1 if candidate_key == stable_key else 1
            stable_key = candidate_key
        else:
            stable_key = None
            stable_confirmations = 0
        state["stability_confirmations"] = stable_confirmations
        state["stability_required"] = LOGIN_STABILITY_CONFIRMATIONS
        state["ok"] = bool(session_ready and stable_confirmations >= LOGIN_STABILITY_CONFIRMATIONS)
        if state.get("platform_security_limit"):
            try:
                await active_page.bring_to_front()
            except Exception:
                pass
            print(
                f"[xhs-login] {phase}检测到平台安全限制，终止本轮。",
                flush=True,
            )
            return active_page, _public_login_state(state)
        if state["ok"]:
            return active_page, _public_login_state(state)
        if challenge_markers and not challenge_announced:
            try:
                await active_page.bring_to_front()
            except Exception:
                pass
            print(
                f"[xhs-login] {phase}检测到安全验证，已保留并置前页面，"
                f"等待人工处理，最长 {timeout_seconds} 秒。",
                flush=True,
            )
            challenge_announced = True
        await active_page.wait_for_timeout(LOGIN_STABILITY_POLL_MS)
    state["challenge_observed"] = bool(observed_challenge_markers)
    state["observed_challenge_markers"] = sorted(observed_challenge_markers)
    state["ok"] = False
    state["page_replacements"] = page_replacements
    return active_page, _public_login_state(state)


def login_lease_seconds(timeout_seconds: int) -> int:
    return login_lease_budget(timeout_seconds).lease_seconds


def write_login_execution_state(path: Path, value: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


async def open_account_context(playwright: Any, profile_dir: Path, browser_path: str | None) -> BrowserContext:
    return await launch_login_context(
        playwright,
        profile_dir,
        browser_path or discover_cdp_browser_path(),
        native_window_size=XHS_NATIVE_WINDOW_SIZE,
    )


async def single_login_page(context: BrowserContext) -> Page:
    pages = [page for page in context.pages if not page.is_closed()]
    page = pages[0] if pages else await context.new_page()
    for other_page in pages:
        if other_page is page:
            continue
        await other_page.close()
    return page


async def _run_login_session(
    args: argparse.Namespace,
    *,
    account_id: str,
    db_path: Path,
    run_id: str,
    output_dir: Path,
    lease_guard: LeaseGuard,
) -> tuple[int, dict[str, Any]]:
    paths = account_paths(account_id)
    ensure_dir(paths["profile"])
    screenshot_path = output_dir / "challenge.png"
    key = load_snapshot_key(create=True)
    initial_state: dict[str, Any] = {}
    persisted_state: dict[str, Any] = {}
    initial_storage_state: dict[str, Any] = {}
    error = ""
    challenge_phases: list[str] = []

    async with async_playwright() as playwright:
        context = await open_account_context(playwright, paths["profile"], args.browser_path)
        lease_guard.observe_profile_processes()
        page = await single_login_page(context)
        if paths["encrypted_state"].is_file():
            await restore_context_state(
                context,
                decrypt_storage_state(paths["encrypted_state"], account_id=account_id, key=key),
                primary_page=page,
            )
        initial_self_info = SelfInfoMonitor()
        initial_self_info.attach(context)
        try:
            page = await navigate_login_page(context, page)
            page, initial_state = await wait_for_login(
                page,
                args.timeout_seconds,
                phase="初次登录",
                context=context,
                self_info_monitor=initial_self_info,
            )
            if initial_state.get("challenge_observed"):
                challenge_phases.append("initial_login")
            if not initial_state.get("ok") and initial_state.get("challenge_observed"):
                await page.screenshot(path=str(screenshot_path), full_page=False, timeout=10_000)
                error = "xhs_challenge_detected_during_login"
            elif not initial_state.get("ok"):
                error = "xhs_login_timeout"
            else:
                platform_id = str(initial_state["profile_ids"][0])
                identity_hash = hashlib.sha256(platform_id.encode("utf-8")).hexdigest()
                initial_storage_state = await capture_context_state(
                    context,
                    account_id=account_id,
                    identity_hash=identity_hash,
                    primary_page=page,
                    session_verification={
                        "status": "verified",
                        "run_id": run_id,
                        "source": "stable_ui_storage_and_selfinfo",
                        "phase": "initial_login",
                        "verified_at": utc_iso(),
                    },
                )
        except PlaywrightTimeoutError as exc:
            error = f"navigation_timeout:{exc}"
        finally:
            await context.close()

        if not error:
            verify_context = await open_account_context(playwright, paths["profile"], args.browser_path)
            lease_guard.observe_profile_processes()
            verify_page = await single_login_page(verify_context)
            await restore_context_state(
                verify_context,
                initial_storage_state,
                primary_page=verify_page,
            )
            persisted_self_info = SelfInfoMonitor()
            persisted_self_info.attach(verify_context)
            try:
                verify_page = await navigate_login_page(verify_context, verify_page)
                verify_page, persisted_state = await wait_for_login(
                    verify_page,
                    args.timeout_seconds,
                    phase="关闭重开复验",
                    context=verify_context,
                    self_info_monitor=persisted_self_info,
                )
                if persisted_state.get("challenge_observed"):
                    challenge_phases.append("reopen_verification")
                if not persisted_state.get("ok") and persisted_state.get("challenge_observed"):
                    await verify_page.screenshot(
                        path=str(screenshot_path),
                        full_page=False,
                        timeout=10_000,
                    )
                if (
                    persisted_state.get("ok")
                    and persisted_state.get("profile_ids", [None])[0]
                    == initial_state.get("profile_ids", [None])[0]
                ):
                    platform_id = str(persisted_state["profile_ids"][0])
                    identity_hash = hashlib.sha256(platform_id.encode("utf-8")).hexdigest()
                    storage_state = await capture_context_state(
                        verify_context,
                        account_id=account_id,
                        identity_hash=identity_hash,
                        primary_page=verify_page,
                        session_verification={
                            "status": "verified",
                            "run_id": run_id,
                            "source": "stable_ui_storage_and_selfinfo",
                            "phase": "reopen_verification",
                            "verified_at": utc_iso(),
                        },
                    )
                    encrypt_storage_state(
                        storage_state,
                        paths["encrypted_state"],
                        account_id=account_id,
                        key=key,
                    )
                    public_metadata = {
                        key: storage_state["trippostcollect"].get(key)
                        for key in (
                            "schema_version",
                            "platform",
                            "account_id",
                            "identity_hash",
                            "captured_at",
                        )
                    }
                    paths["metadata"].write_text(
                        json.dumps(public_metadata, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    paths["metadata"].chmod(0o600)
            finally:
                await verify_context.close()
            if not persisted_state.get("ok"):
                error = "xhs_persisted_login_not_verified"
            elif persisted_state.get("profile_ids", [None])[0] != initial_state.get("profile_ids", [None])[0]:
                error = "xhs_profile_identity_changed_after_reopen"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        if error:
            set_account_status(conn, account_id, "login_required", reason=error)
        else:
            identity_hash = hashlib.sha256(str(persisted_state["profile_ids"][0]).encode("utf-8")).hexdigest()
            try:
                mark_account_verified(conn, account_id, identity_hash)
            except ValueError as exc:
                error = f"xhs_identity_conflict:{exc}"
                set_account_status(conn, account_id, "quarantined", reason=error)
        if challenge_phases:
            record_event(
                conn,
                account_id=account_id,
                event_type=(
                    "login_challenge_completed"
                    if not error
                    else "login_challenge_detected"
                ),
                details={"error": error, "phases": challenge_phases},
            )
        record_event(
            conn,
            account_id=account_id,
            event_type="login_check_finished",
            details={"ok": not error, "error": error},
        )
        conn.commit()

    summary = {
        "status": "completed" if not error else "failed",
        "run_id": run_id,
        "account_id": account_id,
        "profile_dir": str(paths["profile"]),
        "encrypted_state_path": str(paths["encrypted_state"]),
        "initial_state": initial_state,
        "persisted_state": persisted_state,
        "challenge_screenshot": str(screenshot_path) if screenshot_path.is_file() else None,
        "error": error,
        "finished_at": utc_iso(),
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary"] = str(summary_path)
    return (0 if not error else 1), summary


async def run_login(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    account_id = validate_account_id(args.account_id)
    db_path = bootstrap_xhs_control_database(args.db)
    run_id = f"xhs-login-{account_id}-{utc_stamp()}"
    budget = login_lease_budget(args.timeout_seconds)
    output_dir = ensure_dir(XHS_LOGIN_OUTPUT / run_id)
    state_path = ensure_dir(XHS_LOGIN_EXECUTION_STATE_ROOT) / f"{run_id}.json"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        account = get_account(conn, account_id)
        if not account:
            raise SystemExit(f"XHS account is not enrolled: {account_id}")
        if account["status"] == "retired":
            raise SystemExit(f"XHS account is retired: {account_id}")

    guard = LeaseGuard(
        db_path=db_path,
        account_id=account_id,
        run_id=run_id,
        lease_kind="login",
        execution_state_path=state_path,
        budget=budget,
    )
    with guard:
        running_state = {
            "run_id": run_id,
            "status": "running",
            "plan": {
                "account_id": account_id,
                "lease_kind": "login",
                "lease_budget": budget.public(),
            },
            "steps": {"task_finalized": {"status": "frozen"}},
            "events": [{"at": utc_iso(), "type": "login_started"}],
        }
        write_login_execution_state(state_path, running_state)
        try:
            code, summary = await _run_login_session(
                args,
                account_id=account_id,
                db_path=db_path,
                run_id=run_id,
                output_dir=output_dir,
                lease_guard=guard,
            )
        except Exception as exc:
            failed_state = {
                **running_state,
                "status": "failed",
                "steps": {"task_finalized": {"status": "failed"}},
                "events": [
                    *running_state["events"],
                    {
                        "at": utc_iso(),
                        "type": "login_failed",
                        "details": {"error": f"{type(exc).__name__}: {exc}"},
                    },
                ],
            }
            write_login_execution_state(state_path, failed_state)
            raise
        outcome = "completed" if code == 0 else "failed"
        guard.set_outcome(outcome)
        terminal_state = {
            **running_state,
            "status": outcome,
            "steps": {"task_finalized": {"status": outcome}},
            "events": [
                *running_state["events"],
                {"at": utc_iso(), "type": "login_finished", "details": {"outcome": outcome}},
            ],
        }
        write_login_execution_state(state_path, terminal_state)
        summary["lease_id"] = guard.lease_id
        summary["lease_budget"] = budget.public()
        summary["execution_state"] = str(state_path)
        Path(summary["summary"]).write_text(
            json.dumps({key: value for key, value in summary.items() if key != "summary"}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    summary["lease_released"] = True
    Path(summary["summary"]).write_text(
        json.dumps(
            {key: value for key, value in summary.items() if key != "summary"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return code, summary


def main() -> int:
    args = parse_args()
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive")
    try:
        code, summary = asyncio.run(run_login(args))
    except XhsLeaseSignal as exc:
        account_id = validate_account_id(args.account_id)
        output_dir = ensure_dir(XHS_LOGIN_OUTPUT / utc_stamp())
        summary = {
            "status": "interrupted",
            "account_id": account_id,
            "signal": exc.signum,
            "finished_at": utc_iso(),
        }
        summary_path = output_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["summary"] = str(summary_path)
        code = 128 + exc.signum
    except XhsLeaseProcessesAlive as exc:
        account_id = validate_account_id(args.account_id)
        output_dir = ensure_dir(XHS_LOGIN_OUTPUT / utc_stamp())
        summary = {
            "status": "failed",
            "account_id": account_id,
            "error": f"xhs_login_lease_retained:{exc}",
            "account_health_mutated": False,
            "finished_at": utc_iso(),
        }
        summary_path = output_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["summary"] = str(summary_path)
        code = 2
    except XhsAccountUnavailable as exc:
        account_id = validate_account_id(args.account_id)
        output_dir = ensure_dir(XHS_LOGIN_OUTPUT / utc_stamp())
        summary = {
            "status": "blocked",
            "account_id": account_id,
            "error": exc.reason,
            "wait_seconds": exc.wait_seconds,
            "finished_at": utc_iso(),
        }
        summary_path = output_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["summary"] = str(summary_path)
        code = 2
    except Exception as exc:
        account_id = validate_account_id(args.account_id)
        error = f"xhs_login_runtime_failed:{type(exc).__name__}:{exc}"
        db_path = bootstrap_xhs_control_database(args.db)
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_xhs_schema(conn)
            account = get_account(conn, account_id)
            if account and account["status"] != "retired":
                set_account_status(conn, account_id, "login_required", reason=error)
                record_event(
                    conn,
                    account_id=account_id,
                    event_type="login_runtime_failed",
                    details={"error": error},
                )
                conn.commit()
        output_dir = ensure_dir(XHS_LOGIN_OUTPUT / utc_stamp())
        summary = {
            "status": "failed",
            "account_id": account_id,
            "error": error,
            "finished_at": utc_iso(),
        }
        summary_path = output_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["summary"] = str(summary_path)
        code = 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
