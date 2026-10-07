from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from trippostcollect.platforms.xhs import core as xhs_core

from .support import dependencies


class _PlaywrightContext:
    def __init__(self) -> None:
        self.playwright = object()

    async def __aenter__(self) -> object:
        return self.playwright

    async def __aexit__(self, *_args: object) -> None:
        return None


def _crawler_without_init() -> xhs_core.XiaoHongShuCrawler:
    return object.__new__(xhs_core.XiaoHongShuCrawler)


@pytest.mark.asyncio
async def test_start_preserves_primary_error_when_shutdown_guard_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = _crawler_without_init()
    primary_error = RuntimeError("request transport failed")
    cleanup_error = RuntimeError("manual checkpoint timeout")
    calls: list[str] = []

    async def fail_session(*_args: object) -> None:
        calls.append("session")
        raise primary_error

    async def fail_shutdown() -> None:
        calls.append("shutdown")
        raise cleanup_error

    monkeypatch.setattr(crawler, "ports", SimpleNamespace(async_playwright=_PlaywrightContext), raising=False)
    monkeypatch.setattr(crawler, "inputs", dependencies()["inputs"], raising=False)
    monkeypatch.setattr(crawler, "_run_browser_session", fail_session)
    monkeypatch.setattr(crawler, "_prepare_browser_shutdown", fail_shutdown)

    with pytest.raises(RuntimeError) as caught:
        await crawler.start()

    assert caught.value is primary_error
    assert cleanup_error is not caught.value
    assert calls == ["session", "shutdown"]
    assert getattr(primary_error, "__notes__", []) == [
        "XHS browser shutdown also failed: RuntimeError: manual checkpoint timeout"
    ]


@pytest.mark.asyncio
async def test_start_preserves_cancellation_when_shutdown_guard_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = _crawler_without_init()
    cancellation = asyncio.CancelledError("operator interrupt")
    calls: list[str] = []

    async def cancel_session(*_args: object) -> None:
        calls.append("session")
        raise cancellation

    async def fail_shutdown() -> None:
        calls.append("shutdown")
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(crawler, "ports", SimpleNamespace(async_playwright=_PlaywrightContext), raising=False)
    monkeypatch.setattr(crawler, "inputs", dependencies()["inputs"], raising=False)
    monkeypatch.setattr(crawler, "_run_browser_session", cancel_session)
    monkeypatch.setattr(crawler, "_prepare_browser_shutdown", fail_shutdown)

    with pytest.raises(asyncio.CancelledError) as caught:
        await crawler.start()

    assert caught.value is cancellation
    assert calls == ["session", "shutdown"]
    assert getattr(cancellation, "__notes__", []) == [
        "XHS browser shutdown also failed: RuntimeError: cleanup failed"
    ]


@pytest.mark.asyncio
async def test_start_raises_shutdown_error_when_session_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = _crawler_without_init()
    cleanup_error = RuntimeError("cleanup is the only failure")
    calls: list[str] = []

    async def complete_session(*_args: object) -> None:
        calls.append("session")

    async def fail_shutdown() -> None:
        calls.append("shutdown")
        raise cleanup_error

    monkeypatch.setattr(crawler, "ports", SimpleNamespace(async_playwright=_PlaywrightContext), raising=False)
    monkeypatch.setattr(crawler, "inputs", dependencies()["inputs"], raising=False)
    monkeypatch.setattr(crawler, "_run_browser_session", complete_session)
    monkeypatch.setattr(crawler, "_prepare_browser_shutdown", fail_shutdown)

    with pytest.raises(RuntimeError) as caught:
        await crawler.start()

    assert caught.value is cleanup_error
    assert calls == ["session", "shutdown"]


@pytest.mark.asyncio
async def test_start_runs_shutdown_once_after_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = _crawler_without_init()
    primary_error = ValueError("invalid response")
    calls: list[str] = []

    async def fail_session(*_args: object) -> None:
        calls.append("session")
        raise primary_error

    async def complete_shutdown() -> None:
        calls.append("shutdown")

    monkeypatch.setattr(crawler, "ports", SimpleNamespace(async_playwright=_PlaywrightContext), raising=False)
    monkeypatch.setattr(crawler, "inputs", dependencies()["inputs"], raising=False)
    monkeypatch.setattr(crawler, "_run_browser_session", fail_session)
    monkeypatch.setattr(crawler, "_prepare_browser_shutdown", complete_shutdown)

    with pytest.raises(ValueError) as caught:
        await crawler.start()

    assert caught.value is primary_error
    assert calls == ["session", "shutdown"]
    assert not getattr(primary_error, "__notes__", [])
