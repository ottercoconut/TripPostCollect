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
from trippostcollect.core.paths import DEFAULT_DB, XHS_LOGIN_OUTPUT, ensure_dir
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.xhs.accounts import (
    XhsAccountUnavailable,
    acquire_account_login_lease,
    account_paths,
    ensure_xhs_schema,
    get_account,
    mark_account_verified,
    record_event,
    release_account_lease,
    set_account_status,
    validate_account_id,
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
    return {
        "ok": bool(me_visible and profile_ids),
        "url": page.url,
        "me_visible": me_visible,
        "profile_ids": profile_ids,
        "platform_security_limit": platform_security_limit,
        "challenge_markers": challenge_markers,
        "visible_text_sample": normalized_text[:360],
    }


async def wait_for_login(
    page: Page,
    timeout_seconds: int,
    *,
    phase: str,
) -> dict[str, Any]:
    started = time.monotonic()
    state: dict[str, Any] = {}
    observed_challenge_markers: set[str] = set()
    challenge_announced = False
    while time.monotonic() - started < timeout_seconds:
        state = await xhs_page_state(page)
        challenge_markers = set(state.get("challenge_markers") or [])
        observed_challenge_markers.update(challenge_markers)
        state["challenge_observed"] = bool(observed_challenge_markers)
        state["observed_challenge_markers"] = sorted(observed_challenge_markers)
        if state.get("platform_security_limit"):
            try:
                await page.bring_to_front()
            except Exception:
                pass
            print(
                f"[xhs-login] {phase}检测到平台安全限制，终止本轮。",
                flush=True,
            )
            return state
        # A stale signed-in navigation shell can remain visible behind a
        # verification overlay.  Current challenge evidence therefore wins
        # over the profile marker and keeps the operator window open.
        if state["ok"] and not challenge_markers:
            return state
        if challenge_markers and not challenge_announced:
            try:
                await page.bring_to_front()
            except Exception:
                pass
            print(
                f"[xhs-login] {phase}检测到安全验证，已保留并置前页面，"
                f"等待人工处理，最长 {timeout_seconds} 秒。",
                flush=True,
            )
            challenge_announced = True
        await page.wait_for_timeout(2_000)
    state["challenge_observed"] = bool(observed_challenge_markers)
    state["observed_challenge_markers"] = sorted(observed_challenge_markers)
    return state


def login_lease_seconds(timeout_seconds: int) -> int:
    """Cover full operator waits for initial login and reopen verification."""
    return max(timeout_seconds * 2 + 300, 600)


async def open_account_context(playwright: Any, profile_dir: Path, browser_path: str | None) -> BrowserContext:
    return await launch_login_context(
        playwright,
        profile_dir,
        browser_path or discover_cdp_browser_path(),
        native_window_size=XHS_NATIVE_WINDOW_SIZE,
    )


async def single_login_page(context: BrowserContext) -> Page:
    """Reuse one existing page and close stale pages while a user is logging in."""
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
) -> tuple[int, dict[str, Any]]:
    paths = account_paths(account_id)
    ensure_dir(paths["profile"])
    output_dir = ensure_dir(XHS_LOGIN_OUTPUT / utc_stamp())
    screenshot_path = output_dir / "challenge.png"
    key = load_snapshot_key(create=True)
    initial_state: dict[str, Any] = {}
    persisted_state: dict[str, Any] = {}
    error = ""
    challenge_phases: list[str] = []

    async with async_playwright() as playwright:
        context = await open_account_context(playwright, paths["profile"], args.browser_path)
        if paths["encrypted_state"].is_file():
            await restore_context_state(
                context,
                decrypt_storage_state(paths["encrypted_state"], account_id=account_id, key=key),
            )
        page = await single_login_page(context)
        try:
            await page.goto(XHS_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            initial_state = await wait_for_login(
                page,
                args.timeout_seconds,
                phase="初次登录",
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
                storage_state = await capture_context_state(
                    context,
                    account_id=account_id,
                    identity_hash=identity_hash,
                )
                encrypt_storage_state(storage_state, paths["encrypted_state"], account_id=account_id, key=key)
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
        except PlaywrightTimeoutError as exc:
            error = f"navigation_timeout:{exc}"
        finally:
            await context.close()

        if not error:
            verify_context = await open_account_context(playwright, paths["profile"], args.browser_path)
            await restore_context_state(
                verify_context,
                decrypt_storage_state(paths["encrypted_state"], account_id=account_id, key=key),
            )
            verify_page = await single_login_page(verify_context)
            try:
                await verify_page.goto(XHS_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
                persisted_state = await wait_for_login(
                    verify_page,
                    args.timeout_seconds,
                    phase="关闭重开复验",
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
                    )
                    encrypt_storage_state(
                        storage_state,
                        paths["encrypted_state"],
                        account_id=account_id,
                        key=key,
                    )
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
    db_path = Path(args.db).expanduser()
    bootstrap_database(db_path, sync_jobs=False)
    run_id = f"xhs-login-{account_id}-{utc_stamp()}"
    lease_seconds = login_lease_seconds(args.timeout_seconds)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        account = get_account(conn, account_id)
        if not account:
            raise SystemExit(f"XHS account is not enrolled: {account_id}")
        if account["status"] == "retired":
            raise SystemExit(f"XHS account is retired: {account_id}")
        acquire_account_login_lease(
            conn,
            run_id=run_id,
            requested_account_id=account_id,
            lease_seconds=lease_seconds,
        )

    succeeded = False
    try:
        code, summary = await _run_login_session(
            args,
            account_id=account_id,
            db_path=db_path,
            run_id=run_id,
        )
        succeeded = code == 0
        return code, summary
    finally:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_xhs_schema(conn)
            release_account_lease(
                conn,
                account_id=account_id,
                run_id=run_id,
                outcome="completed" if succeeded else "failed",
            )


def main() -> int:
    args = parse_args()
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive")
    try:
        code, summary = asyncio.run(run_login(args))
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
        db_path = Path(args.db).expanduser()
        bootstrap_database(db_path, sync_jobs=False)
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
