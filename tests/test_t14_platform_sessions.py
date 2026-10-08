"""T14：非小红书持久 profile 与 Cookie 快照迁出 fork 目录，并对未迁移旧目录失败关闭。

全部只用 tmp_path 下的临时目录；不访问网络、不启动浏览器、不读写真实 profile。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from trippostcollect.core import paths

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "trippostcollect"
GENERIC = {"bilibili": "bili", "weibo": "wb", "douyin": "dy", "zhihu": "zhihu"}
SECRET = "t14-secret-cookie-value"


@pytest.fixture
def session_roots(isolated_platform_sessions) -> tuple[Path, Path]:
    # conftest 的自动隔离已把两个根指向本用例的临时目录。
    return isolated_platform_sessions.legacy, isolated_platform_sessions.sessions


def _legacy_profile(legacy: Path, name: str) -> Path:
    profile = legacy / name
    profile.mkdir(parents=True)
    (profile / "trippostcollect_cookie_snapshot.json").write_text(
        json.dumps({"cookies": [{"name": "z_c0", "value": SECRET}]}), encoding="utf-8",
    )
    return profile


# ---------- paths 新布局 ----------

def test_platform_sessions_layout_is_under_runtime_not_fork(isolated_platform_sessions) -> None:
    original = isolated_platform_sessions.original
    assert original.sessions == paths.RUNTIME_ROOT / "platform_sessions"
    assert original.legacy == paths.MEDIACRAWLER_DIR / "browser_data"
    for platform in GENERIC:
        session = paths.PLATFORM_SESSIONS_ROOT / platform
        assert paths.platform_session_dir(platform) == session
        assert paths.platform_profile_dir(platform) == session / "profile"
        assert paths.platform_cdp_profile_dir(platform) == session / "cdp_profile"
        assert paths.platform_cookie_snapshot_path(platform) == session / paths.COOKIE_SNAPSHOT_FILENAME
        assert paths.COOKIE_SNAPSHOT_FILENAME == "trippostcollect_cookie_snapshot.json"
        for value in (
            paths.platform_profile_dir(platform),
            paths.platform_cdp_profile_dir(platform),
            paths.platform_cookie_snapshot_path(platform),
        ):
            assert not value.is_relative_to(paths.TOOLS_ROOT)


def test_xhs_and_unknown_platforms_have_no_persistent_session() -> None:
    for function in (
        paths.platform_profile_dir,
        paths.platform_cdp_profile_dir,
        paths.platform_cookie_snapshot_path,
        paths.require_platform_session_migrated,
    ):
        with pytest.raises(ValueError, match="XHS_SESSION_ROOT"):
            function("xhs")
        with pytest.raises(KeyError):
            function("kuaishou")


def test_profile_code_maps_back_to_platform_key() -> None:
    for platform, code in GENERIC.items():
        assert paths.platform_key_for_profile_code(code) == platform
    with pytest.raises(KeyError):
        paths.platform_key_for_profile_code("ks")


def test_legacy_pairs_are_one_to_one(session_roots: tuple[Path, Path]) -> None:
    legacy, sessions = session_roots
    for platform, code in GENERIC.items():
        assert paths.legacy_fork_profile_dirs(platform) == (
            (legacy / f"{code}_user_data_dir", sessions / platform / "profile"),
            (legacy / f"cdp_{code}_user_data_dir", sessions / platform / "cdp_profile"),
        )


# ---------- Cookie 快照读写同一路径 ----------

def test_cookie_snapshot_write_and_load_use_the_same_sibling_path(session_roots: tuple[Path, Path]) -> None:
    from trippostcollect.application import warmup
    from trippostcollect.runtime import cookies

    _, sessions = session_roots
    target = paths.platform_cookie_snapshot_path("zhihu")
    cookie_rows = [{"name": "d_c0", "value": "d"}, {"name": "z_c0", "value": "z"}]
    info = cookies.write_cookie_snapshot(
        "zhihu", target, cookie_rows,
        source="reopen_verify", label="知乎", urls=["https://www.zhihu.com"], state={"markers": {}},
    )
    assert info["path"] == str(sessions / "zhihu" / "trippostcollect_cookie_snapshot.json")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # 快照与 profile 同级，写快照不创建或写入 profile 目录。
    assert not paths.platform_profile_dir("zhihu").exists()
    loaded = cookies.load_cookie_snapshot("zhihu")
    assert loaded["snapshot_path"] == str(target)
    assert loaded["cookie_header"] == "d_c0=d;z_c0=z"
    # warmup 写快照与 collection/cookies 读取取同一定义。
    target.unlink()
    assert warmup.cookie_snapshot_path("zhihu") == target
    info = warmup.write_cookie_snapshot("zhihu", cookie_rows, source="session", state={})
    assert info["path"] == str(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert cookies.load_cookie_snapshot("zhihu")["snapshot_path"] == str(target)


# ---------- 失败关闭检查 ----------

@pytest.mark.parametrize("platform", sorted(GENERIC))
def test_legacy_profile_without_new_profile_is_refused(
    session_roots: tuple[Path, Path], platform: str,
) -> None:
    legacy, sessions = session_roots
    _legacy_profile(legacy, f"{GENERIC[platform]}_user_data_dir")
    with pytest.raises(RuntimeError, match=rf"^platform_session_migration_required:{platform} ") as caught:
        paths.require_platform_session_migrated(platform)
    assert SECRET not in str(caught.value)
    assert "operations-runbook.md" in str(caught.value)
    # 失败关闭：不创建新目录，也不回退使用旧目录。
    assert not sessions.exists()


def test_legacy_cdp_profile_is_checked_separately(session_roots: tuple[Path, Path]) -> None:
    legacy, _ = session_roots
    _legacy_profile(legacy, "wb_user_data_dir")
    _legacy_profile(legacy, "cdp_wb_user_data_dir")
    paths.platform_profile_dir("weibo").mkdir(parents=True)
    with pytest.raises(RuntimeError, match=r"cdp_wb_user_data_dir -> .*cdp_profile"):
        paths.require_platform_session_migrated("weibo")
    paths.platform_cdp_profile_dir("weibo").mkdir()
    paths.require_platform_session_migrated("weibo")


def test_new_profile_passes_even_when_legacy_remains(session_roots: tuple[Path, Path]) -> None:
    legacy, _ = session_roots
    _legacy_profile(legacy, "dy_user_data_dir")
    paths.platform_profile_dir("douyin").mkdir(parents=True)
    paths.require_platform_session_migrated("douyin")


def test_first_login_without_any_profile_passes(session_roots: tuple[Path, Path]) -> None:
    _, sessions = session_roots
    for platform in GENERIC:
        paths.require_platform_session_migrated(platform)
    assert not sessions.exists()


def test_migration_failure_is_classified_final() -> None:
    from trippostcollect.application.failures import classify_attempt

    result = classify_attempt(
        exit_code=1,
        stderr=(
            "platform_session_failed:RuntimeError:platform_session_migration_required:zhihu "
            "(按 docs/operations-runbook.md「T14 非小红书登录资料迁移」迁移：a -> b)"
        ),
        meta={"platform": "zhihu"},
    )
    assert result == {
        "status": "failed_final",
        "failure_type": "platform_session_migration_required",
        "retryable": False,
        "wait_seconds": 0,
        "reason": "platform_session_migration_required:zhihu",
    }


def test_runner_refuses_before_policy_guard_and_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session_roots: tuple[Path, Path],
) -> None:
    from trippostcollect.application import collection

    legacy, _ = session_roots
    _legacy_profile(legacy, "wb_user_data_dir")

    def forbidden(*args, **kwargs):
        raise AssertionError("must not consume site budget or start a worker")

    monkeypatch.setattr(collection, "site_request_guard", forbidden)
    monkeypatch.setattr(collection, "_run_platform_without_policy", forbidden)
    record = collection.run_platform(
        "weibo", argparse.Namespace(keyword="青岛"), tmp_path / "batch", ports=None,
    )
    assert record["ok"] is False
    assert record["policy_events"] == []
    assert record["failure_classification"]["failure_type"] == "platform_session_migration_required"
    assert record["failure_classification"]["reason"] == "platform_session_migration_required:weibo"
    assert SECRET not in json.dumps(record, ensure_ascii=False)


def test_worker_entry_refuses_generic_platform_but_not_xhs(session_roots: tuple[Path, Path]) -> None:
    from trippostcollect.platforms import entry

    legacy, _ = session_roots
    for code in ("wb", "dy", "zhihu"):
        _legacy_profile(legacy, f"{code}_user_data_dir")
        with pytest.raises(RuntimeError, match="^platform_session_migration_required:"):
            entry.require_persistent_session_migrated(code)
    _legacy_profile(legacy, "xhs_user_data_dir")
    entry.require_persistent_session_migrated("xhs")


def test_warmup_refuses_before_creating_new_profile(
    session_roots: tuple[Path, Path], tmp_path: Path,
) -> None:
    from trippostcollect.application import warmup

    legacy, sessions = session_roots
    _legacy_profile(legacy, "bili_user_data_dir")
    args = argparse.Namespace(browser_path=None, timeout_seconds=1)
    with pytest.raises(RuntimeError, match="^platform_session_migration_required:bilibili"):
        asyncio.run(warmup.warmup_one(None, "bilibili", tmp_path, args))
    assert not sessions.exists()


# ---------- warmup 不再依赖 fork 目录 ----------

def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names
    }


def test_runtime_modules_no_longer_reference_fork_directory() -> None:
    for relative in (
        "application/warmup.py",
        "artifacts/jsonl.py",
        "platforms/entry.py",
        "platforms/weibo/core.py",
        "platforms/douyin/core.py",
        "platforms/zhihu/core.py",
        "platforms/douyin/login_support.py",
        "runtime/browser.py",
        "runtime/cookies.py",
    ):
        source = (PACKAGE / relative).read_text(encoding="utf-8")
        assert "MEDIACRAWLER_DIR" not in _names(PACKAGE / relative), relative
        assert "browser_data" not in source, relative


def test_unified_warmup_runs_without_fork_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from trippostcollect.application import warmup

    class FakePlaywright:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_target(playwright, target, batch_dir, args):
        return {"target": target, "ok": True, "profile_dir": str(paths.platform_profile_dir(target))}

    monkeypatch.setattr(paths, "MEDIACRAWLER_DIR", tmp_path / "missing-fork")
    monkeypatch.setattr(warmup, "async_playwright", FakePlaywright)
    monkeypatch.setattr(warmup, "run_target", fake_target)
    args = argparse.Namespace(timeout_seconds=1, targets=["all"], output_dir=str(tmp_path / "out"))
    assert asyncio.run(warmup.main_async(args)) == 0


# ---------- 抖音滑块临时图落点 ----------

def test_douyin_slider_images_use_runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import cv2
    import numpy as np

    from trippostcollect.platforms.douyin import login_support

    assert paths.DOUYIN_SLIDER_IMAGE_DIR.is_relative_to(paths.RUNTIME_ROOT)
    target = tmp_path / "douyin_slider_images"
    monkeypatch.setattr(login_support, "DOUYIN_SLIDER_IMAGE_DIR", target)
    ok, encoded = cv2.imencode(".jpg", np.zeros((8, 8, 3), dtype="uint8"))
    assert ok
    requests = []

    def fake_get(url, headers):
        requests.append(url)
        return SimpleNamespace(status_code=200, content=encoded.tobytes())

    monkeypatch.setattr(login_support.httpx, "get", fake_get)
    slide = login_support.Slide(gap="https://example.invalid/gap.png", bg="https://example.invalid/bg.png")
    assert slide.img_dir == str(target)
    assert slide.out == str(target / "out.jpg")
    assert slide.bg == str(target / "bg.jpg") and Path(slide.bg).is_file()
    assert slide.gap == str(target / "gap.jpg") and Path(slide.gap).is_file()
    assert len(requests) == 2


# ---------- entry/jsonl 缺省值不指向 fork ----------

def test_jsonl_writer_requires_explicit_output_root(tmp_path: Path) -> None:
    from trippostcollect.artifacts.jsonl import AsyncFileWriter

    writer = AsyncFileWriter(
        "weibo", "search", save_data_path=lambda: "", current_date=lambda: "2026-10-08", sanitizer=dict,
    )
    with pytest.raises(RuntimeError, match="^jsonl_save_data_path_required$"):
        asyncio.run(writer.write_to_jsonl({"note_id": "1"}, "contents"))
    with pytest.raises(TypeError):
        AsyncFileWriter("weibo", "search")
    writer = AsyncFileWriter(
        "weibo", "search", save_data_path=lambda: str(tmp_path), current_date=lambda: "2026-10-08", sanitizer=dict,
    )
    asyncio.run(writer.write_to_jsonl({"note_id": "1"}, "contents"))
    assert (tmp_path / "weibo" / "jsonl" / "search_contents_2026-10-08.jsonl").is_file()


def test_entry_image_root_requires_explicit_save_path(tmp_path: Path) -> None:
    from trippostcollect.platforms import entry

    with pytest.raises(RuntimeError, match="^worker_save_data_path_required$"):
        entry._save_data_root("")
    assert entry._save_data_root(str(tmp_path)) == tmp_path


# ---------- 三站持久 profile 落点 ----------

@pytest.mark.parametrize(
    ("module", "cls", "attr", "platform"),
    [
        ("weibo", "WeiboCrawler", "config", "weibo"),
        ("douyin", "DouYinCrawler", "settings", "douyin"),
        ("zhihu", "ZhihuCrawler", "settings", "zhihu"),
    ],
)
def test_crawler_persistent_context_uses_platform_session_profile(
    session_roots: tuple[Path, Path], module: str, cls: str, attr: str, platform: str,
) -> None:
    import importlib

    crawler_cls = getattr(importlib.import_module(f"trippostcollect.platforms.{module}.core"), cls)
    launches = []

    class FakeChromium:
        async def launch_persistent_context(self, **kwargs):
            launches.append(kwargs)
            return "context"

    fake_self = SimpleNamespace(
        **{attr: SimpleNamespace(SAVE_LOGIN_STATE=True, PLATFORM=GENERIC[platform])},
        ports=SimpleNamespace(project_browser_args=lambda: []),
    )
    result = asyncio.run(crawler_cls.launch_browser(fake_self, FakeChromium(), None, None, True))
    assert result == "context"
    # 与旧行为一致：目录由浏览器在首次启动时创建，crawler 只给出位置。
    assert launches[0]["user_data_dir"] == str(paths.platform_profile_dir(platform))


# ---------- CDP 模式 profile 落点 ----------

@pytest.mark.parametrize(("share", "leaf"), [(True, "profile"), (False, "cdp_profile")])
def test_cdp_manager_profile_follows_share_flag(
    monkeypatch: pytest.MonkeyPatch, session_roots: tuple[Path, Path], share: bool, leaf: str,
) -> None:
    from dataclasses import replace
    from unittest.mock import AsyncMock, MagicMock

    from support.browser_settings import BROWSER_SETTINGS
    from trippostcollect.runtime.browser import CDPBrowserManager

    _, sessions = session_roots
    if share:
        monkeypatch.setenv("TRIPPOSTCOLLECT_SHARE_CDP_PROFILE", "1")
    else:
        monkeypatch.delenv("TRIPPOSTCOLLECT_SHARE_CDP_PROFILE", raising=False)
    manager = CDPBrowserManager(replace(BROWSER_SETTINGS, PLATFORM="wb"), project_browser_args=lambda: [])
    manager.debug_port = 9444
    manager.launcher = MagicMock()
    manager.launcher.wait_for_browser_ready.return_value = True
    manager._test_cdp_connection = AsyncMock(return_value=True)
    manager._clean_session_restore_tabs = MagicMock()
    monkeypatch.setattr("trippostcollect.runtime.browser.asyncio.sleep", AsyncMock())
    asyncio.run(manager._launch_browser("/fake/chrome", True))
    expected = str(sessions / "weibo" / leaf)
    assert manager.launcher.launch_browser.call_args.kwargs["user_data_dir"] == expected
    assert Path(expected).is_dir()


# ---------- 未完成的迁移残留（.partial）失败关闭 ----------

@pytest.mark.parametrize("leaf", ["profile", "cdp_profile"])
@pytest.mark.parametrize("legacy_exists", [True, False])
def test_partial_copy_is_refused_even_when_target_exists(
    session_roots: tuple[Path, Path], leaf: str, legacy_exists: bool,
) -> None:
    legacy, sessions = session_roots
    if legacy_exists:
        _legacy_profile(legacy, "zhihu_user_data_dir")
        _legacy_profile(legacy, "cdp_zhihu_user_data_dir")
    (sessions / "zhihu" / "profile").mkdir(parents=True)
    (sessions / "zhihu" / "cdp_profile").mkdir()
    (sessions / "zhihu" / f"{leaf}.partial").mkdir()
    with pytest.raises(RuntimeError, match=rf"^platform_session_migration_required:zhihu .*{leaf}\.partial"):
        paths.require_platform_session_migrated("zhihu")
    (sessions / "zhihu" / f"{leaf}.partial").rmdir()
    paths.require_platform_session_migrated("zhihu")


# ---------- entry.main 检查 ----------

def _golden_argv(name: str, tmp: Path) -> list[str]:
    commands = json.loads((ROOT / "tests/golden/t02_worker_commands.json").read_text(encoding="utf-8"))
    return [part.replace("<TMP>", str(tmp)) for part in commands[name]["cmd"][4:]]


def test_entry_main_refuses_before_hooks_and_crawler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session_roots: tuple[Path, Path],
) -> None:
    from trippostcollect.platforms import entry

    legacy, _ = session_roots
    _legacy_profile(legacy, "wb_user_data_dir")

    def forbidden(*args, **kwargs):
        raise AssertionError("worker must refuse before hooks or crawler start")

    monkeypatch.setattr(entry, "install_hooks", forbidden)
    monkeypatch.setattr(entry, "load_crawler", forbidden)
    monkeypatch.setattr(entry, "_config", None)
    with pytest.raises(RuntimeError, match="^platform_session_migration_required:weibo "):
        entry.main(_golden_argv("weibo_search", tmp_path))


# ---------- 未迁移 checkout 下测试隔离仍成立 ----------

ENTRY_PROBE = (
    "import json\n"
    "{redirect}"
    "import trippostcollect.platforms.entry as entry\n"
    "class FakeCrawler:\n"
    "    browser_context = None\n"
    "    async def start(self):\n"
    "        pass\n"
    "entry.load_crawler = lambda code: FakeCrawler\n"
    "print(json.dumps({{'code': entry.main({argv!r})}}))\n"
)


def _run_entry_probe(tmp_path: Path, checkout: Path, *, redirect: bool) -> subprocess.CompletedProcess[str]:
    from support.platform_sessions import child_redirect_source

    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONIOENCODING"] = "utf-8"
    # 现有工作根机制：子进程的 core.paths 以 fake checkout 为根，其旧 fork 目录模拟 Mac 上尚未迁移的状态。
    environment["TRIPPOST_PROJECT_ROOT"] = str(checkout)
    code = ENTRY_PROBE.format(
        redirect=child_redirect_source(tmp_path / "isolation") if redirect else "",
        argv=_golden_argv("weibo_search", tmp_path),
    )
    return subprocess.run(
        [sys.executable, "-P", "-c", code], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=120, check=False,
    )


def test_subprocess_entry_cases_pass_in_unmigrated_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _legacy_profile(checkout / "tools/MediaCrawler/browser_data", "wb_user_data_dir")
    control = _run_entry_probe(tmp_path, checkout, redirect=False)
    assert control.returncode != 0
    assert "platform_session_migration_required:weibo" in control.stderr
    assert SECRET not in control.stderr + control.stdout
    isolated = _run_entry_probe(tmp_path, checkout, redirect=True)
    assert isolated.returncode == 0, isolated.stderr[-3000:]
    assert json.loads(isolated.stdout.strip().splitlines()[-1]) == {"code": 0}


def test_in_process_cases_pass_in_unmigrated_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from support.platform_sessions import redirect_platform_session_roots
    from trippostcollect.application import collection

    class Reached(BaseException):
        pass

    def reached(*args, **kwargs):
        raise Reached

    unmigrated = tmp_path / "checkout" / "tools/MediaCrawler/browser_data"
    for code in GENERIC.values():
        _legacy_profile(unmigrated, f"{code}_user_data_dir")
    monkeypatch.setattr(collection, "_run_platform_without_policy", reached)

    @contextlib.contextmanager
    def no_policy(*args, **kwargs):
        yield {}

    monkeypatch.setattr(collection, "site_request_guard", no_policy)
    # 对照：不隔离时（旧根指向未迁移目录）进程内正式入口被拒绝。
    monkeypatch.setattr(paths, "LEGACY_FORK_PROFILE_ROOT", unmigrated)
    record = collection.run_platform("douyin", argparse.Namespace(keyword="青岛"), tmp_path / "a", ports=None)
    assert record["failure_classification"]["reason"] == "platform_session_migration_required:douyin"
    # conftest 使用的同一重定向恢复隔离后，进程内用例越过检查进入原有流程。
    redirect_platform_session_roots(monkeypatch, tmp_path / "isolation")
    for platform in GENERIC:
        paths.require_platform_session_migrated(platform)
    with pytest.raises(Reached):
        collection.run_platform("douyin", argparse.Namespace(keyword="青岛"), tmp_path / "b", ports=None)
