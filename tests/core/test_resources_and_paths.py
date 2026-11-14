"""T01 验收：包内资源为唯一真源、路径集中、台账冻结于基线并跟踪迁移进度。"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT / "scripts", ROOT / "scripts" / "dev"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

resources = import_module("trippostcollect.core.resources")
paths = import_module("trippostcollect.core.paths")


# ---------- 包内资源 ----------


def test_resource_path_is_inside_package_not_fork() -> None:
    with resources.path("js/douyin.js") as location:
        assert location.is_file()
        assert "MediaCrawler" not in location.parts
        assert "trippostcollect" in location.parts


def test_unknown_resource_is_rejected() -> None:
    with pytest.raises(FileNotFoundError):
        resources.read_bytes("js/missing.js")
    with pytest.raises(ValueError):
        resources.read_bytes("../core/paths.py")


def test_resource_text_preserves_bom_handling() -> None:
    # 原调用以 utf-8-sig 读取 JS；读取接口必须给出与之相同的文本
    for name in ("js/douyin.js",):
        raw = resources.read_bytes(name)
        assert resources.read_text(name) == raw.decode("utf-8-sig")


# ---------- 路径集中 ----------

def test_platform_profile_paths_are_centralized(isolated_platform_sessions) -> None:
    # T14：非小红书 profile 定义只在 core.paths，位于 data/runtime/platform_sessions/<platform>/
    assert isolated_platform_sessions.original.sessions == paths.RUNTIME_ROOT / "platform_sessions"
    for platform in ("bilibili", "weibo", "douyin", "zhihu"):
        session = paths.PLATFORM_SESSIONS_ROOT / platform
        assert paths.platform_profile_dir(platform) == session / "profile"
        assert paths.platform_cookie_snapshot_path(platform) == session / "trippostcollect_cookie_snapshot.json"


def test_unknown_platform_profile_is_rejected() -> None:
    with pytest.raises(KeyError):
        paths.platform_profile_dir("kuaishou")


def test_executor_and_warmup_share_profile_definition() -> None:
    crawl = import_module("mediacrawler_crawl")
    warmup = import_module("mediacrawler_login_warmup")
    for platform in ("bilibili", "weibo", "douyin", "zhihu"):
        assert crawl.profile_dir_for(platform) == paths.platform_profile_dir(platform)
        assert warmup.profile_dir_for(platform) == paths.platform_profile_dir(platform)
        assert crawl.cookie_snapshot_path(platform) == warmup.cookie_snapshot_path(platform)


def test_browser_discovery_has_single_definition() -> None:
    launcher = import_module("trippostcollect.runtime.browser_launcher")
    crawl = import_module("mediacrawler_crawl")
    warmup = import_module("mediacrawler_login_warmup")
    assert crawl.discover_cdp_browser_path is launcher.discover_cdp_browser_path
    assert warmup.discover_cdp_browser_path is launcher.discover_cdp_browser_path


def test_browser_discovery_prefers_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = import_module("trippostcollect.runtime.browser_launcher")
    fake = tmp_path / "chrome"
    fake.write_text("")
    monkeypatch.setenv("TRIPPOSTCOLLECT_CUSTOM_BROWSER_PATH", str(fake))
    assert launcher.discover_cdp_browser_path() == str(fake)


def test_prerequisites_check_resources_not_fork_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl = import_module("mediacrawler_crawl")
    calls = []
    monkeypatch.setattr(crawl, "verify_package_resources", lambda: calls.append(True))
    crawl.ensure_prerequisites()
    assert calls == [True]


def test_bilibili_resources_not_read_from_fork(monkeypatch: pytest.MonkeyPatch) -> None:
    # #127 第 2 项起B站行为会话不读任何 JS 资源；执行器仍不得回到 fork libs 目录。
    crawl = import_module("mediacrawler_crawl")
    source = Path(crawl.__file__).read_text()
    assert 'MEDIACRAWLER_DIR / "libs"' not in source
    login = import_module("trippostcollect.platforms.bilibili.login")
    assert "stealth" not in Path(login.__file__).read_text()


# ---------- 台账：基线冻结与迁移进度 ----------


