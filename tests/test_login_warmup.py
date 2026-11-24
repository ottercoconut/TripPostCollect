"""TripPostCollect tests for login warmup."""

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


def test_login_context_launch_arguments_keep_the_renderer_sandbox(monkeypatch, tmp_path) -> None:
    """#83：登录预热不传 --no-sandbox，并显式开启沙箱（否则 Playwright 自动追加），其他参数不变。

    #139：启动参数末尾追加原生窗口尺寸 ``--window-size=1450,900``。
    """
    from trippostcollect.application import warmup

    launches = []

    class FakeChromium:
        async def launch_persistent_context(self, **kwargs):
            launches.append(kwargs)
            return "context"

    monkeypatch.setattr(warmup, "browser_runtime_args", lambda: ["--lang=zh-CN"])
    monkeypatch.setattr(warmup, "browser_launch_environment", lambda: {})

    playwright = argparse.Namespace(chromium=FakeChromium())
    assert asyncio.run(warmup.launch_login_context(playwright, tmp_path, None)) == "context"

    assert launches[0]["chromium_sandbox"] is True
    assert launches[0]["args"] == [
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--lang=zh-CN",
        "--window-size=1450,900",
    ]


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

    monkeypatch.setattr("trippostcollect.application.warmup.warmup_one", fail_warmup)
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


def test_weibo_login_accepts_truthy_config_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    media_login = import_module("mediacrawler_login_warmup")

    class FakeContext:
        def on(self, _event, _callback=None):
            return None

    class FakePage:
        url = "https://weibo.com/"

        def on(self, _event, _callback=None):
            return None

        async def evaluate(self, script, arg=None):
            assert "$CONFIG" in script
            return {"exists": True, "truthy": True, "uid": "secret-uid"}

    async def fake_storage(_page):
        return {}

    monkeypatch.setattr("trippostcollect.application.warmup.safe_local_storage", fake_storage)
    state = asyncio.run(media_login.current_state(FakeContext(), FakePage(), "weibo"))

    assert state["ok"] is True
    assert state["markers"] == {"uid_exists": True, "uid_truthy": True}
    assert set(state["api"]) == {"exists", "truthy"}


def test_weibo_login_rejects_missing_or_false_config_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    media_login = import_module("mediacrawler_login_warmup")

    class FakeContext:
        def on(self, _event, _callback=None):
            return None

    class FakePage:
        url = "https://weibo.com/"

        def on(self, _event, _callback=None):
            return None

        async def evaluate(self, _script, _arg=None):
            return {"exists": True, "truthy": False}

    async def fake_storage(_page):
        return {}

    monkeypatch.setattr("trippostcollect.application.warmup.safe_local_storage", fake_storage)
    state = asyncio.run(media_login.current_state(FakeContext(), FakePage(), "weibo"))

    assert state["ok"] is False
    assert state["markers"] == {"uid_exists": True, "uid_truthy": False}


def test_weibo_login_uses_desktop_home_and_passport() -> None:
    media_login = import_module("mediacrawler_login_warmup")
    config = media_login.PLATFORMS["weibo"]

    assert config["urls"] == ["https://weibo.com/"]
    assert config["verify_url"] == "https://weibo.com/"
    assert config["login_url"] == "https://passport.weibo.com/sso/signin"
    assert "m.weibo.cn" not in config["required"]


# ---------- #112：warmup 报告不含平台原始响应与头像 ----------
# 用 Playwright 自带的 node driver 执行 page.evaluate 使用的同一段脚本，只把 fetch 换成合成响应；
# 不联网、不启动浏览器。

import json  # noqa: E402
import subprocess  # noqa: E402

from playwright._impl._driver import compute_driver_executable  # noqa: E402

from trippostcollect.records.sanitization import AUTHOR_AVATAR_KEYS  # noqa: E402

LOGIN_SCRIPT_RUNNER = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
globalThis.fetch = async () => {
  if (input.throws) { throw new TypeError('Failed to fetch https://www.zhihu.com/secret?token=abc'); }
  return {
    ok: input.status >= 200 && input.status < 300,
    status: input.status,
    json: async () => JSON.parse(input.body),
    text: async () => input.body,
  };
};
(async () => {
  const result = await eval('(' + input.script + ')')();
  process.stdout.write(JSON.stringify(result));
})();
"""
AVATAR = "https://picx.zhimg.com/v2-synthetic_l.jpg"
ME_BODY = json.dumps({
    "id": "abc", "uid": "1234567890", "name": "青岛旅人", "url_token": "qd",
    "avatar_url": AVATAR, "avatar_url_template": AVATAR.replace("_l", "_{size}"),
    "email": "secret@example.test", "headline": "签名",
})
ERROR_BODY = json.dumps({"error": {
    "code": 100, "name": "AuthenticationInvalidRequest",
    "message": "请求头或参数封装错误 " + AVATAR, "need_login": True,
}})
def evaluate_login_script(name: str, *, status: int, body: str = "", throws: bool = False) -> dict:
    from trippostcollect.application import warmup

    scripts = []

    class RecordingPage:
        async def evaluate(self, script):
            scripts.append(script)
            node, _cli = compute_driver_executable()
            completed = subprocess.run(
                [str(node), "-e", LOGIN_SCRIPT_RUNNER],
                input=json.dumps({"script": script, "status": status, "body": body, "throws": throws}),
                capture_output=True, text=True, timeout=60, env={}, check=True,
            )
            return json.loads(completed.stdout)

    result = asyncio.run(getattr(warmup, name)(RecordingPage()))
    assert len(scripts) == 1
    return result


def assert_no_avatar_or_raw_text(value) -> None:
    text = json.dumps(value, ensure_ascii=False)
    assert AVATAR not in text and "zhimg" not in text
    assert not any(f'"{key}"' in text for key in AUTHOR_AVATAR_KEYS)
    for raw in ("secret@example.test", "签名", "请求头", "token=abc", '"text"', "need_login"):
        assert raw not in text


def test_zhihu_me_success_keeps_only_uid_and_name() -> None:
    result = evaluate_login_script("zhihu_api_check", status=200, body=ME_BODY)

    assert result == {"ok": True, "status": 200, "uid": "1234567890", "name": "青岛旅人"}
    assert_no_avatar_or_raw_text(result)


def test_zhihu_me_failure_keeps_only_status_and_clean_error_identity() -> None:
    result = evaluate_login_script("zhihu_api_check", status=401, body=ERROR_BODY)

    assert result == {"ok": False, "status": 401, "error_code": 100, "error_name": "AuthenticationInvalidRequest"}
    assert_no_avatar_or_raw_text(result)


def test_zhihu_me_non_json_body_keeps_no_raw_text() -> None:
    result = evaluate_login_script("zhihu_api_check", status=502, body=f"<html>{AVATAR}</html>")

    assert result == {"ok": False, "status": 502}


def test_login_check_network_error_keeps_only_error_name() -> None:
    result = evaluate_login_script("zhihu_api_check", status=0, throws=True)

    assert result["ok"] is False and result["error_name"] == "TypeError"
    assert_no_avatar_or_raw_text(result)


def test_weibo_config_uid_script_reports_exists_and_truthy_only() -> None:
    from trippostcollect.platforms.weibo.page_scripts import STATE_SCRIPTS

    script = STATE_SCRIPTS["weibo.config_uid"]
    node, _cli = compute_driver_executable()
    runner = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
globalThis.window = { $CONFIG: input.config };
const result = eval('(' + input.script + ')')();
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        [str(node), "-e", runner],
        input=json.dumps({
            "script": script,
            "config": {"uid": "7654321", "avatar_hd": AVATAR},
        }),
        capture_output=True, text=True, timeout=60, env={}, check=True,
    )
    assert json.loads(completed.stdout) == {"exists": True, "truthy": True}


def test_login_check_projection_drops_unknown_and_unsafe_fields() -> None:
    from trippostcollect.application import warmup

    result = warmup.project_login_api_result(
        {"ok": False, "status": 403, "text": ME_BODY, "avatar_url": AVATAR,
         "error_code": f"code {AVATAR}", "error_name": "Error: " + "x" * 200},
        success_keys=("uid", "name"),
    )
    assert result == {"ok": False, "status": 403}
    assert warmup.project_login_api_result(None, success_keys=("uid",)) == {
        "ok": False, "status": None, "error_name": "unexpected_result",
    }


def test_warmup_report_files_carry_no_avatar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """知乎 warmup 全流程写出的 zhihu.json（含 initial_state、state、reopen_verify）不含头像键与原文。"""

    from trippostcollect.application import warmup

    monkeypatch.setattr(
        warmup, "zhihu_api_check",
        lambda _page: asyncio.sleep(0, warmup.project_login_api_result(
            {"ok": True, "status": 200, "uid": "1", "name": "n", "text": ME_BODY, "avatar_url": AVATAR},
            success_keys=("uid", "name"),
        )),
    )

    class FakePage:
        url = "https://www.zhihu.com"

        async def goto(self, *_a, **_k):
            return None

        async def evaluate(self, _script):
            return {}

        async def wait_for_timeout(self, _ms):
            return None

    class FakeContext:
        pages = [FakePage()]

        async def cookies(self, _urls=None):
            return [{"name": "z_c0", "value": "z"}, {"name": "d_c0", "value": "d"}]

        async def close(self):
            return None

    async def fake_launch(*_args, **_kwargs):
        return FakeContext()

    monkeypatch.setattr(warmup, "launch_login_context", fake_launch)
    monkeypatch.setattr(warmup, "discover_cdp_browser_path", lambda: None)
    args = argparse.Namespace(
        browser_path=None, timeout_seconds=5, no_close_on_success=False, skip_reopen_verify=False,
    )
    result = asyncio.run(warmup.warmup_one(None, "zhihu", tmp_path, args))

    assert result["ok"] is True
    written = json.loads((tmp_path / "zhihu.json").read_text(encoding="utf-8"))
    assert written["state"]["api"] == {"ok": True, "status": 200, "uid": "1", "name": "n"}
    assert_no_avatar_or_raw_text(written)
