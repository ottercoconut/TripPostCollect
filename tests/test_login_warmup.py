from __future__ import annotations

import argparse
import asyncio
import sys
from importlib import import_module
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

login_warmup = import_module("login_warmup")


def test_selected_targets_supports_all_aliases_and_deduplication() -> None:
    assert login_warmup.selected_targets(["all"]) == [
        "douyin",
        "zhihu",
        "weibo",
        "bilibili",
    ]
    assert login_warmup.selected_targets(["微博", "wb"]) == ["weibo"]


def test_selected_targets_rejects_unknown_target() -> None:
    with pytest.raises(SystemExit, match="Unknown login target"):
        login_warmup.selected_targets(["unknown"])


def test_run_target_records_failure_and_continues_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fail_warmup(*_args, **_kwargs):
        raise RuntimeError("browser failed")

    monkeypatch.setattr(login_warmup, "warmup_mediacrawler", fail_warmup)
    args = argparse.Namespace(
        timeout_seconds=10,
        output_dir=str(tmp_path),
        browser_path=None,
    )

    result = asyncio.run(login_warmup.run_target(object(), "weibo", tmp_path, args))

    assert result["ok"] is False
    assert result["target"] == "weibo"
    assert result["error"] == "RuntimeError: browser failed"
    assert (tmp_path / "weibo.json").is_file()


def test_markdown_summary_includes_login_refresh_state() -> None:
    summary = {
        "status": "completed",
        "started_at": "2026-07-13T00:00:00+00:00",
        "finished_at": "2026-07-13T00:01:00+00:00",
        "ok_count": 1,
        "failed_count": 0,
        "records": [
            {
                "target": "weibo",
                "target_kind": "mediacrawler",
                "ok": True,
                "initial_ok": False,
                "login_refreshed": True,
                "persisted_ok": True,
                "profile_dir": "/tmp/weibo-profile",
            }
        ],
    }

    report = login_warmup.markdown_summary(summary)

    assert "| weibo | mediacrawler | ok | False | True | True |" in report


def test_weibo_login_accepts_current_mobile_session(monkeypatch: pytest.MonkeyPatch) -> None:
    media_login = sys.modules["mediacrawler_login_warmup"]

    class FakeContext:
        async def cookies(self, _urls):
            return [
                {"name": "SUB", "value": "present"},
                {"name": "MLOGIN", "value": "1"},
            ]

    class FakePage:
        url = "https://m.weibo.cn"

    async def fake_storage(_page):
        return {}

    async def fake_api(_page):
        return {"ok": True, "login": True, "uid": "123"}

    monkeypatch.setattr(media_login, "safe_local_storage", fake_storage)
    monkeypatch.setattr(media_login, "weibo_api_check", fake_api)

    state = asyncio.run(media_login.current_state(FakeContext(), FakePage(), "weibo"))

    assert state["ok"] is True
    assert state["markers"]["current_cookie_pair"] is True
    assert state["markers"]["api_login"] is True


def test_weibo_login_rejects_desktop_cookie_without_mobile_api(monkeypatch: pytest.MonkeyPatch) -> None:
    media_login = sys.modules["mediacrawler_login_warmup"]

    class FakeContext:
        async def cookies(self, urls):
            assert urls == ["https://m.weibo.cn"]
            return [{"name": "WBPSESS", "value": "desktop-only"}]

    class FakePage:
        url = "https://m.weibo.cn"

    async def fake_storage(_page):
        return {}

    async def fake_api(_page):
        return {"ok": True, "login": False, "uid": None}

    monkeypatch.setattr(media_login, "safe_local_storage", fake_storage)
    monkeypatch.setattr(media_login, "weibo_api_check", fake_api)

    state = asyncio.run(media_login.current_state(FakeContext(), FakePage(), "weibo"))

    assert state["ok"] is False
    assert state["markers"]["api_login"] is False


def test_weibo_login_uses_desktop_login_and_mobile_verification() -> None:
    media_login = sys.modules["mediacrawler_login_warmup"]
    config = media_login.PLATFORMS["weibo"]

    assert config["login_url"] == "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog"
    assert config["verify_url"] == "https://m.weibo.cn"
    assert config["login_url"] != config["verify_url"]


def test_weibo_desktop_login_requires_sso_or_changed_session() -> None:
    media_login = sys.modules["mediacrawler_login_warmup"]

    assert media_login.weibo_desktop_login_completed(
        {"WBPSESS": "anonymous"},
        {"WBPSESS": "anonymous", "SSOLoginState": None},
    ) is False
    assert media_login.weibo_desktop_login_completed(
        {"WBPSESS": "anonymous"},
        {"WBPSESS": "authenticated", "SSOLoginState": None},
    ) is True
    assert media_login.weibo_desktop_login_completed(
        {"WBPSESS": "anonymous"},
        {"WBPSESS": "anonymous", "SSOLoginState": "present"},
    ) is True
